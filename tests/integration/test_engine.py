from __future__ import annotations

import json
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from agent_debate.adapters.base import BaseAdapter, CommandSpec
from agent_debate.artifacts import ArtifactStore
from agent_debate.config import DebateConfig
from agent_debate.engine import DebateEngine, _provider_scratch_root, resume_debate
from agent_debate.errors import (
    AgentExecutionError,
    ConfigError,
    JudgeProtocolError,
    ResumeError,
)
from agent_debate.preflight import AgentDiagnostic

_FIXTURE_COMMANDS: dict[str, tuple[str, ...]] = {}
_SCRATCH_OBSERVATIONS: list[tuple[Path, int, int]] = []


class _FixtureAdapter(BaseAdapter):
    """Test-only supervised adapter selected independently of provider argv."""

    name = "fixture"

    def build_command(
        self,
        request: Any,
        agent_config: Any = None,
    ) -> CommandSpec:
        del agent_config
        if request.final_output_path is not None:
            _SCRATCH_OBSERVATIONS.append(
                (
                    request.final_output_path,
                    stat.S_IMODE(request.final_output_path.parent.stat().st_mode),
                    stat.S_IMODE(request.final_output_path.stat().st_mode),
                )
            )
        argv = _FIXTURE_COMMANDS[request.agent_id]
        return CommandSpec(
            argv=argv,
            display_argv=argv,
            cwd=request.cwd,
            stdin=request.prompt,
            timeout_seconds=request.timeout_seconds,
            max_output_chars=request.max_output_chars,
            max_final_output_chars=request.max_final_output_chars,
        )


