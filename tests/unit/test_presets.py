from __future__ import annotations

from pathlib import Path

import pytest

from agent_debate.config import AgentAdapter
from agent_debate.errors import ConfigError
from agent_debate.models import PermissionMode
from agent_debate.presets import DebateDepth, build_technical_review_config


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (DebateDepth.QUICK, (1, 1, 1, 1)),
        (DebateDepth.STANDARD, (1, 3, 1, 2)),
        (DebateDepth.DEEP, (2, 5, 2, 3)),
    ],
)
def test_technical_review_depths_are_bounded_and_safe(
    tmp_path: Path,
    depth: DebateDepth,
    expected: tuple[int, int, int, int],
) -> None:
    config = build_technical_review_config(tmp_path, depth=depth)
    min_rounds, max_rounds, stable_rounds, keep_recent = expected

    assert config.run.workspace == tmp_path.resolve()
    assert config.run.output_dir == tmp_path / ".agent-debate" / "skill-runs"
    assert config.run.max_parallel == 2
    assert config.run.stream is False
    assert config.workflow.stop.min_rounds == min_rounds
    assert config.workflow.stop.max_rounds == max_rounds
    assert config.workflow.stop.stable_rounds == stable_rounds
    assert config.context.keep_recent_rounds == keep_recent
    assert [stage.id for stage in config.workflow.stages] == [
        "proposals",
        "critique",
        "revision",
    ]
    assert all(agent.adapter is AgentAdapter.CODEX for agent in config.agents.values())
    assert all(agent.permission is PermissionMode.READ_ONLY for agent in config.agents.values())
    assert all(
        participant.prompt.is_file()
        for stage in config.workflow.stages
        for participant in stage.participants
    )
    assert config.workflow.judge.prompt.is_file()


def test_technical_review_accepts_explicit_absolute_output_and_codex(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "custom-runs"
    executable = tmp_path / "bin" / "codex"

    config = build_technical_review_config(
        tmp_path,
        output_dir=output_dir,
        codex_command=executable,
        depth="quick",
        stream=True,
    )

    assert config.run.output_dir == output_dir
    assert config.run.stream is True
    assert {agent.command for agent in config.agents.values()} == {(str(executable),)}


def test_technical_review_resolves_relative_output_from_workspace(tmp_path: Path) -> None:
    config = build_technical_review_config(tmp_path, output_dir="custom/runs")

    assert config.run.output_dir == tmp_path / "custom" / "runs"


@pytest.mark.parametrize("depth", ["", "slow", "STANDARD", 1])
def test_technical_review_rejects_unknown_depth(tmp_path: Path, depth: object) -> None:
    with pytest.raises(ConfigError, match="Unknown debate depth"):
        build_technical_review_config(tmp_path, depth=depth)  # type: ignore[arg-type]


def test_technical_review_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="workspace directory does not exist"):
        build_technical_review_config(tmp_path / "missing")


@pytest.mark.parametrize("command", ["", "bad\x00command"])
def test_technical_review_rejects_invalid_codex_command(
    tmp_path: Path,
    command: str,
) -> None:
    with pytest.raises(ConfigError, match="codex_command"):
        build_technical_review_config(tmp_path, codex_command=command)
