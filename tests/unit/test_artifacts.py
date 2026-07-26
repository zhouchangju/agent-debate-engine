from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from agent_debate import artifacts
from agent_debate.artifacts import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactStore,
    RunLockedError,
    UnsafeArtifactPathError,
    content_sha256,
    load_existing,
    read_manifest,
    validate_path_id,
)


def test_concurrent_run_creation_is_unique(tmp_path: Path) -> None:
    def create_one(index: int) -> Path:
        store = ArtifactStore.create(tmp_path, {"index": index}, f"request {index}")
        try:
            return store.run_dir
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=12) as executor:
        run_dirs = list(executor.map(create_one, range(32)))

    assert len(set(run_dirs)) == 32
    assert all(path.is_dir() for path in run_dirs)
    assert all(path.name.endswith(tuple("0123456789abcdef")) for path in run_dirs)


def test_create_redacts_config_and_initializes_manifest(tmp_path: Path) -> None:
    with ArtifactStore.create(
        tmp_path,
        {
            "api_key": "do-not-save",
            "command": ["agent", "--access-token", "token-value", "--secret=secret-value"],
            "nested": {"AUTHORIZATION": "Bearer secret", "token_budget": 4096},
            "normal": "visible",
        },
        "please debate this",
    ) as store:
        snapshot = yaml.safe_load(
            (store.run_dir / "config.resolved.yaml").read_text(encoding="utf-8")
        )
        manifest = store.manifest

        assert snapshot == {
            "api_key": "[REDACTED]",
            "command": [
                "agent",
                "--access-token",
                "[REDACTED]",
                "--secret=[REDACTED]",
            ],
            "nested": {"AUTHORIZATION": "[REDACTED]", "token_budget": 4096},
            "normal": "visible",
        }
        assert (store.run_dir / "request.md").read_text(encoding="utf-8") == ("please debate this")
        assert manifest["run_id"] == store.run_dir.name
        assert manifest["status"] == "running"
        assert manifest["event_count"] == 1
        assert manifest["artifacts"]["config.resolved.yaml"]["content_sha256"] == content_sha256(
            (store.run_dir / "config.resolved.yaml").read_bytes()
        )


def test_redaction_preserves_sensitive_looking_agent_ids(tmp_path: Path) -> None:
    with ArtifactStore.create(
        tmp_path,
        {
            "agents": {
                "token": {
                    "adapter": "codex",
                    "command": ["codex"],
                    "api_key": "secret",
                }
            }
        },
        "request",
    ) as store:
        snapshot = yaml.safe_load(store.read_artifact_text("config.resolved.yaml"))

    assert snapshot["agents"]["token"]["adapter"] == "codex"
    assert snapshot["agents"]["token"]["api_key"] == "[REDACTED]"


