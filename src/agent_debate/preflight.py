"""Read-only executable discovery and version diagnostics."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_debate.adapters.base import CommandSpec
from agent_debate.adapters.process import (
    ProcessExecutionError,
    ProcessSpawnError,
    run_process,
)
from agent_debate.errors import PreflightError

_VERSION_PROBE_MAX_OUTPUT_CHARS = 16 * 1024
_VERSION_PROBE_TERMINATE_GRACE_SECONDS = 0.2
_DIAGNOSTIC_DETAIL_CHARS = 500
_BUILTIN_EXECUTABLES = {
    "codex": "codex",
    "kimi": "kimi",
}
_BUILTIN_VERSION_PATTERNS = {
    # The adapter contract is tied to the 0.145 CLI surface. Patch releases
    # retain the same provider-owned argv contract.
    "codex": re.compile(r"\Acodex-cli 0\.145\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z"),
    # Kimi's permission behavior was verified against this exact release.
    "kimi": re.compile(r"\A0\.29\.1\Z"),
}
_BUILTIN_VERSION_DESCRIPTIONS = {
    "codex": "codex-cli 0.145.x",
    "kimi": "Kimi 0.29.1 (version output 0.29.1)",
}


class AgentLike(Protocol):
    """The configuration surface needed by preflight checks."""

    @property
    def adapter(self) -> object: ...

    @property
    def command(self) -> Sequence[str]: ...

    @property
    def permission(self) -> object: ...


@dataclass(frozen=True, slots=True)
class AgentDiagnostic:
    """Result of a non-mutating executable probe."""

    agent_id: str
    adapter: str
    executable: Path | None
    version: str | None
    ok: bool
    warnings: tuple[str, ...] = ()
    error: str | None = None


def resolve_executable(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
) -> Path:
    """Resolve an argv command without invoking a shell."""

    if not command or not command[0].strip():
        raise PreflightError("Agent command must contain a non-empty executable.")

    working_directory = _working_directory(cwd)
    # Process execution receives argv directly, so shell conveniences such as
    # ``$VAR`` and ``~`` are literal characters. Preflight must use the same
    # interpretation or it can approve a different executable than runtime.
    configured = command[0]
    expanded = Path(configured)
    if os.sep in configured or (os.altsep and os.altsep in configured):
        candidate = expanded
        path = (candidate if candidate.is_absolute() else working_directory / candidate).resolve()
        return _require_executable_file(path)

    raw = str(expanded)
    # Relative PATH entries are interpreted after the child changes to ``cwd``.
    # Normalize them against the same directory before using ``which``.
    search_path = os.pathsep.join(
        str(entry if entry.is_absolute() else working_directory / entry)
        for entry in (Path(value) for value in os.get_exec_path())
    )
    resolved = shutil.which(raw, path=search_path)
    if resolved is None:
        raise PreflightError(f"Executable is not on PATH: {raw}")
    return _require_executable_file(Path(resolved).resolve())


def _require_executable_file(path: Path) -> Path:
    if not path.is_file():
        raise PreflightError(f"Executable does not exist: {path}")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise PreflightError(f"File is not executable: {path}")
    return path


async def probe_version(
    command: Path | Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 5.0,
) -> str:
    """Run a bounded conventional ``--version`` probe without a shell.

    This low-level helper executes its input and is therefore used only after a
    built-in provider command has passed the product allowlist. Generic
    commands deliberately never reach this function. The shared process
    supervisor owns timeout, cancellation, output limits, and process-group
    cleanup.
    """

    configured_command = (
        (str(command),)
        if isinstance(command, Path)
        else tuple(str(argument) for argument in command)
    )
    working_directory = _working_directory(cwd)
    executable = resolve_executable(configured_command, cwd=working_directory)
    argv = (str(executable), *configured_command[1:], "--version")
    spec = CommandSpec(
        argv=argv,
        display_argv=argv,
        cwd=working_directory,
        timeout_seconds=timeout_seconds,
        max_output_chars=_VERSION_PROBE_MAX_OUTPUT_CHARS,
        terminate_grace_seconds=_VERSION_PROBE_TERMINATE_GRACE_SECONDS,
    )
    try:
        result = await run_process(spec)
    except ProcessExecutionError as exc:
        output_detail = _first_diagnostic_line(exc.stdout, exc.stderr)
        suffix = f" ({output_detail})" if output_detail else ""
        raise PreflightError(f"Version probe failed for {executable}: {exc}{suffix}") from exc
    except ProcessSpawnError as exc:
        raise PreflightError(f"Version probe failed for {executable}: {exc}") from exc

    return _first_diagnostic_line(result.stdout, result.stderr) or "version not reported"


def _first_diagnostic_line(stdout: str, stderr: str) -> str:
    text = stdout.strip() or stderr.strip()
    return text.splitlines()[0][:_DIAGNOSTIC_DETAIL_CHARS] if text else ""


def _working_directory(cwd: Path | None) -> Path:
    working_directory = Path.cwd() if cwd is None else cwd
    path = working_directory.expanduser().absolute()
    if not path.is_dir():
        raise PreflightError(f"Probe working directory does not exist: {path}")
    return path


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _validate_builtin_command(adapter: str, command: Sequence[str]) -> None:
    expected = _BUILTIN_EXECUTABLES[adapter]
    if len(command) != 1:
        raise PreflightError(
            f"{adapter} preflight requires exactly one executable and will not execute "
            "a configured argv prefix"
        )
    configured_name = Path(command[0]).name
    if configured_name != expected:
        raise PreflightError(
            f"{adapter} preflight will only execute an executable named {expected!r}; "
            f"configured name is {configured_name!r}"
        )


def _validate_preflight_command(adapter: str, command: Sequence[str]) -> None:
    if adapter == "generic":
        return
    if adapter not in _BUILTIN_EXECUTABLES:
        raise PreflightError(f"Unsupported adapter for preflight: {adapter!r}")
    _validate_builtin_command(adapter, command)


def _validate_builtin_version(adapter: str, version: str) -> None:
    pattern = _BUILTIN_VERSION_PATTERNS[adapter]
    if pattern.fullmatch(version) is None:
        expected = _BUILTIN_VERSION_DESCRIPTIONS[adapter]
        raise PreflightError(
            f"Unsupported {adapter} version response {version!r}; "
            f"expected the verified {expected} contract"
        )


async def diagnose_agents(
    agents: Mapping[str, AgentLike],
    *,
    cwd: Path | None = None,
    probe_timeout_seconds: float = 5.0,
) -> list[AgentDiagnostic]:
    """Diagnose configured agents without authenticating or sending a prompt."""

    diagnostics: list[AgentDiagnostic] = []
    working_directory = _working_directory(cwd)
    for agent_id, config in agents.items():
        adapter = _value(config.adapter)
        permission = _value(config.permission)
        warnings: list[str] = []
        if permission != "read_only":
            warnings.append(f"permission mode is {permission!r}, not read_only")
        if adapter == "kimi":
            warnings.append(
                "Kimi 0.29.1 prompt mode forces auto permission and auto-approves tools; "
                "run it only inside an external sandbox"
            )
        if adapter == "generic":
            warnings.append(
                "generic adapter safety is delegated to the configured executable; "
                "preflight does not execute generic commands because no portable "
                "side-effect-free version probe exists"
            )

        try:
            _validate_preflight_command(adapter, config.command)
            executable = resolve_executable(config.command, cwd=working_directory)
        except PreflightError as exc:
            diagnostics.append(
                AgentDiagnostic(
                    agent_id=agent_id,
                    adapter=adapter,
                    executable=None,
                    version=None,
                    ok=False,
                    warnings=tuple(warnings),
                    error=str(exc),
                )
            )
            continue

        if adapter == "generic":
            diagnostics.append(
                AgentDiagnostic(
                    agent_id=agent_id,
                    adapter=adapter,
                    executable=executable,
                    version=None,
                    ok=True,
                    warnings=tuple(warnings),
                )
            )
            continue

        try:
            version = await probe_version(
                executable,
                cwd=working_directory,
                timeout_seconds=probe_timeout_seconds,
            )
            _validate_builtin_version(adapter, version)
        except PreflightError as exc:
            diagnostics.append(
                AgentDiagnostic(
                    agent_id=agent_id,
                    adapter=adapter,
                    executable=executable,
                    version=None,
                    ok=False,
                    warnings=tuple(warnings),
                    error=str(exc),
                )
            )
        else:
            diagnostics.append(
                AgentDiagnostic(
                    agent_id=agent_id,
                    adapter=adapter,
                    executable=executable,
                    version=version,
                    ok=True,
                    warnings=tuple(warnings),
                )
            )
    return diagnostics


def require_healthy(diagnostics: Sequence[AgentDiagnostic]) -> None:
    """Fail preflight when any configured executable is unavailable."""

    failed = [item for item in diagnostics if not item.ok]
    if failed:
        details = "; ".join(f"{item.agent_id}: {item.error}" for item in failed)
        raise PreflightError(f"Agent preflight failed: {details}")