@pytest.fixture(autouse=True)
def _use_fixture_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    _SCRATCH_OBSERVATIONS.clear()
    state_home = (
        Path(__file__).parents[2] / ".pytest-engine-state" / tmp_path.parent.name / tmp_path.name
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr("agent_debate.engine.get_adapter", lambda _kind: _FixtureAdapter())

    async def fixture_diagnostics(*_args: Any, **_kwargs: Any) -> list[AgentDiagnostic]:
        return [
            AgentDiagnostic(
                agent_id="fixture",
                adapter="codex",
                executable=Path(sys.executable),
                version="codex-cli 0.145.0",
                ok=True,
            )
        ]

    monkeypatch.setattr("agent_debate.engine.diagnose_agents", fixture_diagnostics)
    yield
    shutil.rmtree(state_home, ignore_errors=True)
    for parent in (state_home.parent, state_home.parent.parent):
        with suppress(OSError):
            parent.rmdir()


def _write_prompt(root: Path, name: str, text: str) -> Path:
    path = root / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_command(*args: str) -> list[str]:
    script = Path(__file__).parents[1] / "fixtures/fake_agent.py"
    # ``-S`` keeps pytest-cov's subprocess site hook from creating incompatible
    # statement-only coverage shards for this standard-library-only fixture.
    return [sys.executable, "-S", str(script), *args]


def _config(
    tmp_path: Path,
    *,
    judge_verdict: str = "finalize",
    judge_confidence: float = 0.97,
    max_rounds: int = 2,
    max_elapsed_seconds: float = 30.0,
    max_parallel: int = 2,
) -> DebateConfig:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    role_paths = {
        name: _write_prompt(prompts, name, f"You are the {name}.")
        for name in ("proposal-a", "proposal-b", "critique", "revision", "judge")
    }

    def agent(agent_id: str, command: list[str]) -> dict[str, Any]:
        _FIXTURE_COMMANDS[agent_id] = tuple(command)
        return {
            "adapter": "codex",
            "command": [sys.executable],
            "permission": "read_only",
            "timeout": 5.0,
            "max_output": 100_000,
            "retries": 0,
        }

    raw = {
        "schema_version": 1,
        "run": {
            "output_dir": str(tmp_path / "runs"),
            "workspace": str(tmp_path),
            "max_parallel": max_parallel,
            "stream": False,
        },
        "agents": {
            "proposal_a": agent(
                "proposal_a",
                _fake_command(
                    "--id",
                    "proposal-a",
                    "--delay",
                    "0.1",
                    "--trace-file",
                    str(tmp_path / "parallel.trace"),
                    "--barrier-count",
                    "2",
                ),
            ),
            "proposal_b": agent(
                "proposal_b",
                _fake_command(
                    "--id",
                    "proposal-b",
                    "--delay",
                    "0.1",
                    "--trace-file",
                    str(tmp_path / "parallel.trace"),
                    "--barrier-count",
                    "2",
                ),
            ),
            "critic": agent("critic", _fake_command("--id", "critique")),
            "reviewer": agent("reviewer", _fake_command("--id", "revision")),
            "judge": agent(
                "judge",
                _fake_command(
                    "--judge",
                    "--verdict",
                    judge_verdict,
                    "--confidence",
                    str(judge_confidence),
                ),
            ),
        },
        "workflow": {
            "stages": [
                {
                    "id": "proposals",
                    "mode": "parallel",
                    "participants": [
                        {
                            "id": "proposal-a",
                            "agent": "proposal_a",
                            "prompt": str(role_paths["proposal-a"]),
                        },
                        {
                            "id": "proposal-b",
                            "agent": "proposal_b",
                            "prompt": str(role_paths["proposal-b"]),
                        },
                    ],
                },
                {
                    "id": "critique",
                    "mode": "sequential",
                    "participants": [
                        {
                            "id": "critique",
                            "agent": "critic",
                            "prompt": str(role_paths["critique"]),
                        }
                    ],
                },
                {
                    "id": "revision",
                    "mode": "sequential",
                    "participants": [
                        {
                            "id": "revision",
                            "agent": "reviewer",
                            "prompt": str(role_paths["revision"]),
                        }
                    ],
                },
            ],
            "judge": {"agent": "judge", "prompt": str(role_paths["judge"])},
            "stop": {
                "min_rounds": 1,
                "max_rounds": max_rounds,
                "confidence_threshold": 0.9,
                "stable_rounds": 1,
                "max_elapsed_seconds": max_elapsed_seconds,
            },
        },
        "context": {
            "max_prompt_chars": 20_000,
            "max_requirement_chars": 2_000,
            "max_response_chars": 4_000,
            "keep_recent_rounds": 1,
        },
        "failure": {
            "on_agent_error": "abort",
            "on_judge_error": "retry",
            "require_all_participants": True,
            "schema_repair_attempts": 1,
        },
    }
    return DebateConfig.model_validate(raw).resolved(relative_to=tmp_path)


def _assert_private_scratch_observations(workspace: Path) -> None:
    system_temp = Path(tempfile.gettempdir()).resolve()
    for output_path, scratch_mode, output_mode in _SCRATCH_OBSERVATIONS:
        assert not output_path.exists()
        assert workspace.resolve() not in output_path.resolve(strict=False).parents
        assert system_temp not in output_path.resolve(strict=False).parents
        assert scratch_mode == 0o700
        assert output_mode == 0o600
        assert stat.S_IMODE(output_path.parent.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_engine_preserves_stage_dependencies_and_artifacts(tmp_path: Path) -> None:
    engine = DebateEngine(_config(tmp_path), run_preflight=False)

    result = await engine.run("Choose a robust architecture.")

    assert result.status == "finalized"
    assert result.rounds_completed == 1
    assert result.run_dir.is_dir()
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "finalized"
    assert manifest["round_count"] == 1
    assert (result.run_dir / "final.md").is_file()
    assert (result.run_dir / "events.jsonl").is_file()

    def invocation_file(stage: str, participant: str, filename: str) -> Path:
        invocation = next(
            item
            for item in manifest["invocations"]
            if item["stage"] == stage and item["participant"] == participant
        )
        return result.run_dir / invocation["path"] / filename

    proposal_a = invocation_file("proposals", "proposal-a", "request.md")
    proposal_b = invocation_file("proposals", "proposal-b", "request.md")
    critic = invocation_file("critique", "critique", "request.md")
    revision = invocation_file("revision", "revision", "request.md")
    judge = result.run_dir / "rounds/001/judge/request.md"

    assert "fixture-response-proposal-b" not in proposal_a.read_text(encoding="utf-8")
    assert "fixture-response-proposal-a" not in proposal_b.read_text(encoding="utf-8")
    critic_prompt = critic.read_text(encoding="utf-8")
    assert "fixture-response-proposal-a" in critic_prompt
    assert "fixture-response-proposal-b" in critic_prompt
    assert "fixture-response-critique" in revision.read_text(encoding="utf-8")
    judge_prompt = judge.read_text(encoding="utf-8")
    assert "fixture-response-revision" in judge_prompt
    evidence_positions = [
        judge_prompt.index(marker)
        for marker in (
            "fixture-response-proposal-a",
            "fixture-response-proposal-b",
            "fixture-response-critique",
            "fixture-response-revision",
        )
    ]
    assert evidence_positions == sorted(evidence_positions)
    proposal_meta = json.loads(
        invocation_file("proposals", "proposal-a", "meta.json").read_text(encoding="utf-8")
    )
    assert proposal_meta["transport_truncated"] is False
    assert proposal_meta["transport_observed_chars"] > 0
    assert not (result.run_dir / ".provider-output").exists()
    assert _SCRATCH_OBSERVATIONS
    _assert_private_scratch_observations(tmp_path)
    trace_events = [
        line.split(":", maxsplit=2)[:2]
        for line in (tmp_path / "parallel.trace").read_text(encoding="utf-8").splitlines()
    ]
    assert [event for event, _identity in trace_events[:2]] == ["start", "start"]
    assert {identity for _event, identity in trace_events[:2]} == {
        "proposal-a",
        "proposal-b",
    }


@pytest.mark.asyncio
async def test_direct_engine_api_rejects_oversized_task_before_creating_run(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(ConfigError, match="max_requirement_chars"):
        await DebateEngine(config, run_preflight=False).run("x" * 2_001)

    assert not config.run.output_dir.exists()


@pytest.mark.asyncio
async def test_failed_command_build_redacts_credentials_from_run_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "engine-fallback-literal-secret"
    original = _config(tmp_path)
    raw = original.model_dump(mode="python")
    raw["workflow"]["stages"][0]["mode"] = "sequential"
    raw["agents"]["proposal_a"].update(
        adapter="generic",
        command=("fixture", "--api-key", secret),
        prompt_transport="stdin",
    )
    config = DebateConfig.model_validate(raw).resolved(relative_to=tmp_path)

    class RejectingAdapter(BaseAdapter):
        name = "rejecting"

        def build_command(
            self,
            request: Any,
            agent_config: Any = None,
        ) -> CommandSpec:
            del request, agent_config
            raise ConfigError("Adapter rejected a credential-shaped command.")

    fixture = _FixtureAdapter()
    monkeypatch.setattr(
        "agent_debate.engine.get_adapter",
        lambda kind: RejectingAdapter() if getattr(kind, "value", kind) == "generic" else fixture,
    )

    with pytest.raises(AgentExecutionError, match="credential-shaped"):
        await DebateEngine(
            config,
            allow_unsafe=True,
            run_preflight=False,
        ).run("Keep credentials out of audit artifacts.")

    run_dir = next(config.run.output_dir.iterdir())
    all_artifact_text = "\n".join(
        path.read_text(encoding="utf-8") for path in run_dir.rglob("*") if path.is_file()
    )
    assert secret not in all_artifact_text
    assert "<credential:redacted>" in all_artifact_text


def test_provider_scratch_rejects_symlinked_state_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "real-state"
    target.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(linked_state))

    with pytest.raises(OSError, match="symbolic link"):
        _provider_scratch_root(Path.cwd())


def test_provider_scratch_rejects_workspace_and_system_temp_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_state = workspace / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(workspace_state))
    with pytest.raises(ConfigError, match="workspace"):
        _provider_scratch_root(workspace)
    assert not (workspace_state / "agent-debate-engine").exists()

    temporary_state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(temporary_state))
    with pytest.raises(ConfigError, match="system temporary"):
        _provider_scratch_root(Path.cwd())
    assert not (temporary_state / "agent-debate-engine").exists()


