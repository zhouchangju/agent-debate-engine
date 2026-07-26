from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_debate import cli
from agent_debate.cli import app
from agent_debate.config import load_config
from agent_debate.engine import EngineResult
from agent_debate.preflight import AgentDiagnostic

runner = CliRunner()


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "agent-debate-engine" in result.stdout


def test_init_then_validate_without_calling_agents(tmp_path: Path) -> None:
    initialized = runner.invoke(app, ["init", str(tmp_path)])
    assert initialized.exit_code == 0, initialized.output

    config_path = tmp_path / "debate.yaml"
    assert config_path.is_file()
    loaded = load_config(config_path)
    assert loaded.workflow.stages

    validated = runner.invoke(app, ["validate", "--config", str(config_path)])
    assert validated.exit_code == 0, validated.output
    assert "Valid schema v1 configuration" in validated.stdout
    assert "--sandbox read-only" in validated.stdout


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0

    repeated = runner.invoke(app, ["init", str(tmp_path)])

    assert repeated.exit_code == 2
    assert "Refusing to overwrite" in repeated.output


def test_schema_prints_judge_protocol() -> None:
    result = runner.invoke(app, ["schema", "--kind", "judge"])

    assert result.exit_code == 0
    assert '"title": "Agent Debate Judge Decision v1"' in result.stdout


def test_validate_reports_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 99", encoding="utf-8")

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 2
    assert "ConfigError" in result.output


def test_run_reports_invalid_utf8_task_file_without_traceback(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    task_path = tmp_path / "task.md"
    task_path.write_bytes(b"\xff\xfe")

    result = runner.invoke(
        app,
        [
            "run",
            "--task-file",
            str(task_path),
            "--config",
            str(tmp_path / "debate.yaml"),
        ],
    )

    assert result.exit_code == 2
    assert "Could not read task file" in result.output
    assert "Traceback" not in result.output


def test_run_loads_configuration_before_reading_task_file(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--task-file",
            str(tmp_path / "also-missing.md"),
            "--config",
            str(tmp_path / "missing.yaml"),
        ],
    )

    assert result.exit_code == 2
    assert "Could not read configuration" in result.output
    assert "also-missing.md" not in result.output


def test_run_rejects_oversized_task_file_with_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    task_path = tmp_path / "task.md"
    task_path.write_text("x" * 8_001, encoding="utf-8")
    observed_sizes: list[int] = []
    original_open = Path.open

    class TrackingReader:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __enter__(self) -> TrackingReader:
            self._wrapped.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self._wrapped.__exit__(*args)  # type: ignore[attr-defined,no-any-return]

        def read(self, size: int = -1) -> str:
            observed_sizes.append(size)
            return self._wrapped.read(size)  # type: ignore[attr-defined,no-any-return]

    def tracking_open(path: Path, *args: object, **kwargs: object) -> object:
        opened = original_open(path, *args, **kwargs)
        if path == task_path:
            return TrackingReader(opened)
        return opened

    monkeypatch.setattr(Path, "open", tracking_open)
    result = runner.invoke(
        app,
        [
            "run",
            "--task-file",
            str(task_path),
            "--config",
            str(tmp_path / "debate.yaml"),
        ],
    )

    assert result.exit_code == 2
    assert "max_requirement_chars" in result.output
    assert "8000" in result.output
    assert observed_sizes == [8_001]


def test_run_rejects_oversized_piped_stdin(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        ["run", "--config", str(tmp_path / "debate.yaml")],
        input="x" * 8_001,
    )

    assert result.exit_code == 2
    assert "max_requirement_chars" in result.output
    assert "8000" in result.output


def test_schema_rejects_unknown_kind() -> None:
    result = runner.invoke(app, ["schema", "--kind", "other"])

    assert result.exit_code == 2
    assert "config" in result.output
    assert "judge" in result.output


def test_doctor_renders_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / "debate.yaml"

    async def healthy(
        _agents: object,
        *,
        cwd: Path | None = None,
    ) -> list[AgentDiagnostic]:
        assert cwd == tmp_path
        return [
            AgentDiagnostic(
                "codex",
                "codex",
                Path("/bin/codex"),
                "codex 1.0",
                True,
                warnings=("read-only",),
            )
        ]

    monkeypatch.setattr(cli, "diagnose_agents", healthy)
    success = runner.invoke(app, ["doctor", "--config", str(config_path)])
    assert success.exit_code == 0
    assert "codex 1.0" in success.output

    async def unhealthy(
        _agents: object,
        *,
        cwd: Path | None = None,
    ) -> list[AgentDiagnostic]:
        assert cwd == tmp_path
        return [
            AgentDiagnostic(
                "missing",
                "generic",
                None,
                None,
                False,
                error="not found",
            )
        ]

    monkeypatch.setattr(cli, "diagnose_agents", unhealthy)
    failure = runner.invoke(app, ["doctor", "--config", str(config_path)])
    assert failure.exit_code == 3
    assert "not found" in failure.output


def test_run_command_handles_task_file_and_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / "debate.yaml"
    task_file = tmp_path / "task.md"
    task_file.write_text("Debate this requirement.", encoding="utf-8")

    async def fake_run(_self: object, task: str) -> EngineResult:
        assert task == "Debate this requirement."
        return EngineResult(
            "run-1",
            tmp_path / "runs/run-1",
            "finalized",
            "criteria satisfied",
            1,
            "# Result",
        )

    monkeypatch.setattr(cli.DebateEngine, "run", fake_run)
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_path),
            "--task-file",
            str(task_file),
            "--no-stream",
        ],
    )
    assert result.exit_code == 0
    assert "Run status: finalized" in result.output


def test_run_command_rejects_conflicting_task_sources(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    task_file = tmp_path / "task.md"
    task_file.write_text("file task", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "argument task",
            "--config",
            str(tmp_path / "debate.yaml"),
            "--task-file",
            str(task_file),
        ],
    )

    assert result.exit_code == 2
    assert "either a task argument or --task-file" in result.output


def test_resume_maps_blocked_status_to_automation_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resume(*_args: object, **_kwargs: object) -> EngineResult:
        return EngineResult(
            "run-2",
            tmp_path / "run-2",
            "blocked",
            "critical issue",
            2,
            "# Provisional",
        )

    monkeypatch.setattr(cli, "resume_debate", fake_resume)
    result = runner.invoke(app, ["resume", str(tmp_path / "run-2"), "--no-stream"])

    assert result.exit_code == 11
    assert "Run status: blocked" in result.output
