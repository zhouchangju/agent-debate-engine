"""Private, auditable storage for debate runs.

The store owns an exclusive lock for its lifetime.  Callers should therefore
close it explicitly or use it as a context manager.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib
import json
import math
import os
import re
import socket
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Literal

import yaml

from agent_debate.errors import DebateError, ResumeError
from agent_debate.models import ensure_portable_path_component

SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE_KEYS = {
    "access_key",
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer_token",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "session",
    "session_id",
    "signing_key",
    "ssh_key",
    "token",
}
_SENSITIVE_SUFFIXES = (
    "_access_key",
    "_access_token",
    "_api_key",
    "_auth_token",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_secret_key",
    "_session",
    "_signing_key",
    "_ssh_key",
    "_token",
)
_TERMINAL_STATUSES = {"cancelled", "completed", "failed"}
_RUN_STATUSES = {
    "blocked",
    "cancelled",
    "completed",
    "exhausted",
    "failed",
    "finalized",
    "judging",
    "running",
    "timed_out",
}
_BASELINE_ARTIFACTS = {
    "config.resolved.yaml",
    "events.jsonl",
    "request.md",
}
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_INVOCATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_INVOCATION_KINDS = {"judge_attempt", "participant"}
_INVOCATION_STATUSES = {
    "cancelled",
    "failed",
    "output_limit",
    "success",
    "timed_out",
}
_MAX_ARTIFACT_READ_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_EVENT_TYPE_LENGTH = 128
_RESULT_CONTENT_KEYS = {
    "decision",
    "final",
    "final_output",
    "output",
    "prompt",
    "raw",
    "raw_output",
    "stderr",
    "stdout",
    "text",
}

_FALLBACK_LOCKS: set[str] = set()
_FALLBACK_LOCKS_GUARD = threading.Lock()
_FCNTL: Any = importlib.import_module("fcntl") if os.name != "nt" else None
_MSVCRT: Any = importlib.import_module("msvcrt") if os.name == "nt" else None


class ArtifactError(DebateError):
    """Base class for artifact persistence errors."""


class UnsafeArtifactPathError(ArtifactError, ValueError):
    """An identifier or relative path could escape the run directory."""


class ArtifactIntegrityError(ResumeError):
    """Saved artifact content does not match the manifest."""


class RunLockedError(ResumeError):
    """A run is already open by another store."""


def utc_now() -> str:
    """Return a sortable RFC 3339 UTC timestamp."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def content_sha256(content: str | bytes | bytearray | memoryview) -> str:
    """Return the SHA-256 digest for exact content bytes."""

    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest for a file without loading it all at once."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_path_id(value: object, *, kind: str = "identifier") -> str:
    """Validate one user-controlled path component and return it as text."""

    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise UnsafeArtifactPathError(f"{kind} must be a string")
    if not _SAFE_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise UnsafeArtifactPathError(f"unsafe {kind}: {value!r}")
    try:
        ensure_portable_path_component(value)
    except ValueError as exc:
        raise UnsafeArtifactPathError(f"reserved {kind}: {value!r}") from exc
    return value