@pytest.mark.asyncio
async def test_engine_marks_non_converged_round_limit_as_exhausted(tmp_path: Path) -> None:
    engine = DebateEngine(
        _config(
            tmp_path,
            judge_verdict="continue",
            judge_confidence=0.5,
            max_rounds=1,
        ),
        run_preflight=False,
    )

    result = await engine.run("Find a decision.")

    assert result.status == "exhausted"
    assert "Not converged" in result.final_report
    assert "maximum rounds reached" in result.stop_reason


@pytest.mark.asyncio
async def test_failed_run_resumes_from_last_judge_barrier(tmp_path: Path) -> None:
    config = _config(tmp_path)
    fail_once = tmp_path / "fail-once.marker"
    _FIXTURE_COMMANDS["proposal_a"] = (
        *_FIXTURE_COMMANDS["proposal_a"],
        "--fail-once-file",
        str(fail_once),
    )

    with pytest.raises(AgentExecutionError):
        await DebateEngine(config, run_preflight=False).run("Resume this safely.")

    run_dir = next((tmp_path / "runs").iterdir())
    failed_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert failed_manifest["status"] == "failed"
    assert failed_manifest["elapsed_seconds"] > 0
    failed_elapsed = failed_manifest["elapsed_seconds"]
    failed_invocation_paths = {item["path"] for item in failed_manifest["invocations"]}

    resumed = await resume_debate(run_dir, retry_failed=True)

    assert resumed.status == "finalized"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["resume_count"] == 1
    assert manifest["status"] == "finalized"
    assert manifest["elapsed_seconds"] >= failed_elapsed
    assert failed_invocation_paths < {item["path"] for item in manifest["invocations"]}


