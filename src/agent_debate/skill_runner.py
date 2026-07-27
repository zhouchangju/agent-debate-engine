"""Machine-readable runner used by the bundled natural-language Skill."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from agent_debate.engine import EngineResult, resume_debate, run_debate
from agent_debate.errors import ConfigError, DebateError
from agent_debate.presets import DebateDepth, build_technical_review_config
from agent_debate.dashboard_launcher import open_run_dashboard

_EXIT_ERROR = 1
_EXIT_USAGE = 2


class _RunnerParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise RunnerUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    """Create the stable command contract consumed by the Skill."""

    parser = _RunnerParser(
        prog="agent-debate-skill",
        description="Structured JSON bridge for the Agent Debate Skill.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Describe the safe preset without invoking a provider.",
    )
    _add_workspace_and_depth(plan_parser)

    run_parser = subparsers.add_parser(
        "run",
        help="Run a safe read-only technical review.",
    )
    _add_workspace_and_depth(run_parser)
    run_parser.add_argument(
        "--task-file",
        type=Path,
        required=True,
        help="UTF-8 task file. Task text is never placed in process argv.",
    )
    run_parser.add_argument("--open-dashboard", action="store_true")

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume one existing run after its last verified Judge barrier.",
    )
    resume_parser.add_argument("run_dir", type=Path)
    resume_parser.add_argument("--retry-failed", action="store_true")
    resume_parser.add_argument("--open-dashboard", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one bridge command and emit exactly one JSON object."""

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else _EXIT_USAGE
    except RunnerUsageError as exc:
        _emit(_error_payload(exc))
        return _EXIT_USAGE

    try:
        if args.command == "plan":
            payload = _plan_payload(args)
        elif args.command == "run":
            payload = asyncio.run(_run_command(args))
        else:
            payload = asyncio.run(_resume_command(args))
    except KeyboardInterrupt:
        _emit(
            {
                "ok": False,
                "error_type": "KeyboardInterrupt",
                "error": "Debate execution was cancelled.",
            }
        )
        return 130
    except DebateError as exc:
        _emit(_error_payload(exc))
        return _EXIT_ERROR
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        _emit(_error_payload(exc))
        return _EXIT_ERROR

    _emit(payload)
    return 0


def _add_workspace_and_depth(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(),
        help="Repository or directory the read-only agents may inspect.",
    )
    parser.add_argument(
        "--depth",
        choices=[item.value for item in DebateDepth],
        default=DebateDepth.STANDARD.value,
    )


def _plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    config = build_technical_review_config(
        args.workspace,
        depth=args.depth,
        stream=False,
    )
    return {
        "ok": True,
        "mode": "plan",
        "provider_calls": False,
        "preset": "technical-review",
        "depth": args.depth,
        "workspace": str(config.run.workspace),
        "permission": "read_only",
        "agents": list(config.agents),
        "stages": [stage.id for stage in config.workflow.stages],
        "max_rounds": config.workflow.stop.max_rounds,
        "max_elapsed_seconds": config.workflow.stop.max_elapsed_seconds,
    }


async def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.expanduser().resolve()
    output_root = _unique_output_root(workspace)
    config = build_technical_review_config(
        workspace,
        output_dir=output_root,
        depth=args.depth,
        stream=False,
    )
    try:
        task = _read_task_file(
            args.task_file,
            max_chars=config.context.max_requirement_chars,
        )
        result = await run_debate(config, task, stream_handler=None)
    except Exception as exc:
        run_dir = _single_run_dir(output_root)
        if isinstance(exc, DebateError):
            raise _RunnerError(str(exc), run_dir=run_dir, cause=exc) from exc
        raise _RunnerError(
            "Unexpected debate runner failure.",
            run_dir=run_dir,
            cause=exc,
        ) from exc
    payload = _result_payload(result, depth=args.depth)
    if args.open_dashboard:
        payload.update(_dashboard_payload(result.run_dir))
    return payload


async def _resume_command(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.expanduser().resolve()
    try:
        result = await resume_debate(
            run_dir,
            retry_failed=args.retry_failed,
            stream_handler=None,
        )
    except DebateError as exc:
        raise _RunnerError(str(exc), run_dir=run_dir, cause=exc) from exc
    payload = _result_payload(result, depth=None)
    if args.open_dashboard:
        payload.update(_dashboard_payload(result.run_dir))
    return payload


def _dashboard_payload(run_dir: Path) -> dict[str, Any]:
    try:
        launch = open_run_dashboard(run_dir)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return {
            "dashboard_opened": False,
            "dashboard_error": str(exc),
        }
    return {
        "dashboard_url": launch.url,
        "dashboard_opened": launch.browser_opened,
        "dashboard_reused": launch.reused,
    }


def _read_task_file(path: Path, *, max_chars: int) -> str:
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            value = handle.read(max_chars + 1)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"Could not read task file {path}: {exc}") from exc
    if len(value) > max_chars:
        raise ConfigError(f"The debate task exceeds the {max_chars}-character preset limit.")
    if not value.strip():
        raise ConfigError("The debate task must contain non-whitespace text.")
    return value.strip()


def _unique_output_root(workspace: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return workspace / ".agent-debate" / "skill-runs" / f"{timestamp}-{uuid.uuid4().hex}"


def _single_run_dir(output_root: Path) -> Path | None:
    try:
        candidates = sorted(
            path
            for path in output_root.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _result_payload(result: EngineResult, *, depth: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "run",
        "preset": "technical-review",
        "status": result.status,
        "converged": result.converged,
        "run_id": result.run_id,
        "run_dir": str(result.run_dir),
        "rounds_completed": result.rounds_completed,
        "stop_reason": result.stop_reason,
        "final_report": result.final_report,
        "manifest_path": str(result.run_dir / "manifest.json"),
        "result_path": str(result.run_dir / "result.json"),
        "final_path": str(result.run_dir / "final.md"),
        "evidence_path": str(result.run_dir / "evidence.md"),
    }
    if depth is not None:
        payload["depth"] = depth
    return payload


class _RunnerError(DebateError):
    """Bridge error carrying the only run directory created by this request."""

    def __init__(
        self,
        message: str,
        *,
        run_dir: Path | None,
        cause: BaseException,
    ) -> None:
        super().__init__(message)
        self.run_dir = run_dir
        self.cause_type = type(cause).__name__


class RunnerUsageError(ValueError):
    """Invalid machine-runner invocation."""


def _error_payload(error: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error_type": (
            error.cause_type if isinstance(error, _RunnerError) else type(error).__name__
        ),
        "error": str(error),
    }
    if isinstance(error, _RunnerError) and error.run_dir is not None:
        payload["run_dir"] = str(error.run_dir)
        payload["manifest_path"] = str(error.run_dir / "manifest.json")
        result_path = error.run_dir / "result.json"
        if result_path.is_file():
            payload["result_path"] = str(result_path)
        evidence_path = error.run_dir / "evidence.md"
        if evidence_path.is_file():
            payload["evidence_path"] = str(evidence_path)
    return payload


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