def redact_config(config: object) -> Any:
    """Return a JSON-compatible configuration snapshot with secrets removed."""

    value = _jsonable(config)
    if not isinstance(value, dict):
        return _redact(value)

    # Agent identifiers are mapping keys, not configuration field names. IDs
    # such as ``token`` and ``auth`` must survive a run/resume round trip.
    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text == "agents" and isinstance(item, Mapping):
            result[key_text] = {
                str(agent_id): _redact(agent_config) for agent_id, agent_config in item.items()
            }
        else:
            result[key_text] = REDACTED if _is_sensitive_key(key_text) else _redact(item)
    return result


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Atomically replace a file with private permissions."""

    target = Path(path)
    _make_private_dir(target.parent)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(descriptor, "wb") as file_handle:
            descriptor = None
            file_handle.write(data)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        _best_effort_chmod(temporary, FILE_MODE)
        os.replace(temporary, target)  # noqa: PTH105 - the protocol explicitly requires os.replace
        _best_effort_chmod(target, FILE_MODE)
        _best_effort_fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def atomic_write_text(path: str | Path, text: str) -> None:
    """Atomically replace a UTF-8 text file with private permissions."""

    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | Path, value: object) -> None:
    """Atomically replace a deterministic UTF-8 JSON file."""

    atomic_write_bytes(path, _json_bytes(value))


def read_manifest(run_dir: str | Path) -> dict[str, Any]:
    """Read a manifest from a run directory (or a manifest path)."""

    path = Path(run_dir)
    if path.name != "manifest.json":
        path = path / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError(f"cannot read manifest at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactIntegrityError(f"manifest at {path} is not a JSON object")
    return value


def _validated_existing_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = _read_relative_bytes(
            path,
            Path("manifest.json"),
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError(f"cannot safely read manifest at {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ArtifactIntegrityError(f"manifest at {path} is not a JSON object")
    _validate_manifest_v1(manifest, path)
    return manifest


def _validate_manifest_v1(manifest: dict[str, Any], path: Path) -> None:
    """Fail closed on incompatible, ambiguous, or uncontained persisted state."""

    _validate_manifest_header(manifest, path)
    artifacts = _validate_manifest_artifacts(manifest)
    _validate_manifest_invocations(manifest, artifacts)
    judge_count = _validate_manifest_judges(manifest, artifacts)
    _validate_manifest_progress(manifest, artifacts, judge_count)


def _validate_manifest_header(manifest: Mapping[str, Any], path: Path) -> None:
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ArtifactIntegrityError("unsupported run manifest schema_version")
    try:
        run_id = validate_path_id(manifest.get("run_id"), kind="run id")
    except UnsafeArtifactPathError as exc:
        raise ArtifactIntegrityError(str(exc)) from exc
    if run_id != path.name:
        raise ArtifactIntegrityError("manifest run_id does not match its directory")

    status = manifest.get("status")
    if not isinstance(status, str) or status not in _RUN_STATUSES:
        raise ArtifactIntegrityError("manifest status is invalid")
    for name in ("event_count", "resume_count"):
        value = manifest.get(name)
        if type(value) is not int or value < 0:
            raise ArtifactIntegrityError(f"manifest {name} must be a non-negative integer")

    fixed_pointers = {
        "config_snapshot": "config.resolved.yaml",
        "events_artifact": "events.jsonl",
        "request_artifact": "request.md",
    }
    for field, expected in fixed_pointers.items():
        if manifest.get(field) != expected:
            raise ArtifactIntegrityError(f"manifest {field} is not the canonical v1 path")
    evidence_artifact = manifest.get("evidence_artifact")
    if evidence_artifact is not None and evidence_artifact != "evidence.md":
        raise ArtifactIntegrityError("manifest evidence_artifact is not the canonical v1 path")
    result_artifact = manifest.get("result_artifact")
    if result_artifact is not None and result_artifact != "result.json":
        raise ArtifactIntegrityError("manifest result_artifact is not the canonical v1 path")


def _validate_manifest_artifacts(manifest: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ArtifactIntegrityError("manifest artifacts field is not an object")
    if not _BASELINE_ARTIFACTS.issubset(artifacts):
        missing = ", ".join(sorted(_BASELINE_ARTIFACTS - artifacts.keys()))
        raise ArtifactIntegrityError(f"manifest is missing baseline artifact(s): {missing}")
    evidence_artifact = manifest.get("evidence_artifact")
    if evidence_artifact is not None and evidence_artifact not in artifacts:
        raise ArtifactIntegrityError("manifest evidence_artifact is not indexed")
    result_artifact = manifest.get("result_artifact")
    if result_artifact is not None and result_artifact not in artifacts:
        raise ArtifactIntegrityError("manifest result_artifact is not indexed")
    for relative_text, record in artifacts.items():
        canonical = _canonical_manifest_path(relative_text)
        if canonical != relative_text:
            raise ArtifactIntegrityError(f"artifact path is not canonical: {relative_text!r}")
        _validate_file_record(record, relative_text)
    return artifacts


def _validate_manifest_invocations(
    manifest: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> None:
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        raise ArtifactIntegrityError("manifest invocations field is not a list")
    invocation_ids: set[str] = set()
    invocation_sequences: list[int] = []
    for invocation in invocations:
        expected_base, invocation_id = _validate_invocation_identity(invocation)
        if invocation_id in invocation_ids:
            raise ArtifactIntegrityError("manifest invocation_id values must be unique")
        invocation_ids.add(invocation_id)
        invocation_sequences.append(invocation["invocation_sequence"])
        _validate_index_artifacts(
            invocation.get("artifacts"),
            artifacts,
            expected_base,
            {"final.md", "meta.json", "request.md", "stderr.log", "stdout.log"},
        )
    if invocation_sequences != list(range(1, len(invocation_sequences) + 1)):
        raise ArtifactIntegrityError(
            "manifest invocation_sequence values must be ordered and contiguous"
        )


def _validate_invocation_identity(invocation: object) -> tuple[str, str]:
    if not isinstance(invocation, dict):
        raise ArtifactIntegrityError("manifest invocation entry is not an object")
    number = _strict_manifest_round(invocation.get("round_number"))
    try:
        stage = validate_path_id(invocation.get("stage"), kind="stage")
        participant = validate_path_id(invocation.get("participant"), kind="participant")
    except UnsafeArtifactPathError as exc:
        raise ArtifactIntegrityError(str(exc)) from exc
    invocation_id = invocation.get("invocation_id")
    if not isinstance(invocation_id, str) or _INVOCATION_ID_RE.fullmatch(invocation_id) is None:
        raise ArtifactIntegrityError("manifest invocation_id is invalid")
    expected_base = (
        Path("rounds") / f"{number:03d}" / stage / participant / invocation_id
    ).as_posix()
    if invocation.get("path") != expected_base:
        raise ArtifactIntegrityError("manifest invocation path is not canonical")
    if invocation.get("kind") not in _INVOCATION_KINDS:
        raise ArtifactIntegrityError("manifest invocation kind is invalid")
    invocation_sequence = invocation.get("invocation_sequence")
    if type(invocation_sequence) is not int or invocation_sequence < 1:
        raise ArtifactIntegrityError("manifest invocation_sequence is invalid")
    attempt = invocation.get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise ArtifactIntegrityError("manifest invocation attempt is invalid")
    if invocation.get("status") not in _INVOCATION_STATUSES:
        raise ArtifactIntegrityError("manifest invocation status is invalid")
    return expected_base, invocation_id


def _validate_manifest_judges(
    manifest: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> int:
    judges = manifest.get("judges")
    if not isinstance(judges, list):
        raise ArtifactIntegrityError("manifest judges field is not a list")
    judge_rounds: list[int] = []
    for judge in judges:
        if not isinstance(judge, dict):
            raise ArtifactIntegrityError("manifest Judge entry is not an object")
        number = _strict_manifest_round(judge.get("round_number"))
        judge_rounds.append(number)
        expected_base = f"rounds/{number:03d}/judge"
        if judge.get("path") != expected_base:
            raise ArtifactIntegrityError("manifest Judge path is not canonical")
        _validate_index_artifacts(
            judge.get("artifacts"),
            artifacts,
            expected_base,
            {"decision.json", "meta.json", "raw.md", "request.md"},
        )
    if judge_rounds != list(range(1, len(judge_rounds) + 1)):
        raise ArtifactIntegrityError("Judge checkpoints must be ordered and contiguous")
    return len(judges)


def _validate_manifest_progress(
    manifest: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    judge_count: int,
) -> None:
    round_count = manifest.get("round_count", 0)
    if type(round_count) is not int or round_count < 0 or round_count > judge_count:
        raise ArtifactIntegrityError("manifest round_count exceeds valid Judge checkpoints")
    elapsed = manifest.get("elapsed_seconds", 0.0)
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ArtifactIntegrityError("manifest elapsed_seconds must be finite and non-negative")
    rounds = manifest.get("rounds")
    if not isinstance(rounds, list):
        raise ArtifactIntegrityError("manifest rounds field is not a list")
    round_numbers = [
        item.get("round_number") if isinstance(item, Mapping) else None for item in rounds
    ]
    if round_numbers != list(range(1, len(rounds) + 1)) or len(rounds) > judge_count:
        raise ArtifactIntegrityError(
            "manifest round summaries must be an ordered checkpoint prefix"
        )

    for pointer in ("failure_artifact", "final_artifact"):
        value = manifest.get(pointer)
        if value is None:
            continue
        canonical = _canonical_manifest_path(value)
        if canonical not in artifacts:
            raise ArtifactIntegrityError(f"manifest {pointer} is not integrity-indexed")


def _canonical_manifest_path(value: object) -> str:
    try:
        return _validate_relative_path(value).as_posix()
    except UnsafeArtifactPathError as exc:
        raise ArtifactIntegrityError(str(exc)) from exc


def _strict_manifest_round(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ArtifactIntegrityError("manifest round number must be a positive integer")
    return value


def _validate_file_record(record: object, path: str) -> None:
    if not isinstance(record, Mapping):
        raise ArtifactIntegrityError(f"artifact record is not an object: {path}")
    digest = record.get("content_sha256")
    alias = record.get("sha256")
    size = record.get("size_bytes")
    if (
        not isinstance(digest, str)
        or _DIGEST_RE.fullmatch(digest) is None
        or alias != digest
        or type(size) is not int
        or size < 0
    ):
        raise ArtifactIntegrityError(f"artifact record is invalid: {path}")


def _validate_index_artifacts(
    indexed: object,
    all_artifacts: Mapping[str, object],
    base: str,
    filenames: set[str],
) -> None:
    if not isinstance(indexed, Mapping):
        raise ArtifactIntegrityError("manifest index artifacts field is not an object")
    expected_paths = {f"{base}/{filename}" for filename in filenames}
    if set(indexed) != expected_paths:
        raise ArtifactIntegrityError("manifest index artifact set is incomplete or unexpected")
    for relative_text, record in indexed.items():
        canonical = _canonical_manifest_path(relative_text)
        if canonical not in all_artifacts or all_artifacts[canonical] != record:
            raise ArtifactIntegrityError("manifest index is not linked to verified artifacts")


class ArtifactStore:
    """Exclusive writer for one run directory."""

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = self.run_dir.name
        self._manifest: dict[str, Any] = {}
        self._guard = threading.RLock()
        self._lock_file: BinaryIO | None = None
        self._lock_kind: str | None = None
        self._run_dir_fd: int | None = None
        self._run_dir_stat: os.stat_result | None = None
        self._lock_stat: os.stat_result | None = None
        self._closed = False
        self._acquire_run_lock()

    @classmethod
    def create(
        cls,
        root: str | Path,
        config: object | None = None,
        request: object = "",
    ) -> ArtifactStore:
        """Create and exclusively lock a collision-safe run directory."""

        runs_root = Path(root)
        _make_private_dir(runs_root)
        run_dir: Path | None = None
        for _attempt in range(100):
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            candidate = runs_root / f"{timestamp}-{uuid.uuid4().hex}"
            try:
                candidate.mkdir(mode=DIRECTORY_MODE)
            except FileExistsError:
                continue
            _best_effort_chmod(candidate, DIRECTORY_MODE)
            run_dir = candidate
            break
        if run_dir is None:  # pragma: no cover - UUID failure is practically unreachable
            raise ArtifactError("could not allocate a unique run directory")

        store = cls(run_dir)
        try:
            store._initialize(config if config is not None else {}, request)
        except BaseException as exc:
            store._persist_initialization_failure(exc)
            store.close()
            raise
        return store

    @classmethod
    def load_existing(
        cls,
        run_dir: str | Path,
        *,
        verify: bool = True,
    ) -> ArtifactStore:
        """Open an existing run for resume and acquire its exclusive lock."""

        path = Path(run_dir)
        if path.is_symlink() or not path.is_dir():
            raise ResumeError(f"run directory does not exist or is unsafe: {path}")
        store = cls(path)
        try:
            manifest = store._read_validated_manifest()
            store.run_id = validate_path_id(manifest["run_id"], kind="run id")
            store._manifest = manifest
            if verify:
                store.verify_integrity(strict=True)
        except BaseException:
            store.close()
            raise
        return store

    def mark_resumed(self) -> dict[str, Any]:
        """Record an eligible resume after all non-mutating gates pass."""

        with self._guard:
            self._ensure_open()
            resume_time = utc_now()
            resume_count = int(self._manifest.get("resume_count", 0)) + 1
            self.update_manifest({"resumed_at": resume_time, "resume_count": resume_count})
            self.append_event("run_resumed", {"resume_count": resume_count})
            return self.manifest

    def read_artifact_bytes(self, relative: str | Path) -> bytes:
        """Read one contained regular artifact without following symlinks."""

        with self._guard:
            self._ensure_open()
            run_dir_fd = self._require_run_dir_fd()
            return _read_relative_bytes_at(
                run_dir_fd,
                _validate_relative_path(relative),
            )

    def read_artifact_text(self, relative: str | Path) -> str:
        """Read one contained artifact as strict UTF-8."""

        try:
            return self.read_artifact_bytes(relative).decode("utf-8")
        except UnicodeError as exc:
            raise ArtifactIntegrityError(f"artifact is not valid UTF-8: {relative}") from exc

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a defensive copy of the current manifest."""

        with self._guard:
            return copy.deepcopy(self._manifest)

    @property
    def closed(self) -> bool:
        """Whether the store has released its run lock."""

        return self._closed

    def read_manifest(self) -> dict[str, Any]:
        """Safely reload and verify the latest on-disk manifest."""

        with self._guard:
            self._ensure_open()
            value = self._read_validated_manifest()
            previous = self._manifest
            self._manifest = value
            try:
                self.verify_integrity(strict=True)
            except BaseException:
                self._manifest = previous
                raise
            return copy.deepcopy(self._manifest)

    def _read_validated_manifest(self) -> dict[str, Any]:
        try:
            raw = _read_relative_bytes_at(
                self._require_run_dir_fd(),
                Path("manifest.json"),
                max_bytes=_MAX_MANIFEST_BYTES,
            )
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError(
                f"cannot safely read manifest at {self.run_dir}: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise ArtifactIntegrityError(f"manifest at {self.run_dir} is not a JSON object")
        _validate_manifest_v1(manifest, self.run_dir)
        return manifest

    def update_manifest(
        self,
        fields: Mapping[str, object] | None = None,
        **updates: object,
    ) -> dict[str, Any]:
        """Atomically apply a shallow manifest update."""

        if fields is not None and not isinstance(fields, Mapping):
            raise TypeError("manifest fields must be a mapping")
        changes = dict(fields or {})
        overlap = changes.keys() & updates.keys()
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"duplicate manifest fields: {names}")
        changes.update(updates)

        def mutate(manifest: dict[str, Any]) -> None:
            if "run_id" in changes and changes["run_id"] != self.run_id:
                raise ArtifactIntegrityError("run_id is immutable")
            manifest.update(_jsonable(changes))
            manifest["updated_at"] = utc_now()
            status = _enum_value(manifest.get("status"))
            manifest["status"] = status
            if status in _TERMINAL_STATUSES and not manifest.get("finished_at"):
                manifest["finished_at"] = manifest["updated_at"]

        return self._mutate_manifest(mutate)

    def append_event(
        self,
        event_type: str | None = None,
        payload: object | None = None,
        **aliases: object,
    ) -> dict[str, Any]:
        """Append one durable JSON object to ``events.jsonl``."""

        if event_type is None:
            alias_type = aliases.pop("type", None)
            if alias_type is not None and not isinstance(alias_type, str):
                raise ValueError("event type must be a string")
            event_type = alias_type
        if aliases:
            names = ", ".join(sorted(aliases))
            raise TypeError(f"unexpected event arguments: {names}")
        if (
            not isinstance(event_type, str)
            or not event_type
            or len(event_type) > _MAX_EVENT_TYPE_LENGTH
        ):
            raise ValueError("event type must be a non-empty string of at most 128 characters")
        if "\n" in event_type or "\r" in event_type:
            raise ValueError("event type cannot contain newlines")

        with self._guard:
            self._ensure_open()
            sequence = int(self._manifest.get("event_count", 0)) + 1
            unsigned_event = {
                "event_id": uuid.uuid4().hex,
                "sequence": sequence,
                "timestamp": utc_now(),
                "type": event_type,
                "payload": _jsonable(payload if payload is not None else {}),
            }
            canonical = json.dumps(
                unsigned_event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            event = {
                **unsigned_event,
                "content_sha256": content_sha256(canonical),
            }
            encoded = (
                json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode("utf-8")
            run_dir_fd = self._require_run_dir_fd()
            _append_relative_bytes(run_dir_fd, Path("events.jsonl"), encoded)
            record = _file_record(_read_relative_bytes_at(run_dir_fd, Path("events.jsonl")))

            def mutate(manifest: dict[str, Any]) -> None:
                artifacts = _artifacts_dict(manifest)
                artifacts["events.jsonl"] = record
                manifest["event_count"] = sequence
                manifest["last_event_at"] = event["timestamp"]

            self._mutate_manifest(mutate)
            return copy.deepcopy(event)

    def write_invocation(
        self,
        round_number: int | str | None = None,
        stage: str | None = None,
        role: str | None = None,
        prompt: object = "",
        result: object | None = None,
        *,
        kind: Literal["participant", "judge_attempt"] = "participant",
        attempt: int = 1,
        elapsed_seconds: float | None = None,
        **aliases: object,
    ) -> dict[str, Any]:
        """Persist one participant invocation and return its artifact index."""

        round_number, role = _resolve_invocation_aliases(round_number, role, aliases)
        number = _validate_round_number(round_number)
        stage_id = validate_path_id(stage, kind="stage")
        participant_id = validate_path_id(role, kind="participant")
        if kind not in {"participant", "judge_attempt"}:
            raise ArtifactError(f"unsupported invocation kind: {kind!r}")
        if type(attempt) is not int or attempt < 1:
            raise ArtifactError("invocation attempt must be a positive integer")
        if elapsed_seconds is not None and (
            isinstance(elapsed_seconds, bool)
            or not isinstance(elapsed_seconds, (int, float))
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
        ):
            raise ArtifactError("elapsed_seconds must be finite and non-negative")
        with self._guard:
            self._ensure_open()
            invocation_sequence = len(_list_field(self._manifest, "invocations")) + 1
            return self._write_invocation_locked(
                number=number,
                stage_id=stage_id,
                participant_id=participant_id,
                prompt=prompt,
                result=result,
                kind=kind,
                attempt=attempt,
                invocation_sequence=invocation_sequence,
                elapsed_seconds=elapsed_seconds,
            )

    def _write_invocation_locked(
        self,
        *,
        number: int,
        stage_id: str,
        participant_id: str,
        prompt: object,
        result: object | None,
        kind: Literal["participant", "judge_attempt"],
        attempt: int,
        invocation_sequence: int,
        elapsed_seconds: float | None,
    ) -> dict[str, Any]:
        """Write one invocation while the store guard reserves its sequence."""

        invocation_id = uuid.uuid4().hex
        base = Path("rounds") / f"{number:03d}" / stage_id / participant_id / invocation_id
        stdout, stderr, final, result_meta = _result_parts(result)
        status = str(result_meta.get("status", "success" if final else "failed"))
        if status not in _INVOCATION_STATUSES:
            raise ArtifactError(f"unsupported invocation status: {status!r}")
        recorded_at = utc_now()
        meta = {
            **result_meta,
            "round_number": number,
            "stage": stage_id,
            "participant": participant_id,
            "invocation_id": invocation_id,
            "invocation_sequence": invocation_sequence,
            "kind": kind,
            "attempt": attempt,
            "status": status,
            "recorded_at": recorded_at,
        }
        files = {
            base / "request.md": _as_text(prompt).encode("utf-8"),
            base / "stdout.log": stdout.encode("utf-8"),
            base / "stderr.log": stderr.encode("utf-8"),
            base / "final.md": final.encode("utf-8"),
            base / "meta.json": _json_bytes(meta),
        }
        records = self._write_artifact_batch(files)
        invocation = {
            "round_number": number,
            "stage": stage_id,
            "participant": participant_id,
            "invocation_id": invocation_id,
            "invocation_sequence": invocation_sequence,
            "kind": kind,
            "attempt": attempt,
            "status": status,
            "path": base.as_posix(),
            "recorded_at": recorded_at,
            "artifacts": records,
        }

        def mutate(manifest: dict[str, Any]) -> None:
            _artifacts_dict(manifest).update(records)
            invocations = _list_field(manifest, "invocations")
            invocations.append(invocation)
            if elapsed_seconds is not None:
                manifest["elapsed_seconds"] = elapsed_seconds

        self._mutate_manifest(mutate)
        self.append_event(
            "invocation_written",
            {
                "round_number": number,
                "stage": stage_id,
                "participant": participant_id,
                "invocation_id": invocation_id,
                "invocation_sequence": invocation_sequence,
                "kind": kind,
                "attempt": attempt,
                "status": status,
                "path": base.as_posix(),
            },
        )
        return copy.deepcopy(invocation)

    def write_judge(
        self,
        round_number: int | str | None = None,
        prompt: object = "",
        raw: object = "",
        decision: object | None = None,
        result: object | None = None,
        **aliases: object,
    ) -> dict[str, Any]:
        """Persist the judge prompt, raw output, parsed decision, and metadata."""

        if round_number is None:
            alias_round = aliases.pop("round", None)
            if alias_round is not None and not isinstance(alias_round, (int, str)):
                raise UnsafeArtifactPathError("round number must be a positive integer")
            round_number = alias_round
        if aliases:
            names = ", ".join(sorted(aliases))
            raise TypeError(f"unexpected judge arguments: {names}")
        number = _validate_round_number(round_number)
        if decision is None:
            raise ArtifactError("only a validated Judge decision can create a checkpoint")
        base = Path("rounds") / f"{number:03d}" / "judge"
        _stdout, stderr, _final, result_meta = _result_parts(result)
        recorded_at = utc_now()
        meta = {
            **result_meta,
            "round_number": number,
            "recorded_at": recorded_at,
        }
        if stderr:
            meta.setdefault("stderr", stderr)
        files = {
            base / "request.md": _as_text(prompt).encode("utf-8"),
            base / "raw.md": _as_text(raw).encode("utf-8"),
            base / "decision.json": _json_bytes(decision),
            base / "meta.json": _json_bytes(meta),
        }
        records = self._write_artifact_batch(files)
        judge = {
            "round_number": number,
            "path": base.as_posix(),
            "recorded_at": recorded_at,
            "artifacts": records,
        }

        def mutate(manifest: dict[str, Any]) -> None:
            _artifacts_dict(manifest).update(records)
            judges = _list_field(manifest, "judges")
            _replace_index_entry(judges, judge, key="round_number")

        self._mutate_manifest(mutate)
        self.append_event("judge_written", {"round_number": number, "path": base.as_posix()})
        return copy.deepcopy(judge)

    def write_final(self, text: object) -> dict[str, Any]:
        """Persist the final synthesis."""

        final_text = _as_text(text)
        records = self._write_artifact_batch({Path("final.md"): final_text.encode("utf-8")})

        def mutate(manifest: dict[str, Any]) -> None:
            _artifacts_dict(manifest).update(records)
            manifest["final_artifact"] = "final.md"
            manifest["final_synthesis"] = final_text

        self._mutate_manifest(mutate)
        self.append_event("final_written", {"path": "final.md"})
        return copy.deepcopy(records["final.md"])

    def write_evidence(self, text: object) -> dict[str, Any]:
        """Persist the complete reader-facing evidence transcript."""

        evidence_text = _as_text(text)
        records = self._write_artifact_batch({Path("evidence.md"): evidence_text.encode("utf-8")})

        def mutate(manifest: dict[str, Any]) -> None:
            _artifacts_dict(manifest).update(records)
            manifest["evidence_artifact"] = "evidence.md"

        self._mutate_manifest(mutate)
        self.append_event("evidence_written", {"path": "evidence.md"})
        return copy.deepcopy(records["evidence.md"])

    def write_result(self, document: Mapping[str, object]) -> dict[str, Any]:
        """Persist the canonical machine-readable result document."""

        records = self._write_artifact_batch({Path("result.json"): _json_bytes(document)})

        def mutate(manifest: dict[str, Any]) -> None:
            _artifacts_dict(manifest).update(records)
            manifest["result_artifact"] = "result.json"

        self._mutate_manifest(mutate)
        self.append_event("result_written", {"path": "result.json"})
        return copy.deepcopy(records["result.json"])

    def record_failure(
        self,
        error: BaseException | str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Persist an error artifact and mark the run failed."""

        self._ensure_open()
        timestamp = utc_now()
        error_payload: dict[str, Any] = {
            "type": type(error).__name__ if isinstance(error, BaseException) else "Error",
            "message": str(error),
            "timestamp": timestamp,
        }
        if details:
            error_payload["details"] = _jsonable(details)
        name_time = timestamp.replace("-", "").replace(":", "").replace(".", "")
        relative = Path("failures") / f"{name_time}-{uuid.uuid4().hex}.json"
        records = self._write_artifact_batch({relative: _json_bytes(error_payload)})

        def mutate(manifest: dict[str, Any]) -> None:
            _artifacts_dict(manifest).update(records)
            manifest["status"] = "failed"
            manifest["error"] = str(error)
            manifest["error_details"] = error_payload
            manifest["failure_artifact"] = relative.as_posix()
            manifest["finished_at"] = timestamp

        self._mutate_manifest(mutate)
        self.append_event(
            "run_failed",
            {"error": error_payload, "failure_artifact": relative.as_posix()},
        )
        return copy.deepcopy(error_payload)

    mark_failed = record_failure

    def mark_completed(self, final: object | None = None) -> dict[str, Any]:
        """Optionally write a final synthesis and mark the run completed."""

        if final is not None:
            self.write_final(final)
        self.append_event("run_completed", {})
        return self.update_manifest(status="completed", error=None)

    def verify_integrity(self, *, strict: bool = False) -> dict[str, bool]:
        """Check all manifest-indexed artifacts against their content hashes."""

        with self._guard:
            self._ensure_open()
            results: dict[str, bool] = {}
            artifacts = self._manifest.get("artifacts", {})
            if not isinstance(artifacts, Mapping):
                raise ArtifactIntegrityError("manifest artifacts field is not an object")
            for relative_text, metadata in artifacts.items():
                try:
                    relative = _validate_relative_path(relative_text)
                    expected = _expected_digest(metadata)
                    expected_size = _expected_size(metadata)
                    content = self.read_artifact_bytes(relative)
                    results[str(relative_text)] = bool(
                        expected
                        and expected_size == len(content)
                        and content_sha256(content) == expected
                    )
                except (OSError, ArtifactError, UnsafeArtifactPathError):
                    results[str(relative_text)] = False
            invalid = [path for path, valid in results.items() if not valid]
            if strict and invalid:
                names = ", ".join(sorted(invalid))
                raise ArtifactIntegrityError(f"artifact integrity check failed: {names}")
            if strict:
                self._verify_manifest_semantics()
            return results

    def _verify_manifest_semantics(self) -> None:
        """Cross-check mutable indexes against their verified artifact content."""

        for invocation in self._manifest["invocations"]:
            relative = f"{invocation['path']}/meta.json"
            metadata = self._read_json_object(relative)
            for field in (
                "attempt",
                "invocation_id",
                "invocation_sequence",
                "kind",
                "participant",
                "recorded_at",
                "round_number",
                "stage",
                "status",
            ):
                if metadata.get(field) != invocation.get(field):
                    raise ArtifactIntegrityError(
                        f"invocation index disagrees with verified metadata: {relative} ({field})"
                    )

        for judge in self._manifest["judges"]:
            relative = f"{judge['path']}/meta.json"
            metadata = self._read_json_object(relative)
            if metadata.get("round_number") != judge.get("round_number"):
                raise ArtifactIntegrityError(
                    f"Judge index disagrees with verified metadata: {relative}"
                )
        self._verify_event_log()

    def _read_json_object(self, relative: str) -> dict[str, Any]:
        try:
            value = json.loads(self.read_artifact_text(relative))
        except (ArtifactError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError(
                f"verified artifact is not valid JSON: {relative}"
            ) from exc
        if not isinstance(value, dict):
            raise ArtifactIntegrityError(f"verified artifact is not a JSON object: {relative}")
        return value

    def _verify_event_log(self) -> None:
        try:
            text = self.read_artifact_text("events.jsonl")
        except ArtifactError as exc:
            raise ArtifactIntegrityError("cannot read verified event log") from exc
        lines = text.splitlines()
        if len(lines) != self._manifest["event_count"]:
            raise ArtifactIntegrityError("event_count does not match verified event log")
        for sequence, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ArtifactIntegrityError("event log contains invalid JSON") from exc
            if not isinstance(event, dict) or event.get("sequence") != sequence:
                raise ArtifactIntegrityError("event log sequence is not contiguous")
            recorded_digest = event.pop("content_sha256", None)
            canonical = json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if recorded_digest != content_sha256(canonical):
                raise ArtifactIntegrityError("event log entry digest is invalid")

    def close(self) -> None:
        """Release the run lock.  Calling this more than once is safe."""

        with self._guard:
            if self._closed:
                return
            lock_file = self._lock_file
            try:
                if lock_file is not None:
                    with suppress(OSError):
                        self._write_lock_metadata(lock_file, released_at=utc_now())
                    self._release_run_lock(lock_file)
            finally:
                if lock_file is not None:
                    lock_file.close()
                run_dir_fd = getattr(self, "_run_dir_fd", None)
                if run_dir_fd is not None:
                    with suppress(OSError):
                        os.close(run_dir_fd)
                    self._run_dir_fd = None
                    self._run_dir_stat = None
                self._lock_file = None
                self._closed = True

    def __enter__(self) -> ArtifactStore:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        del exception_type, traceback
        if exception is not None:
            with suppress(Exception):
                self.record_failure(exception)
        self.close()
        return False

    def _initialize(self, config: object, request: object) -> None:
        config_bytes = _yaml_bytes(redact_config(config))
        request_bytes = _as_text_or_json(request).encode("utf-8")
        config_record = self._write_artifact_bytes(Path("config.resolved.yaml"), config_bytes)
        request_record = self._write_artifact_bytes(Path("request.md"), request_bytes)
        events_record = self._write_artifact_bytes(Path("events.jsonl"), b"")
        timestamp = utc_now()
        self._manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": "running",
            "created_at": timestamp,
            "started_at": timestamp,
            "updated_at": timestamp,
            "finished_at": None,
            "resumed_at": None,
            "resume_count": 0,
            "rounds": [],
            "final_synthesis": None,
            "error": None,
            "config_snapshot": "config.resolved.yaml",
            "request_artifact": "request.md",
            "events_artifact": "events.jsonl",
            "event_count": 0,
            "invocations": [],
            "judges": [],
            "artifacts": {
                "config.resolved.yaml": config_record,
                "request.md": request_record,
                "events.jsonl": events_record,
            },
        }
        self._write_manifest_locked(self._manifest)
        self.append_event("run_created", {"run_id": self.run_id})

    def _persist_initialization_failure(self, error: BaseException) -> None:
        try:
            if not self._manifest:
                timestamp = utc_now()
                self._manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": self.run_id,
                    "status": "failed",
                    "created_at": timestamp,
                    "started_at": timestamp,
                    "updated_at": timestamp,
                    "finished_at": timestamp,
                    "rounds": [],
                    "final_synthesis": None,
                    "error": str(error),
                    "artifacts": {},
                    "event_count": 0,
                }
                self._write_manifest_locked(self._manifest)
            else:
                self.record_failure(error)
        except Exception:
            pass

    def _write_artifact_batch(self, files: Mapping[Path, bytes]) -> dict[str, dict[str, Any]]:
        with self._guard:
            self._ensure_open()
            return {
                relative.as_posix(): self._write_artifact_bytes(relative, data)
                for relative, data in files.items()
            }

    def _write_artifact_bytes(self, relative: Path, data: bytes) -> dict[str, Any]:
        relative = _validate_relative_path(relative)
        self._ensure_open()
        _atomic_write_relative_bytes(self._require_run_dir_fd(), relative, data)
        return _file_record(data)

    def _mutate_manifest(
        self,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        with self._guard:
            self._ensure_open()
            updated = copy.deepcopy(self._manifest)
            mutator(updated)
            updated["updated_at"] = utc_now()
            self._write_manifest_locked(updated)
            self._manifest = updated
            return copy.deepcopy(updated)

    def _write_manifest_locked(self, manifest: Mapping[str, object]) -> None:
        self._ensure_open()
        _atomic_write_relative_bytes(
            self._require_run_dir_fd(),
            Path("manifest.json"),
            _json_bytes(manifest),
        )

    def _require_run_dir_fd(self) -> int:
        run_dir_fd = self._run_dir_fd
        if run_dir_fd is None:
            raise RunLockedError("run directory descriptor is unavailable")
        return run_dir_fd

    def _ensure_open(self) -> None:
        if self._closed or self._lock_file is None:
            raise ArtifactError("artifact store is closed")
        lock_stat = getattr(self, "_lock_stat", None)
        run_dir_fd = getattr(self, "_run_dir_fd", None)
        run_dir_stat = getattr(self, "_run_dir_stat", None)
        if lock_stat is None or run_dir_fd is None or run_dir_stat is None:
            raise RunLockedError("run lock state is incomplete")
        try:
            named_run_dir = os.lstat(self.run_dir)
            opened_run_dir = os.fstat(run_dir_fd)
        except OSError as exc:
            raise RunLockedError("run directory pathname changed while the store was open") from exc
        expected_identity = (run_dir_stat.st_dev, run_dir_stat.st_ino)
        if (
            not stat.S_ISDIR(named_run_dir.st_mode)
            or not stat.S_ISDIR(opened_run_dir.st_mode)
            or (named_run_dir.st_dev, named_run_dir.st_ino) != expected_identity
            or (opened_run_dir.st_dev, opened_run_dir.st_ino) != expected_identity
        ):
            raise RunLockedError("run directory pathname changed while the store was open")
        try:
            current = os.stat("run.lock", dir_fd=run_dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise RunLockedError("run lock pathname changed while the store was open") from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (lock_stat.st_dev, lock_stat.st_ino)
        ):
            raise RunLockedError("run lock pathname changed while the store was open")

    def _acquire_run_lock(self) -> None:
        validate_path_id(self.run_id, kind="run id")
        lock_path = self.run_dir / "run.lock"
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        run_dir_fd: int | None = None
        try:
            run_dir_fd = os.open(self.run_dir, directory_flags)
            descriptor = os.open(
                "run.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                FILE_MODE,
                dir_fd=run_dir_fd,
            )
        except OSError as exc:
            if run_dir_fd is not None:
                with suppress(OSError):
                    os.close(run_dir_fd)
            raise RunLockedError(f"cannot safely open run lock: {lock_path}") from exc
        opened = os.fstat(descriptor)
        opened_run_dir = os.fstat(run_dir_fd)
        try:
            named = os.stat("run.lock", dir_fd=run_dir_fd, follow_symlinks=False)
        except OSError:
            os.close(descriptor)
            os.close(run_dir_fd)
            raise
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            os.close(descriptor)
            os.close(run_dir_fd)
            raise RunLockedError(f"run lock must be one regular, single-link file: {lock_path}")
        assert run_dir_fd is not None  # narrowed after the guarded open above
        self._run_dir_fd = run_dir_fd
        self._run_dir_stat = opened_run_dir
        self._lock_stat = opened
        with suppress(OSError):
            os.fchmod(descriptor, FILE_MODE)
        lock_file = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            self._lock_kind = _lock_nonblocking(lock_file, lock_path)
        except BaseException:
            lock_file.close()
            os.close(run_dir_fd)
            self._run_dir_fd = None
            raise
        self._lock_file = lock_file
        self._write_lock_metadata(lock_file, acquired_at=utc_now())

    def _release_run_lock(self, lock_file: BinaryIO) -> None:
        if self._lock_kind == "fcntl":
            _FCNTL.flock(lock_file.fileno(), _FCNTL.LOCK_UN)
        elif self._lock_kind == "msvcrt":  # pragma: no cover - Windows only
            lock_file.seek(0)
            _MSVCRT.locking(lock_file.fileno(), _MSVCRT.LK_UNLCK, 1)
        elif self._lock_kind == "fallback":  # pragma: no cover - uncommon platform
            with _FALLBACK_LOCKS_GUARD:
                _FALLBACK_LOCKS.discard(str((self.run_dir / "run.lock").resolve()))

    def _write_lock_metadata(self, lock_file: BinaryIO, **timestamps: str) -> None:
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "run_id": self.run_id,
            **timestamps,
        }
        encoded = _json_bytes(metadata)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(encoded)
        lock_file.flush()
        os.fsync(lock_file.fileno())


RunArtifacts = ArtifactStore
RunArtifactStore = ArtifactStore


def create(
    root: str | Path,
    config: object | None = None,
    request: object = "",
) -> ArtifactStore:
    """Module-level convenience wrapper for :meth:`ArtifactStore.create`."""

    return ArtifactStore.create(root, config=config, request=request)


def load_existing(run_dir: str | Path, *, verify: bool = True) -> ArtifactStore:
    """Module-level convenience wrapper for :meth:`ArtifactStore.load_existing`."""

    return ArtifactStore.load_existing(run_dir, verify=verify)


def _lock_nonblocking(lock_file: BinaryIO, lock_path: Path) -> str:
    if os.name == "nt":  # pragma: no cover - Windows only
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            _MSVCRT.locking(lock_file.fileno(), _MSVCRT.LK_NBLCK, 1)
        except OSError as exc:
            raise RunLockedError(f"run is already open: {lock_path.parent}") from exc
        return "msvcrt"

    if _FCNTL is None:  # pragma: no cover - uncommon platform
        resolved = str(lock_path.resolve())
        with _FALLBACK_LOCKS_GUARD:
            if resolved in _FALLBACK_LOCKS:
                raise RunLockedError(f"run is already open: {lock_path.parent}")
            _FALLBACK_LOCKS.add(resolved)
        return "fallback"

    try:
        _FCNTL.flock(lock_file.fileno(), _FCNTL.LOCK_EX | _FCNTL.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        raise RunLockedError(f"run is already open: {lock_path.parent}") from exc
    return "fcntl"


def _resolve_invocation_aliases(
    round_number: int | str | None,
    role: str | None,
    aliases: dict[str, object],
) -> tuple[int | str | None, str | None]:
    if round_number is None:
        alias_round = aliases.pop("round", None)
        if alias_round is not None and not isinstance(alias_round, (int, str)):
            raise UnsafeArtifactPathError("round number must be a positive integer")
        round_number = alias_round
    participant = aliases.pop("participant", None)
    if role is None and participant is not None:
        if not isinstance(participant, str):
            raise TypeError("participant must be a string")
        role = participant
    if aliases:
        names = ", ".join(sorted(aliases))
        raise TypeError(f"unexpected invocation arguments: {names}")
    return round_number, role


def _validate_round_number(value: object) -> int:
    if isinstance(value, bool):
        raise UnsafeArtifactPathError("round number must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdecimal():
        number = int(value)
    else:
        raise UnsafeArtifactPathError("round number must be a positive integer")
    if number < 1 or str(number) != str(value):
        raise UnsafeArtifactPathError("round number must be a canonical positive integer")
    return number


def _validate_relative_path(value: object) -> Path:
    path = value if isinstance(value, Path) else Path(str(value))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeArtifactPathError(f"unsafe artifact path: {value!r}")
    for part in path.parts:
        validate_path_id(part, kind="artifact path component")
    return path


def _read_relative_bytes(
    root: Path,
    relative: Path,
    *,
    max_bytes: int = _MAX_ARTIFACT_READ_BYTES,
) -> bytes:
    """Open every path component relative to directory FDs with no-follow."""

    directory_flags = _directory_open_flags()
    root_fd: int | None = None
    try:
        root_fd = os.open(root, directory_flags)
        return _read_relative_bytes_at(root_fd, relative, max_bytes=max_bytes)
    finally:
        if root_fd is not None:
            with suppress(OSError):
                os.close(root_fd)


def _read_relative_bytes_at(
    root_fd: int,
    relative: Path,
    *,
    max_bytes: int = _MAX_ARTIFACT_READ_BYTES,
) -> bytes:
    """Read a single-link regular file relative to one stable root FD."""

    relative = _validate_relative_path(relative)
    parent_fd = _open_relative_parent(root_fd, relative.parent, create=False)
    file_descriptor: int | None = None
    try:
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(relative.name, file_flags, dir_fd=parent_fd)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeArtifactPathError(
                f"artifact must be a regular single-link file: {relative}"
            )
        if metadata.st_size > max_bytes:
            raise ArtifactIntegrityError(
                f"artifact exceeds the {max_bytes} byte read ceiling: {relative}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(file_descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)
        with suppress(OSError):
            os.close(parent_fd)


def _atomic_write_relative_bytes(root_fd: int, relative: Path, data: bytes) -> None:
    """Atomically replace one file beneath a stable directory descriptor."""

    relative = _validate_relative_path(relative)
    parent_fd = _open_relative_parent(root_fd, relative.parent, create=True)
    temporary = f".{relative.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            FILE_MODE,
            dir_fd=parent_fd,
        )
        _write_all(descriptor, data)
        os.fsync(descriptor)
        with suppress(NotImplementedError, OSError):
            os.fchmod(descriptor, FILE_MODE)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            relative.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(FileNotFoundError, OSError):
            os.unlink(temporary, dir_fd=parent_fd)
        with suppress(OSError):
            os.close(parent_fd)


def _append_relative_bytes(root_fd: int, relative: Path, data: bytes) -> None:
    """Durably append beneath a stable root without following path links."""

    relative = _validate_relative_path(relative)
    parent_fd = _open_relative_parent(root_fd, relative.parent, create=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            relative.name,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            FILE_MODE,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeArtifactPathError(f"append target is unsafe: {relative}")
        _write_all(descriptor, data)
        os.fsync(descriptor)
        with suppress(NotImplementedError, OSError):
            os.fchmod(descriptor, FILE_MODE)
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            os.close(parent_fd)


def _open_relative_parent(root_fd: int, parent: Path, *, create: bool) -> int:
    """Return a no-follow FD for a relative directory, optionally creating it."""

    current = os.dup(root_fd)
    try:
        for part in parent.parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, DIRECTORY_MODE, dir_fd=current)
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                _require_directory_descriptor(next_fd, parent)
            except BaseException:
                os.close(next_fd)
                raise
            with suppress(NotImplementedError, OSError):
                os.fchmod(next_fd, DIRECTORY_MODE)
            os.close(current)
            current = next_fd
    except BaseException:
        with suppress(OSError):
            os.close(current)
        raise
    else:
        return current


def _require_directory_descriptor(descriptor: int, path: Path) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise UnsafeArtifactPathError(f"artifact parent is not a directory: {path}")


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - defensive OS guard
            raise OSError("write made no progress")
        view = view[written:]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = REDACTED if _is_sensitive_key(key_text) else _redact(item)
        return result
    if isinstance(value, list):
        redacted: list[Any] = []
        redact_next = False
        for item in value:
            if redact_next:
                redacted.append(REDACTED)
                redact_next = False
                continue
            if isinstance(item, str):
                option, separator, _option_value = item.partition("=")
                if option.startswith("-") and _is_sensitive_key(option.lstrip("-")):
                    redacted.append(f"{option}={REDACTED}" if separator else item)
                    redact_next = not separator
                    continue
            redacted.append(_redact(item))
        return redacted
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _jsonable(value: object) -> Any:  # noqa: PLR0911 - explicit conversion table is clearer
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json"))
        except TypeError:
            return _jsonable(model_dump())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(key): _jsonable(item)
            for key, item in attributes.items()
            if not str(key).startswith("_")
        }
    return str(value)


def _enum_value(value: object) -> Any:
    return _jsonable(value.value if isinstance(value, Enum) else value)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _yaml_bytes(value: object) -> bytes:
    serialized = str(
        yaml.safe_dump(
            _jsonable(value),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=True,
        )
    )
    return serialized.encode("utf-8")


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)
    return str(value)


def _as_text_or_json(value: object) -> str:
    if isinstance(value, (str, bytes)):
        return _as_text(value)
    return json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _object_mapping(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str | bytes):
        return {"stdout": _as_text(value), "final": _as_text(value)}
    converted = _jsonable(value)
    if isinstance(converted, dict):
        return converted
    return {"value": converted}


def _result_parts(result: object | None) -> tuple[str, str, str, dict[str, Any]]:
    mapping = _object_mapping(result)
    stdout = _as_text(mapping.get("stdout", mapping.get("raw_output", "")))
    stderr = _as_text(mapping.get("stderr", ""))
    final_value = mapping.get(
        "final",
        mapping.get(
            "final_output",
            mapping.get("output", mapping.get("text", stdout)),
        ),
    )
    final = _as_text(final_value)
    meta = {key: item for key, item in mapping.items() if key not in _RESULT_CONTENT_KEYS}
    return stdout, stderr, final, meta


def _file_record(data: bytes) -> dict[str, Any]:
    digest = content_sha256(data)
    return {
        "content_sha256": digest,
        "sha256": digest,
        "size_bytes": len(data),
    }


def _expected_digest(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("content_sha256", metadata.get("sha256"))
    return value if isinstance(value, str) else None


def _expected_size(metadata: object) -> int | None:
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("size_bytes")
    return value if type(value) is int else None


def _artifacts_dict(manifest: dict[str, Any]) -> dict[str, Any]:
    value = manifest.setdefault("artifacts", {})
    if not isinstance(value, dict):
        raise ArtifactIntegrityError("manifest artifacts field is not an object")
    return value


def _list_field(manifest: dict[str, Any], name: str) -> list[Any]:
    value = manifest.setdefault(name, [])
    if not isinstance(value, list):
        raise ArtifactIntegrityError(f"manifest {name} field is not a list")
    return value


def _replace_index_entry(entries: list[Any], new_entry: dict[str, Any], *, key: str) -> None:
    for index, existing in enumerate(entries):
        if isinstance(existing, Mapping) and existing.get(key) == new_entry[key]:
            entries[index] = new_entry
            return
    entries.append(new_entry)


def _make_private_dir(path: Path) -> None:
    path.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    if path.is_symlink():
        raise UnsafeArtifactPathError(f"directory cannot be a symlink: {path}")
    _best_effort_chmod(path, DIRECTORY_MODE)


def _best_effort_chmod(path: Path, mode: int) -> None:
    with suppress(NotImplementedError, OSError):
        path.chmod(mode)


def _best_effort_fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except (NotImplementedError, OSError):  # pragma: no cover - platform dependent
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


__all__ = [
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactStore",
    "RunArtifactStore",
    "RunArtifacts",
    "RunLockedError",
    "UnsafeArtifactPathError",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "content_sha256",
    "create",
    "load_existing",
    "read_manifest",
    "redact_config",
    "sha256_file",
    "utc_now",
    "validate_path_id",
]