def test_resume_evidence_uses_latest_attempt_even_when_it_failed(tmp_path: Path) -> None:
    engine = DebateEngine(_config(tmp_path), run_preflight=False)

    with ArtifactStore.create(tmp_path / "audit", {}, "request") as store:
        store.write_invocation(
            1,
            "proposals",
            "proposal-a",
            "prompt",
            {"status": "success", "final": "discarded-old-success"},
        )
        store.write_invocation(
            1,
            "proposals",
            "proposal-b",
            "prompt",
            {"status": "success", "final": "current-success"},
        )
        store.write_invocation(
            1,
            "proposals",
            "proposal-a",
            "prompt",
            {"status": "failed", "final": ""},
        )

        evidence = engine._load_prior_evidence(store, completed_rounds=1)

    assert [(item.agent, item.content) for item in evidence] == [("proposal-b", "current-success")]


@pytest.mark.asyncio
async def test_invalid_judge_attempts_do_not_create_barrier_and_resume_cleanly(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state_file = tmp_path / "invalid-judge-attempts.txt"
    _FIXTURE_COMMANDS["judge"] = (
        *_FIXTURE_COMMANDS["judge"],
        "--invalid-judge-attempts",
        "2",
        "--invalid-judge-state-file",
        str(state_file),
    )

    with pytest.raises(JudgeProtocolError):
        await DebateEngine(config, run_preflight=False).run("Repair invalid Judge output.")

    run_dir = next((tmp_path / "runs").iterdir())
    failed = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert failed["judges"] == []
    old_paths = {item["path"] for item in failed["invocations"]}

    resumed = await resume_debate(run_dir, retry_failed=True)

    assert resumed.status == "finalized"
    completed = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [item["round_number"] for item in completed["judges"]] == [1]
    assert old_paths < {item["path"] for item in completed["invocations"]}
    assert all((run_dir / path).is_dir() for path in old_paths)


@pytest.mark.asyncio
async def test_sensitive_looking_agent_id_round_trips_through_resume(tmp_path: Path) -> None:
    original = _config(tmp_path)
    raw = original.model_dump(mode="python")
    raw["agents"]["token"] = raw["agents"].pop("proposal_a")
    raw["workflow"]["stages"][0]["participants"][0]["agent"] = "token"
    config = DebateConfig.model_validate(raw).resolved(relative_to=tmp_path)
    _FIXTURE_COMMANDS["token"] = _FIXTURE_COMMANDS.pop("proposal_a")
    marker = tmp_path / "token-fail-once.txt"
    _FIXTURE_COMMANDS["token"] = (
        *_FIXTURE_COMMANDS["token"],
        "--fail-once-file",
        str(marker),
    )

    with pytest.raises(AgentExecutionError):
        await DebateEngine(config, run_preflight=False).run("Preserve the token agent ID.")

    run_dir = next((tmp_path / "runs").iterdir())
    snapshot = (run_dir / "config.resolved.yaml").read_text(encoding="utf-8")
    assert "\n  token:\n" in snapshot

    result = await resume_debate(run_dir, retry_failed=True)
    assert result.status == "finalized"


@pytest.mark.asyncio
async def test_resume_disambiguates_user_ids_from_internal_judge_namespace(
    tmp_path: Path,
) -> None:
    original = _config(
        tmp_path,
        judge_verdict="continue",
        judge_confidence=0.5,
        max_rounds=2,
    )
    raw = original.model_dump(mode="python")
    raw["workflow"]["stages"][0]["id"] = "judge-call"
    raw["workflow"]["stages"][0]["participants"][0]["id"] = "judge-protocol-1"
    config = DebateConfig.model_validate(raw).resolved(relative_to=tmp_path)
    state_file = tmp_path / "proposal-invocations.txt"
    _FIXTURE_COMMANDS["proposal_a"] = (
        *_FIXTURE_COMMANDS["proposal_a"],
        "--fail-on-invocation",
        "2",
        "--invocation-state-file",
        str(state_file),
    )

    with pytest.raises(AgentExecutionError):
        await DebateEngine(config, run_preflight=False).run("Keep namespaces distinct.")

    run_dir = next((tmp_path / "runs").iterdir())
    result = await resume_debate(run_dir, retry_failed=True)
    assert result.status == "exhausted"

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    participant_calls = [
        item
        for item in manifest["invocations"]
        if item["round_number"] == 2
        and item["stage"] == "judge-call"
        and item["participant"] == "judge-protocol-1"
        and item["kind"] == "participant"
        and item["status"] == "success"
    ]
    assert participant_calls
    resumed_prompt = (run_dir / participant_calls[-1]["path"] / "request.md").read_text(
        encoding="utf-8"
    )
    assert "fixture-response-proposal-a" in resumed_prompt
    assert "Deterministic fixture decision." not in resumed_prompt
    assert any(item["kind"] == "judge_attempt" for item in manifest["invocations"])


@pytest.mark.asyncio
async def test_parallel_queue_budget_exhaustion_is_timed_out(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        max_elapsed_seconds=0.05,
        max_parallel=1,
    )
    _FIXTURE_COMMANDS["proposal_a"] = _fake_command("--id", "proposal-a", "--delay", "0.2")
    _FIXTURE_COMMANDS["proposal_b"] = _fake_command("--id", "proposal-b", "--delay", "0.2")

    result = await DebateEngine(config, run_preflight=False).run("Respect the global budget.")

    assert result.status == "timed_out"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "timed_out"
    assert manifest["elapsed_seconds"] >= 0.05


@pytest.mark.asyncio
async def test_sequential_abort_does_not_invoke_later_participant(tmp_path: Path) -> None:
    original = _config(tmp_path)
    raw = original.model_dump(mode="python")
    critique_stage = raw["workflow"]["stages"][1]
    critique_stage["participants"] = (
        *critique_stage["participants"],
        raw["workflow"]["stages"][2]["participants"][0],
    )
    raw["workflow"]["stages"] = raw["workflow"]["stages"][:2]
    config = DebateConfig.model_validate(raw).resolved(relative_to=tmp_path)
    trace_file = tmp_path / "sequential.trace"
    _FIXTURE_COMMANDS["critic"] = _fake_command(
        "--id",
        "critique",
        "--exit-code",
        "7",
        "--trace-file",
        str(trace_file),
    )
    _FIXTURE_COMMANDS["reviewer"] = _fake_command(
        "--id",
        "revision",
        "--trace-file",
        str(trace_file),
    )

    with pytest.raises(AgentExecutionError):
        await DebateEngine(config, run_preflight=False).run("Abort without side effects.")

    trace = trace_file.read_text(encoding="utf-8")
    assert "start:critique:" in trace
    assert "start:revision:" not in trace


@pytest.mark.asyncio
async def test_timed_out_run_is_terminal_for_resume(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        judge_verdict="continue",
        judge_confidence=0.5,
        max_rounds=1,
    )
    result = await DebateEngine(config, run_preflight=False).run("Reach a terminal run.")
    with ArtifactStore.load_existing(result.run_dir) as store:
        store.update_manifest(status="timed_out")
    before = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))

    with pytest.raises(ResumeError, match=r"already terminal.*timed_out"):
        await DebateEngine(config, run_preflight=False).resume(result.run_dir)

    after = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert after["resume_count"] == before["resume_count"]
    assert after["event_count"] == before["event_count"]


@pytest.mark.asyncio
async def test_public_resume_rejects_lifecycle_before_loading_snapshot_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    result = await DebateEngine(config, run_preflight=False).run("Reach a terminal run.")
    called = False

    def forbidden_config_load(*_args: Any, **_kwargs: Any) -> DebateConfig:
        nonlocal called
        called = True
        raise AssertionError("terminal resume must not load configuration")

    monkeypatch.setattr("agent_debate.engine._load_snapshot_config", forbidden_config_load)

    with pytest.raises(ResumeError, match="already terminal"):
        await resume_debate(result.run_dir)

    assert not called
