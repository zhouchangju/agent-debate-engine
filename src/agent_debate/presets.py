"""Safe, versioned configurations for embedding and natural-language front ends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from agent_debate.config import DebateConfig
from agent_debate.errors import ConfigError


class DebateDepth(StrEnum):
    """Supported deliberation budgets for the bundled technical-review preset."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class _DepthSettings:
    min_rounds: int
    max_rounds: int
    stable_rounds: int
    max_elapsed_seconds: float
    keep_recent_rounds: int


_DEPTH_SETTINGS = {
    DebateDepth.QUICK: _DepthSettings(
        min_rounds=1,
        max_rounds=1,
        stable_rounds=1,
        max_elapsed_seconds=3_600.0,
        keep_recent_rounds=1,
    ),
    DebateDepth.STANDARD: _DepthSettings(
        min_rounds=1,
        max_rounds=3,
        stable_rounds=1,
        max_elapsed_seconds=10_800.0,
        keep_recent_rounds=2,
    ),
    DebateDepth.DEEP: _DepthSettings(
        min_rounds=2,
        max_rounds=5,
        stable_rounds=2,
        max_elapsed_seconds=21_600.0,
        keep_recent_rounds=3,
    ),
}


DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "medium"
TECHNICAL_REVIEW_AGENT_TIMEOUT_SECONDS = 3_600.0


def build_technical_review_config(
    workspace: str | Path,
    *,
    output_dir: str | Path | None = None,
    depth: DebateDepth | str = DebateDepth.STANDARD,
    codex_command: str | Path = "codex",
    stream: bool = False,
) -> DebateConfig:
    """Build the bundled safe, read-only technical-review workflow.

    The preset intentionally exposes no permission switch. Callers that need a
    custom provider or broader permission must use an explicit YAML
    configuration and the engine's existing unsafe acknowledgement.
    """

    workspace_path = _existing_directory(workspace, label="workspace")
    output_path = (
        workspace_path / ".agent-debate" / "skill-runs"
        if output_dir is None
        else _output_path(output_dir, workspace=workspace_path)
    )
    selected_depth = _coerce_depth(depth)
    settings = _DEPTH_SETTINGS[selected_depth]
    prompt_root = Path(__file__).with_name("templates") / "prompts"
    executable = str(codex_command)
    if not executable or "\x00" in executable:
        raise ConfigError("codex_command must be a non-empty, NUL-free executable path")

    raw = {
        "schema_version": 1,
        "run": {
            "output_dir": output_path,
            "workspace": workspace_path,
            "max_parallel": 2,
            "stream": stream,
        },
        "agents": {
            "codex_primary": {
                "adapter": "codex",
                "command": [executable],
                "model": DEFAULT_CODEX_MODEL,
                "model_reasoning_effort": DEFAULT_CODEX_REASONING_EFFORT,
                "permission": "read_only",
                "extra_args": [],
                "timeout": TECHNICAL_REVIEW_AGENT_TIMEOUT_SECONDS,
                "max_output": 200_000,
                "max_final_output": 20_000,
                "retries": 0,
            },
            "codex_alternative": {
                "adapter": "codex",
                "command": [executable],
                "model": DEFAULT_CODEX_MODEL,
                "model_reasoning_effort": DEFAULT_CODEX_REASONING_EFFORT,
                "permission": "read_only",
                "extra_args": [],
                "timeout": TECHNICAL_REVIEW_AGENT_TIMEOUT_SECONDS,
                "max_output": 200_000,
                "max_final_output": 20_000,
                "retries": 0,
            },
        },
        "workflow": {
            "stages": [
                {
                    "id": "proposals",
                    "mode": "parallel",
                    "participants": [
                        {
                            "id": "architect",
                            "agent": "codex_primary",
                            "prompt": prompt_root / "architect.md",
                        },
                        {
                            "id": "alternative",
                            "agent": "codex_alternative",
                            "prompt": prompt_root / "alternative.md",
                        },
                    ],
                },
                {
                    "id": "critique",
                    "mode": "sequential",
                    "participants": [
                        {
                            "id": "critic",
                            "agent": "codex_primary",
                            "prompt": prompt_root / "critic.md",
                        }
                    ],
                },
                {
                    "id": "revision",
                    "mode": "sequential",
                    "participants": [
                        {
                            "id": "reviewer",
                            "agent": "codex_alternative",
                            "prompt": prompt_root / "reviewer.md",
                        }
                    ],
                },
            ],
            "judge": {
                "agent": "codex_primary",
                "prompt": prompt_root / "judge.md",
            },
            "stop": {
                "min_rounds": settings.min_rounds,
                "max_rounds": settings.max_rounds,
                "confidence_threshold": 0.85,
                "stable_rounds": settings.stable_rounds,
                "max_elapsed_seconds": settings.max_elapsed_seconds,
            },
        },
        "context": {
            "max_prompt_chars": 24_000,
            "max_requirement_chars": 8_000,
            "max_response_chars": 8_000,
            "keep_recent_rounds": settings.keep_recent_rounds,
        },
        "failure": {
            "on_agent_error": "abort",
            "on_judge_error": "retry",
            "require_all_participants": True,
            "schema_repair_attempts": 1,
        },
    }
    try:
        return DebateConfig.model_validate(raw).resolved(relative_to=workspace_path)
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        raise ConfigError(f"Could not build the technical-review preset: {exc}") from exc


def _coerce_depth(depth: DebateDepth | str) -> DebateDepth:
    try:
        return depth if isinstance(depth, DebateDepth) else DebateDepth(depth)
    except ValueError as exc:
        choices = ", ".join(item.value for item in DebateDepth)
        raise ConfigError(f"Unknown debate depth {depth!r}; choose one of: {choices}") from exc


def _existing_directory(path: str | Path, *, label: str) -> Path:
    resolved = _absolute_path(path)
    if not resolved.is_dir():
        raise ConfigError(f"{label} directory does not exist: {resolved}")
    return resolved


def _absolute_path(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Could not resolve path {path!r}: {exc}") from exc


def _output_path(path: str | Path, *, workspace: Path) -> Path:
    try:
        candidate = Path(path).expanduser()
        return (candidate if candidate.is_absolute() else workspace / candidate).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Could not resolve output path {path!r}: {exc}") from exc


__all__ = ["DebateDepth", "build_technical_review_config"]