def test_read_manifest_wraps_invalid_utf8(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_bytes(b"\xff\xfe")

    with pytest.raises(ArtifactIntegrityError, match="cannot read manifest"):
        read_manifest(run_dir)


def test_atomic_writes_use_replace_and_private_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def tracking_replace(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        **kwargs: int,
    ) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr(artifacts.os, "replace", tracking_replace)
    store = ArtifactStore.create(tmp_path, {}, "request")
    try:
        store.update_manifest(status="judging")
        store.write_final("answer")
        run_dir = store.run_dir

        assert replacements
        assert all(source.parent == destination.parent for source, destination in replacements)
        assert not list(run_dir.rglob("*.tmp"))
        if os.name != "nt":
            assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
            for directory in (path for path in run_dir.rglob("*") if path.is_dir()):
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            for file_path in (path for path in run_dir.rglob("*") if path.is_file()):
                assert stat.S_IMODE(file_path.stat().st_mode) == 0o600
    finally:
        store.close()


def test_lock_rejects_double_open_and_allows_resume_after_close(tmp_path: Path) -> None:
    first = ArtifactStore.create(tmp_path, {}, "request")
    run_dir = first.run_dir
    try:
        with pytest.raises(RunLockedError):
            load_existing(run_dir)
    finally:
        first.close()

    resumed = load_existing(run_dir)
    try:
        assert resumed.manifest["resume_count"] == 0
        resumed.mark_resumed()
        assert resumed.manifest["resume_count"] == 1
        event_types = [
            json.loads(line)["type"]
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert event_types == ["run_created", "run_resumed"]
    finally:
        resumed.close()


def test_invocation_judge_final_and_events_are_auditable(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        invocation = store.write_invocation(
            round=1,
            stage="critique",
            participant="codex",
            prompt="participant prompt",
            result={
                "stdout": "stdout",
                "stderr": "stderr",
                "final": "participant answer",
                "returncode": 0,
                "duration_ms": 12,
            },
        )
        judge = store.write_judge(
            round=1,
            prompt="judge prompt",
            raw='{"winner":"codex"}',
            decision={"winner": "codex"},
            result={"returncode": 0},
        )
        final_record = store.write_final("synthesis")

        invocation_dir = store.run_dir / invocation["path"]
        judge_dir = store.run_dir / judge["path"]
        assert (invocation_dir / "request.md").read_text(encoding="utf-8") == ("participant prompt")
        assert (invocation_dir / "stdout.log").read_text(encoding="utf-8") == "stdout"
        assert (invocation_dir / "stderr.log").read_text(encoding="utf-8") == "stderr"
        assert (invocation_dir / "final.md").read_text(encoding="utf-8") == ("participant answer")
        assert (
            json.loads((invocation_dir / "meta.json").read_text(encoding="utf-8"))["returncode"]
            == 0
        )
        assert invocation["invocation_sequence"] == 1
        assert (
            json.loads((invocation_dir / "meta.json").read_text(encoding="utf-8"))[
                "invocation_sequence"
            ]
            == 1
        )
        assert json.loads((judge_dir / "decision.json").read_text(encoding="utf-8")) == {
            "winner": "codex"
        }
        assert final_record["content_sha256"] == content_sha256("synthesis")
        assert store.verify_integrity(strict=True)

        lines = (store.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert all(len(event["content_sha256"]) == 64 and event["type"] for event in events)


def test_events_are_append_only(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        events_path = store.run_dir / "events.jsonl"
        before = events_path.read_bytes()
        store.append_event(type="custom", payload={"value": 1})
        after = events_path.read_bytes()

        assert after.startswith(before)
        assert len(after) > len(before)


def test_context_manager_persists_failure(tmp_path: Path) -> None:
    run_dir: Path | None = None
    with (
        pytest.raises(RuntimeError, match="boom"),
        ArtifactStore.create(tmp_path, {}, "request") as store,
    ):
        run_dir = store.run_dir
        raise RuntimeError("boom")

    assert run_dir is not None
    manifest = read_manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "boom"
    failure_path = run_dir / manifest["failure_artifact"]
    assert json.loads(failure_path.read_text(encoding="utf-8"))["message"] == "boom"


def test_integrity_hash_detects_tampering_on_resume(tmp_path: Path) -> None:
    store = ArtifactStore.create(tmp_path, {}, "request")
    run_dir = store.run_dir
    store.close()
    (run_dir / "request.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match=r"request\.md"):
        load_existing(run_dir)

    resumed = load_existing(run_dir, verify=False)
    resumed.close()


def test_manifest_index_must_match_verified_invocation_metadata(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        store.write_invocation(1, "stage", "participant", "prompt", "result")
        run_dir = store.run_dir

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["invocations"][0]["kind"] = "judge_attempt"
    manifest["invocations"][0]["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="disagrees with verified metadata"):
        load_existing(run_dir)


def test_manifest_rejects_reordered_invocation_chronology(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        store.write_invocation(1, "stage", "participant-a", "prompt", "first")
        store.write_invocation(1, "stage", "participant-b", "prompt", "second")
        run_dir = store.run_dir

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["invocations"].reverse()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="invocation_sequence"):
        load_existing(run_dir)


def test_concurrent_invocations_receive_one_total_order(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        with ThreadPoolExecutor(max_workers=8) as executor:
            invocations = list(
                executor.map(
                    lambda index: store.write_invocation(
                        1,
                        "stage",
                        f"participant-{index}",
                        "prompt",
                        f"result-{index}",
                    ),
                    range(24),
                )
            )

        manifest_invocations = store.manifest["invocations"]
        assert [item["invocation_sequence"] for item in manifest_invocations] == list(range(1, 25))
        assert sorted(item["invocation_sequence"] for item in invocations) == list(range(1, 25))
        assert store.verify_integrity(strict=True)


def test_manifest_event_count_must_match_verified_ordered_event_log(
    tmp_path: Path,
) -> None:
    store = ArtifactStore.create(tmp_path, {}, "request")
    run_dir = store.run_dir
    store.close()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["event_count"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="event_count"):
        load_existing(run_dir)


def test_safe_manifest_reload_rejects_tampering_without_poisoning_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore.create(tmp_path, {}, "request")
    original = store.manifest
    manifest_path = store.run_dir / "manifest.json"
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["config_snapshot"] = "../outside.yaml"
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        with pytest.raises(ArtifactIntegrityError, match="config_snapshot"):
            store.read_manifest()
        assert store.manifest == original
    finally:
        store.close()


def test_store_rejects_replaced_run_directory_and_never_writes_replacement(
    tmp_path: Path,
) -> None:
    store = ArtifactStore.create(tmp_path, {}, "request")
    run_dir = store.run_dir
    moved = tmp_path / "moved-run"
    run_dir.rename(moved)
    run_dir.mkdir()
    try:
        with pytest.raises(RunLockedError, match="run directory pathname changed"):
            store.update_manifest(status="judging")
        assert not (run_dir / "manifest.json").exists()
    finally:
        store.close()


def test_manifest_rejects_unknown_schema_and_unsafe_pointers(tmp_path: Path) -> None:
    for field, value, message in (
        ("schema_version", 999, "schema_version"),
        ("config_snapshot", "../outside.yaml", "config_snapshot"),
    ):
        store = ArtifactStore.create(tmp_path, {}, "request")
        run_dir = store.run_dir
        store.close()
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[field] = value
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ArtifactIntegrityError, match=message):
            load_existing(run_dir)


def test_manifest_rejects_unlinked_invocation_index_path(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        store.write_invocation(1, "stage", "participant", "prompt", "result")
        run_dir = store.run_dir

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["invocations"][0]["path"] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="invocation path"):
        load_existing(run_dir)


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf"), "0", True])
def test_manifest_rejects_invalid_elapsed_budget(tmp_path: Path, elapsed: object) -> None:
    store = ArtifactStore.create(tmp_path, {}, "request")
    run_dir = store.run_dir
    store.close()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["elapsed_seconds"] = elapsed
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="elapsed_seconds"):
        load_existing(run_dir)


def test_lock_rejects_symlinks_and_hardlinks_without_clobbering(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("preserve me", encoding="utf-8")

    symlink_run = tmp_path / "symlink-run"
    symlink_run.mkdir()
    (symlink_run / "run.lock").symlink_to(victim)
    with pytest.raises(RunLockedError, match="safely open"):
        ArtifactStore(symlink_run)
    assert victim.read_text(encoding="utf-8") == "preserve me"

    hardlink_run = tmp_path / "hardlink-run"
    hardlink_run.mkdir()
    os.link(victim, hardlink_run / "run.lock")
    with pytest.raises(RunLockedError, match="single-link"):
        ArtifactStore(hardlink_run)
    assert victim.read_text(encoding="utf-8") == "preserve me"


def test_integrity_reader_rejects_artifact_symlinks(tmp_path: Path) -> None:
    store = ArtifactStore.create(tmp_path, {}, "request")
    run_dir = store.run_dir
    store.close()
    victim = tmp_path / "victim.txt"
    victim.write_text("private host content", encoding="utf-8")
    (run_dir / "request.md").unlink()
    (run_dir / "request.md").symlink_to(victim)

    with pytest.raises(ArtifactIntegrityError, match=r"request\.md"):
        load_existing(run_dir)


def test_invocations_are_immutable_and_judge_requires_valid_decision(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        first = store.write_invocation(
            1,
            "stage",
            "participant",
            "prompt",
            "first",
            elapsed_seconds=12.5,
        )
        second = store.write_invocation(1, "stage", "participant", "prompt", "second")

        assert first["path"] != second["path"]
        assert store.manifest["elapsed_seconds"] == 12.5
        assert store.read_artifact_text(f"{first['path']}/final.md") == "first"
        assert store.read_artifact_text(f"{second['path']}/final.md") == "second"
        assert len(store.manifest["invocations"]) == 2
        with pytest.raises(ArtifactError, match="validated Judge"):
            store.write_judge(1, "prompt", "invalid", None)
        assert store.manifest["judges"] == []


def test_invocation_metadata_identity_cannot_be_overridden_by_result(
    tmp_path: Path,
) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        invocation = store.write_invocation(
            1,
            "stage",
            "participant",
            "prompt",
            {
                "final": "result",
                "status": "success",
                "kind": "attacker-value",
                "round_number": 999,
            },
        )
        metadata = json.loads(store.read_artifact_text(f"{invocation['path']}/meta.json"))

    assert metadata["kind"] == "participant"
    assert metadata["round_number"] == 1


def test_invocation_rejects_unknown_result_status(tmp_path: Path) -> None:
    with (
        ArtifactStore.create(tmp_path, {}, "request") as store,
        pytest.raises(ArtifactError, match="unsupported invocation status"),
    ):
        store.write_invocation(
            1,
            "stage",
            "participant",
            "prompt",
            {"final": "result", "status": "unknown"},
        )


@pytest.mark.parametrize("elapsed", [-1.0, float("nan"), float("inf"), True])
def test_invocation_rejects_invalid_elapsed_checkpoint(
    tmp_path: Path,
    elapsed: object,
) -> None:
    with (
        ArtifactStore.create(tmp_path, {}, "request") as store,
        pytest.raises(ArtifactError, match="elapsed_seconds"),
    ):
        store.write_invocation(
            1,
            "stage",
            "participant",
            "prompt",
            "result",
            elapsed_seconds=elapsed,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "value",
    ["../escape", "a/b", ".", "..", "", "white space", "CON", "name\nbreak"],
)
def test_unsafe_path_ids_are_rejected(value: str) -> None:
    with pytest.raises(UnsafeArtifactPathError):
        validate_path_id(value)


def test_invocation_rejects_unsafe_stage_without_writing_outside_run(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {}, "request") as store:
        with pytest.raises(UnsafeArtifactPathError):
            store.write_invocation(1, "../escape", "codex", "prompt", "result")

        assert not (tmp_path / "escape").exists()
