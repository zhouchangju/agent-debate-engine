"""Shared, provider-independent adapter contracts.

The adapter layer intentionally depends on structural protocols rather than the
Pydantic configuration models.  This keeps subprocess execution reusable in
tests and prevents a dependency cycle between configuration and orchestration.
"""

from __future__ import annotations

import asyncio
import math
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from agent_debate.errors import AgentExecutionError, ConfigError

if TYPE_CHECKING:
    from agent_debate.adapters.process import ProcessResult

PermissionMode = Literal["read_only", "workspace_write", "danger_full_access"]
PromptTransport = Literal["stdin", "argument", "flag"]
StreamName = Literal["stdout", "stderr"]
SessionMode = Literal["fresh", "unverified"]
StreamCallback = Callable[[StreamName, str], Awaitable[None] | None]

REDACTED_PROMPT = "<prompt:redacted>"
REDACTED_CREDENTIAL = "<credential:redacted>"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_OUTPUT_CHARS = 100_000
DEFAULT_MAX_FINAL_OUTPUT_CHARS = 20_000
_FINAL_OUTPUT_READ_CHARS = 64 * 1024
_CREDENTIAL_OPTION_NAMES = frozenset(
    {
        "access-key",
        "access-token",
        "api-key",
        "apikey",
        "auth-token",
        "authorization",
        "bearer-token",
        "client-secret",
        "password",
        "secret",
        "token",
    }
)
_CREDENTIAL_OPTION_SUFFIXES = (
    "-access-key",
    "-access-token",
    "-api-key",
    "-auth-token",
    "-client-secret",
    "-password",
    "-secret",
    "-token",
)
_NON_CREDENTIAL_OPTION_NAMES = frozenset(
    {
        "max-token",
        "token-budget",
        "token-count",
        "token-limit",
    }
)
_CREDENTIAL_ENV_MARKERS = (
    "ACCESS_KEY",
    "ACCESS_TOKEN",
    "API_KEY",
    "AUTH_TOKEN",
    "AUTHORIZATION",
    "BEARER_TOKEN",
    "CLIENT_SECRET",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


@runtime_checkable
class AgentRequestLike(Protocol):
    """The subset of an orchestration request consumed by adapters."""

    @property
    def prompt(self) -> str: ...

    @property
    def workspace(self) -> Path: ...

    @property
    def timeout_seconds(self) -> float: ...

    @property
    def max_output_chars(self) -> int: ...

    @property
    def max_final_output_chars(self) -> int: ...

    @property
    def model(self) -> str | None: ...

    @property
    def model_reasoning_effort(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> str | None: ...

    @property
    def extra_args(self) -> tuple[str, ...]: ...

    @property
    def permission(self) -> object: ...

    @property
    def final_output_path(self) -> Path | None: ...

    @property
    def output_schema_path(self) -> Path | None: ...


@runtime_checkable
class AgentConfigLike(Protocol):
    """The subset of an agent configuration consumed by adapters."""

    @property
    def command(self) -> tuple[str, ...]: ...

    @property
    def model(self) -> str | None: ...

    @property
    def model_reasoning_effort(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> str | None: ...

    @property
    def permission(self) -> object: ...

    @property
    def extra_args(self) -> tuple[str, ...]: ...

    @property
    def timeout(self) -> float: ...

    @property
    def max_output(self) -> int: ...

    @property
    def max_final_output(self) -> int | None: ...

    @property
    def prompt_transport(self) -> object: ...

    @property
    def prompt_flag(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A shell-free process invocation and its execution limits."""

    argv: tuple[str, ...]
    display_argv: tuple[str, ...]
    cwd: Path
    stdin: str | bytes | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    max_final_output_chars: int = DEFAULT_MAX_FINAL_OUTPUT_CHARS
    truncate_transport_output: bool = False
    allow_residual_process_cleanup: bool = False
    terminate_grace_seconds: float = 2.0
    final_output_path: Path | None = None
    env: Mapping[str, str] | None = None
    provider_adapter: str = "unknown"
    provider_model: str | None = None
    session_mode: SessionMode = "unverified"
    session_enforcement: str = "adapter does not declare a fresh-session contract"

    def __post_init__(self) -> None:
        if not self.argv:
            raise ConfigError("An adapter command must contain an executable")
        if len(self.argv) != len(self.display_argv):
            raise ConfigError("display_argv must have the same shape as argv")
        if any(not argument or "\x00" in argument for argument in self.argv):
            raise ConfigError("argv entries must be non-empty and NUL-free")
        if any(not argument or "\x00" in argument for argument in self.display_argv):
            raise ConfigError("display_argv entries must be non-empty and NUL-free")
        _ensure_display_argv_redacts_credentials(self.argv, self.display_argv)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigError("timeout_seconds must be finite and greater than zero")
        _validate_output_contract(self)
        if not math.isfinite(self.terminate_grace_seconds) or self.terminate_grace_seconds < 0:
            raise ConfigError("terminate_grace_seconds must be finite and non-negative")
        if not self.provider_adapter or "\x00" in self.provider_adapter:
            raise ConfigError("provider_adapter must be non-empty and NUL-free")
        if self.provider_model is not None and "\x00" in self.provider_model:
            raise ConfigError("provider_model must be NUL-free")
        if self.session_mode not in ("fresh", "unverified"):
            raise ConfigError("session_mode must be fresh or unverified")
        if not self.session_enforcement or "\x00" in self.session_enforcement:
            raise ConfigError("session_enforcement must be non-empty and NUL-free")

    @property
    def command(self) -> tuple[str, ...]:
        """Compatibility alias for consumers that call an argv a command."""

        return self.argv

    @property
    def stdin_data(self) -> str | bytes | None:
        """Compatibility alias that makes the payload nature explicit."""

        return self.stdin


def _validate_output_contract(spec: CommandSpec) -> None:
    for name, value in (
        ("max_output_chars", spec.max_output_chars),
        ("max_final_output_chars", spec.max_final_output_chars),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"{name} must be a positive integer")
    if type(spec.truncate_transport_output) is not bool:
        raise ConfigError("truncate_transport_output must be a boolean")
    if type(spec.allow_residual_process_cleanup) is not bool:
        raise ConfigError("allow_residual_process_cleanup must be a boolean")
    if spec.allow_residual_process_cleanup and spec.final_output_path is None:
        raise ConfigError("allow_residual_process_cleanup requires final_output_path")


@runtime_checkable
class AgentAdapter(Protocol):
    """Structural interface implemented by every CLI adapter."""

    name: str

    def build_command(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
    ) -> CommandSpec: ...

    async def execute(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ProcessResult: ...


class BaseAdapter(ABC):
    """Common implementation of process execution and final-file handling."""

    name: str

    @abstractmethod
    def build_command(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
    ) -> CommandSpec:
        """Build an immutable, display-safe command specification."""

    async def execute(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ProcessResult:
        """Execute one invocation and return only a strictly successful result."""

        # Local import breaks the intentional base-contract/process-runner cycle.
        from agent_debate.adapters.process import (  # noqa: PLC0415
            ProcessOutputLimitError,
            run_process,
        )

        spec = self.build_command(request, agent_config)
        previous_final_output = (
            await asyncio.to_thread(_file_snapshot, spec.final_output_path)
            if spec.final_output_path is not None
            else None
        )
        result = await run_process(spec, on_stream=on_stream)
        if spec.final_output_path is None:
            return result

        try:
            final_text, observed_chars, current_final_output = await asyncio.to_thread(
                _read_bounded_text,
                spec.final_output_path,
                spec.max_final_output_chars,
            )
        except FileNotFoundError as exc:
            raise FinalOutputError(
                f"Agent exited successfully but did not produce or refresh final output: "
                f"{spec.final_output_path}",
                path=spec.final_output_path,
                display_argv=spec.display_argv,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                transport_truncated=result.transport_truncated,
                transport_observed_chars=result.transport_observed_chars,
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise FinalOutputError(
                f"Agent exited successfully but final output could not be read: "
                f"{spec.final_output_path}",
                path=spec.final_output_path,
                display_argv=spec.display_argv,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                transport_truncated=result.transport_truncated,
                transport_observed_chars=result.transport_observed_chars,
            ) from exc

        if current_final_output == previous_final_output:
            raise FinalOutputError(
                f"Agent exited successfully but did not produce or refresh final output: "
                f"{spec.final_output_path}",
                path=spec.final_output_path,
                display_argv=spec.display_argv,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                transport_truncated=result.transport_truncated,
                transport_observed_chars=result.transport_observed_chars,
            )

        if observed_chars > spec.max_final_output_chars:
            raise ProcessOutputLimitError(
                limit=spec.max_final_output_chars,
                observed=observed_chars,
                stream="final",
                display_argv=spec.display_argv,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                transport_truncated=result.transport_truncated,
                transport_observed_chars=result.transport_observed_chars,
            )
        return replace(result, final_text=final_text)


class FinalOutputError(AgentExecutionError):
    """A requested authoritative final-output artifact was unavailable."""

    def __init__(
        self,
        message: str,
        *,
        path: Path,
        display_argv: tuple[str, ...],
        stdout: str,
        stderr: str,
        exit_code: int,
        transport_truncated: bool,
        transport_observed_chars: int,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.display_argv = display_argv
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.transport_truncated = transport_truncated
        self.transport_observed_chars = transport_observed_chars


_FileSnapshot = tuple[int, int, int, int, int, int, int]


def _read_bounded_text(path: Path, limit: int) -> tuple[str, int, _FileSnapshot]:
    """Read one stable, regular, single-link output file without following links."""

    chunks: list[str] = []
    observed = 0
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        initial = _validated_open_file_snapshot(path, descriptor)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            while observed <= limit:
                chunk = handle.read(min(_FINAL_OUTPUT_READ_CHARS, (limit - observed) + 1))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > limit:
                    break
                chunks.append(chunk)
            final = _validated_open_file_snapshot(path, handle.fileno())
            if final != initial:
                raise OSError(f"Final output changed while it was being read: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return ("".join(chunks) if observed <= limit else ""), observed, final


def _validated_open_file_snapshot(path: Path, descriptor: int) -> _FileSnapshot:
    """Bind a visible path to one opened, private-link file identity."""

    opened = os.fstat(descriptor)
    visible = path.lstat()
    for metadata in (opened, visible):
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"Final output is not a regular file: {path}")
        if metadata.st_nlink != 1:
            raise OSError(f"Final output must have exactly one hard link: {path}")
    if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
        raise OSError(f"Final output path changed while it was being opened: {path}")
    return _snapshot_from_stat(opened)


def _snapshot_from_stat(metadata: os.stat_result) -> _FileSnapshot:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_snapshot(path: Path) -> _FileSnapshot | None:
    """Return enough lstat metadata to reject a stale output artifact."""

    try:
        metadata = path.lstat()
    except OSError:
        return None
    return _snapshot_from_stat(metadata)


def command_prefix(
    agent_config: AgentConfigLike | None,
    *,
    default_executable: str,
) -> tuple[str, ...]:
    """Resolve a configured argv prefix without parsing it as shell text."""

    if agent_config is None:
        return (default_executable,)

    configured = getattr(agent_config, "command", ())
    if isinstance(configured, str):
        configured = (configured,)
    command = tuple(str(item) for item in configured)
    if command:
        return command

    executable = getattr(agent_config, "executable", None)
    if executable:
        return (str(executable),)
    raise ConfigError("Agent command must contain an executable")


def provider_executable(
    agent_config: AgentConfigLike | None,
    *,
    default_executable: str,
    adapter_name: str,
) -> str:
    """Resolve one executable for a built-in provider adapter.

    Built-in adapters own every argument after the executable. Allowing a
    configurable argv prefix would let wrapper flags or provider subcommands
    alter a safety contract before adapter-managed flags are parsed.
    """

    command = command_prefix(agent_config, default_executable=default_executable)
    if len(command) != 1:
        raise ConfigError(
            f"{adapter_name} command must contain exactly one executable; "
            "use the generic adapter for an externally sandboxed wrapper"
        )
    return command[0]


def request_workspace(request: AgentRequestLike) -> Path:
    """Resolve the request working directory."""

    workspace = getattr(request, "workspace", None)
    if workspace is None:
        workspace = getattr(request, "cwd", None)
    if workspace is None:
        raise ConfigError("Agent request is missing a workspace")
    return Path(workspace).expanduser().absolute()


def request_timeout(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> float:
    """Resolve the process timeout, preferring a positive request override."""

    value = getattr(request, "timeout_seconds", None)
    if value is None and agent_config is not None:
        value = getattr(agent_config, "timeout", None)
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    return float(value)


def request_max_output(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> int:
    """Resolve the combined stdout/stderr character ceiling."""

    value = getattr(request, "max_output_chars", None)
    if value is None and agent_config is not None:
        value = getattr(agent_config, "max_output", None)
    if value is None:
        return DEFAULT_MAX_OUTPUT_CHARS
    return int(value)


def request_max_final_output(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> int:
    """Resolve the authoritative final-output character ceiling."""

    value = getattr(request, "max_final_output_chars", None)
    if value is None and agent_config is not None:
        value = getattr(agent_config, "max_final_output", None)
        if value is None:
            value = getattr(agent_config, "max_output", None)
    if value is None:
        return DEFAULT_MAX_FINAL_OUTPUT_CHARS
    return int(value)


def request_model(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> str | None:
    """Resolve an optional model, preferring the request value."""

    model = getattr(request, "model", None)
    if model is None and agent_config is not None:
        model = getattr(agent_config, "model", None)
    return str(model) if model is not None else None


def request_model_reasoning_effort(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> str | None:
    """Resolve an optional provider-specific model reasoning effort."""

    effort = getattr(request, "model_reasoning_effort", None)
    if effort is None and agent_config is not None:
        effort = getattr(agent_config, "model_reasoning_effort", None)
    return str(effort) if effort is not None else None


def request_reasoning_effort(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> str | None:
    """Resolve an optional general reasoning effort alias."""

    effort = getattr(request, "reasoning_effort", None)
    if effort is None and agent_config is not None:
        effort = getattr(agent_config, "reasoning_effort", None)
    return str(effort) if effort is not None else None


def request_extra_args(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> tuple[str, ...]:
    """Append configured defaults and request-specific CLI arguments."""

    configured = getattr(agent_config, "extra_args", ()) if agent_config is not None else ()
    requested = getattr(request, "extra_args", ())
    return tuple(str(item) for item in (*configured, *requested))


def request_permission(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None,
) -> PermissionMode:
    """Normalize serialized or Enum permission values."""

    permission = getattr(request, "permission", None)
    if permission is None and agent_config is not None:
        permission = getattr(agent_config, "permission", None)
    raw = getattr(permission, "value", permission)
    if raw is None:
        return "read_only"

    normalized = str(raw).strip().lower().replace("-", "_")
    aliases: dict[str, PermissionMode] = {
        "read_only": "read_only",
        "readonly": "read_only",
        "workspace_write": "workspace_write",
        "workspacewrite": "workspace_write",
        "danger_full_access": "danger_full_access",
        "dangerous_full_access": "danger_full_access",
        "full_access": "danger_full_access",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ConfigError(f"Unsupported permission mode: {raw!r}") from exc


def request_optional_path(
    request: AgentRequestLike,
    name: str,
    *,
    relative_to: Path | None = None,
) -> Path | None:
    """Read an optional path from a structural request."""

    value = getattr(request, name, None)
    if value is None:
        return None
    path = Path(value).expanduser()
    if relative_to is not None and not path.is_absolute():
        path = relative_to / path
    return path.absolute()


def reject_provider_extra_args(
    extra_args: Sequence[str],
    *,
    adapter_name: str,
) -> None:
    """Reject all unmodeled arguments for a built-in provider contract."""

    if extra_args:
        raise ConfigError(
            f"{adapter_name} extra_args are disabled because built-in adapters "
            "own their complete provider argv"
        )


def reject_literal_credentials(
    argv: Sequence[str],
    *,
    context: str,
) -> None:
    """Reject credential values embedded in argv instead of a credential store."""

    credential = _first_credential_argument(argv)
    if credential is None:
        return
    raise ConfigError(
        f"{context} cannot contain literal credential option {credential!r}; "
        "use the provider credential store or process environment"
    )


def redact_display_argv(
    argv: Sequence[str],
    *,
    redactions: Mapping[int, str] | None = None,
) -> tuple[str, ...]:
    """Return a shape-preserving argv safe for manifests, logs, and errors."""

    display = list(argv)
    for index, replacement in (redactions or {}).items():
        if index < 0 or index >= len(display):
            raise ConfigError(f"display argv redaction index is out of range: {index}")
        display[index] = replacement

    redact_next = False
    for index, argument in enumerate(argv):
        if redact_next:
            display[index] = REDACTED_CREDENTIAL
            redact_next = False
            continue

        option, separator, _value = argument.partition("=")
        if _is_credential_option(option):
            if separator:
                display[index] = f"{option}={REDACTED_CREDENTIAL}"
            else:
                redact_next = True
            continue

        if separator and _is_credential_environment_name(option):
            display[index] = f"{option}={REDACTED_CREDENTIAL}"

    return tuple(display)


def _ensure_display_argv_redacts_credentials(
    argv: Sequence[str],
    display_argv: Sequence[str],
) -> None:
    expected = redact_display_argv(argv)
    for index, (actual, safe) in enumerate(zip(display_argv, expected, strict=True)):
        if actual == argv[index] and safe != argv[index]:
            raise ConfigError(
                "display_argv exposes a literal credential at "
                f"argv index {index}; use redact_display_argv"
            )


def _first_credential_argument(argv: Sequence[str]) -> str | None:
    for argument in argv:
        option, separator, _value = argument.partition("=")
        if _is_credential_option(option):
            return option
        if separator and _is_credential_environment_name(option):
            return option
    return None


def _is_credential_option(argument: str) -> bool:
    if not argument.startswith("-"):
        return False
    name = argument.lstrip("-").lower().replace("_", "-")
    if name in _NON_CREDENTIAL_OPTION_NAMES:
        return False
    return name in _CREDENTIAL_OPTION_NAMES or name.endswith(_CREDENTIAL_OPTION_SUFFIXES)


def _is_credential_environment_name(name: str) -> bool:
    normalized = name.upper().replace("-", "_")
    return any(
        normalized == marker or normalized.endswith(f"_{marker}")
        for marker in _CREDENTIAL_ENV_MARKERS
    )
