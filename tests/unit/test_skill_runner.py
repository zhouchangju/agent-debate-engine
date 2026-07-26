from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_debate import skill_runner
from agent_debate.engine import EngineResult
from agent_debate.errors import AgentExecutionError


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_plan_emits_safe_machine_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = skill_runner.main(["plan", "--workspace", str(tmp_path), "--depth", "standard"])

    payload = _output(capsys)
    assert code == 0
    assert payload == {
        "agents": ["codex_primary", "codex_alternative"],
        "depth": "standard",
        "max_elapsed_seconds": 900.0,
        "max_rounds": 3,
        "mode": "plan",
        "ok": True,
        "permission": "read_only",
        "preset": "technical-review",
        "provider_calls": False,
        "stages": ["proposals", "critique", "revision"],
        "workspace": str(tmp_path),
    }


def test_run_uses_task_file_and_emits_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("Review recovery invariants.", encoding="utf-8")
    observed: dict[str, Any] = {}

    async def fake_run(config: Any, task: str, **kwargs: Any) -> EngineResult:
        observed.update(config=config, task=task, kwargs=kwargs)
        run_dir = tmp_path / "run"
        return EngineResult(
            run_id="run-1",
            run_dir=run_dir,
            status="finalized",
            stop_reason="stable decision",
            rounds_completed=2,
            final_report="Use verified barriers.",
        )

    monkeypatch.setattr(skill_runner, "run_debate", fake_run)

    code = skill_runner.main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--depth",
            "deep",
            "--task-file",
            str(task_file),
        ]
    )

    payload = _output(capsys)
    assert code == 0
    assert observed["task"] == "Review recovery invariants."
    assert observed["config"].workflow.stop.max_rounds == 5
    assert observed["kwargs"] == {"stream_handler": None}
    assert payload["ok"] is True
    assert payload["depth"] == "deep"
    assert payload["status"] == "finalized"
    assert payload["run_dir"] == str(tmp_path / "run")
    assert payload["final_path"] == str(tmp_path / "run" / "final.md")


def test_run_error_reports_only_request_owned_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("Review failure semantics.", encoding="utf-8")
    expected_run: Path | None = None

    async def fail_run(config: Any, _task: str, **_kwargs: Any) -> EngineResult:
        nonlocal expected_run
        expected_run = config.run.output_dir / "engine-run"
        expected_run.mkdir(parents=True)
        (expected_run / "manifest.json").write_text("{}", encoding="utf-8")
        raise AgentExecutionError("provider failed")

    monkeypatch.setattr(skill_runner, "run_debate", fail_run)

    code = skill_runner.main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--task-file",
            str(task_file),
        ]
    )

    payload = _output(capsys)
    assert code == 1
    assert expected_run is not None
    assert payload == {
        "error": "provider failed",
        "error_type": "AgentExecutionError",
        "manifest_path": str(expected_run / "manifest.json"),
        "ok": False,
        "run_dir": str(expected_run),
    }


def test_resume_forwards_explicit_retry_without_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}

    async def fake_resume(run_dir: Path, **kwargs: Any) -> EngineResult:
        observed.update(run_dir=run_dir, kwargs=kwargs)
        return EngineResult(
            run_id="run-2",
            run_dir=run_dir,
            status="exhausted",
            stop_reason="maximum rounds reached",
            rounds_completed=3,
            final_report="More evidence is required.",
        )

    monkeypatch.setattr(skill_runner, "resume_debate", fake_resume)
    run_dir = tmp_path / "saved-run"

    code = skill_runner.main(["resume", str(run_dir), "--retry-failed"])

    payload = _output(capsys)
    assert code == 0
    assert observed == {
        "run_dir": run_dir,
        "kwargs": {"retry_failed": True, "stream_handler": None},
    }
    assert payload["status"] == "exhausted"
    assert "depth" not in payload


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_run_rejects_empty_task_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text(text, encoding="utf-8")

    async def unexpected_run(*_args: Any, **_kwargs: Any) -> EngineResult:
        raise AssertionError("provider must not run")

    monkeypatch.setattr(skill_runner, "run_debate", unexpected_run)

    code = skill_runner.main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--task-file",
            str(task_file),
        ]
    )

    assert code == 1
    assert _output(capsys)["error_type"] == "ConfigError"


def test_bundled_script_runs_from_source_checkout(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "skills" / "agent-debate" / "scripts" / "run_debate.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "plan",
            "--workspace",
            str(tmp_path),
            "--depth",
            "quick",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["provider_calls"] is False
    assert payload["max_rounds"] == 1


def test_invalid_runner_usage_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = skill_runner.main(["plan", "--workspace", str(tmp_path), "--depth", "unbounded"])

    assert code == 2
    payload = _output(capsys)
    assert payload["ok"] is False
    assert payload["error_type"] == "RunnerUsageError"
    assert "invalid choice" in payload["error"]
