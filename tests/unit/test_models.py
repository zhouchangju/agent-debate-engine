from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_debate.models import (
    AgentRequest,
    AgentResult,
    InvocationStatus,
    IssueSeverity,
    JudgeDecision,
    JudgeVerdict,
    RoundStatus,
    RoundSummary,
    RunStatus,
    RunSummary,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def _valid_decision(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "verdict": "continue",
        "confidence": 0.7,
        "rationale": "More evidence is needed [R1:proposal:architect].",
        "synthesis": "The current bounded proposal.",
        "accepted_decisions": ["Use atomic writes [R1:proposal:architect]."],
        "rejected_options": [],
        "unresolved_issues": [
            {
                "id": "ISSUE-001",
                "severity": "major",
                "summary": "Recovery behavior remains untested.",
            }
        ],
        "next_round_focus": ["Test recovery after an interrupted write."],
    }
    payload.update(updates)
    return payload


def _valid_result(**updates: Any) -> AgentResult:
    started_at = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    payload: dict[str, Any] = {
        "agent_id": "codex",
        "role_id": "architect",
        "status": "success",
        "stdout": "proposal",
        "stderr": "",
        "final_output": "proposal",
        "exit_code": 0,
        "started_at": started_at,
        "finished_at": started_at + timedelta(seconds=1),
        "duration_seconds": 1.0,
        "timed_out": False,
        "truncated": False,
        "display_command": ["codex", "exec", "<prompt-via-stdin>"],
        "input_hash": HASH_A,
        "output_hash": HASH_B,
    }
    payload.update(updates)
    return AgentResult.model_validate(payload)


def test_agent_request_is_strict_and_json_serializable(tmp_path: Path) -> None:
    request = AgentRequest(
        run_id="2026.07.26-run",
        round_number=1,
        stage_id="proposal",
        agent_id="codex",
        role_id="architect",
        prompt="Preserve this prompt formatting.\n",
        cwd=tmp_path,
        final_output_path=tmp_path / "final.md",
        output_schema_path=tmp_path / "judge.json",
    )

    assert request.workspace == tmp_path
    dumped = request.model_dump(mode="json")
    assert dumped["cwd"] == str(tmp_path)
    assert dumped["permission"] == "read_only"
    assert "model_reasoning_effort" in dumped
    assert dumped["model_reasoning_effort"] is None
    assert "reasoning_effort" in dumped
    assert dumped["prompt"].endswith("\n")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentRequest.model_validate({**request.model_dump(), "unknown": True})


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_agent_request_rejects_coerced_schema_versions(
    tmp_path: Path,
    schema_version: object,
) -> None:
    with pytest.raises(ValidationError, match="integer 1"):
        AgentRequest.model_validate(
            {
                "schema_version": schema_version,
                "agent_id": "codex",
                "role_id": "architect",
                "prompt": "Design it.",
                "cwd": tmp_path,
            }
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"prompt": " \n\t"},
        {"extra_args": [""]},
        {"timeout_seconds": 0},
        {"max_output_chars": 0},
        {"max_final_output_chars": 0},
        {"round_number": 0},
        {"model_reasoning_effort": "bad\x00effort"},
        {"reasoning_effort": "\x00"},
    ],
)
def test_agent_request_rejects_invalid_semantics(tmp_path: Path, updates: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "agent_id": "codex",
        "role_id": "architect",
        "prompt": "Design it.",
        "cwd": tmp_path,
    }
    payload.update(updates)

    with pytest.raises(ValidationError):
        AgentRequest.model_validate(payload)


def test_agent_result_success_contract_and_json_dump() -> None:
    result = _valid_result()

    assert result.status is InvocationStatus.SUCCESS
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "success"
    assert dumped["started_at"].endswith("Z")
    assert dumped["display_command"][2] == "<prompt-via-stdin>"
    assert dumped["transport_truncated"] is False
    assert dumped["transport_observed_chars"] == 0


@pytest.mark.parametrize(
    "updates",
    [
        {"finished_at": datetime(2026, 7, 25, tzinfo=UTC)},
        {"status": "success", "exit_code": 1},
        {"status": "success", "final_output": " \n"},
        {"status": "success", "truncated": True},
        {"status": "timed_out", "timed_out": False, "exit_code": None},
        {"status": "failed", "timed_out": True, "exit_code": 1},
        {"status": "output_limit", "truncated": False, "exit_code": 1},
        {
            "transport_truncated": True,
            "transport_observed_chars": len("proposal"),
        },
        {"transport_observed_chars": -1},
        {"input_hash": "not-a-sha256"},
    ],
)
def test_agent_result_rejects_inconsistent_outcomes(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _valid_result(**updates)


def test_agent_models_reject_unordered_argv_collections(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ordered"):
        AgentRequest.model_validate(
            {
                "agent_id": "codex",
                "role_id": "architect",
                "prompt": "Design it.",
                "cwd": tmp_path,
                "extra_args": {"--safe", "--unsafe"},
            }
        )
    with pytest.raises(ValidationError, match="ordered"):
        _valid_result(display_command={"safe", "unsafe"})


@pytest.mark.parametrize(
    "updates",
    [
        {"started_at": datetime(2026, 7, 26, 10, 0)},
        {"finished_at": datetime(2026, 7, 26, 10, 0)},
        {"started_at": 1_774_000_000},
    ],
)
def test_agent_result_requires_aware_non_numeric_timestamps(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _valid_result(**updates)


def test_persisted_timestamps_are_normalized_to_utc() -> None:
    offset = timezone(timedelta(hours=8))
    started_at = datetime(2026, 7, 26, 18, 0, tzinfo=offset)
    result = _valid_result(
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )

    assert result.started_at == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    assert result.model_dump(mode="json")["started_at"].endswith("Z")


def test_judge_decision_matches_schema_and_round_trips_json() -> None:
    decision = JudgeDecision.model_validate(_valid_decision())

    assert decision.verdict is JudgeVerdict.CONTINUE
    assert decision.unresolved_issues[0].severity is IssueSeverity.MAJOR
    assert decision.model_dump(mode="json")["accepted_decisions"] == [
        "Use atomic writes [R1:proposal:architect]."
    ]
    assert JudgeDecision.model_validate_json(decision.model_dump_json()) == decision


@pytest.mark.parametrize("confidence", ["0.8", True, float("nan"), float("inf"), -0.1, 1.1])
def test_judge_confidence_rejects_coercion_and_non_finite_values(
    confidence: object,
) -> None:
    with pytest.raises(ValidationError):
        JudgeDecision.model_validate(_valid_decision(confidence=confidence))


@pytest.mark.parametrize("confidence", [0, 1, 0.5])
def test_judge_confidence_accepts_json_numbers(confidence: int | float) -> None:
    decision = JudgeDecision.model_validate(_valid_decision(confidence=confidence))
    assert decision.confidence == confidence


def test_judge_protocol_requires_exact_keys_and_integer_schema_version() -> None:
    missing_array = _valid_decision()
    del missing_array["accepted_decisions"]
    with pytest.raises(ValidationError):
        JudgeDecision.model_validate(missing_array)

    with pytest.raises(ValidationError, match="integer 1"):
        JudgeDecision.model_validate(_valid_decision(schema_version="1"))

    with pytest.raises(ValidationError, match="extra_forbidden"):
        JudgeDecision.model_validate(_valid_decision(commentary="not allowed"))

    issue_extra = _valid_decision()
    issue_extra["unresolved_issues"][0]["extra"] = "not allowed"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        JudgeDecision.model_validate(issue_extra)


def test_judge_verdict_semantics_are_enforced() -> None:
    with pytest.raises(ValidationError, match="next_round_focus"):
        JudgeDecision.model_validate(_valid_decision(next_round_focus=[]))

    with pytest.raises(ValidationError, match="critical"):
        JudgeDecision.model_validate(
            _valid_decision(
                verdict="blocked",
                next_round_focus=[],
            )
        )

    critical_issue = [
        {
            "id": "ISSUE-CRITICAL",
            "severity": "critical",
            "summary": "A required authority decision is unavailable.",
        }
    ]
    blocked = JudgeDecision.model_validate(
        _valid_decision(
            verdict="blocked",
            unresolved_issues=critical_issue,
            next_round_focus=[],
        )
    )
    assert blocked.verdict is JudgeVerdict.BLOCKED

    with pytest.raises(ValidationError, match="finalize"):
        JudgeDecision.model_validate(
            _valid_decision(
                verdict="finalize",
                unresolved_issues=critical_issue,
                next_round_focus=[],
            )
        )


def test_judge_issue_ids_are_unique_and_array_items_nonblank() -> None:
    duplicate = _valid_decision()
    duplicate["unresolved_issues"].append(duplicate["unresolved_issues"][0].copy())
    with pytest.raises(ValidationError, match="ids must be unique"):
        JudgeDecision.model_validate(duplicate)

    with pytest.raises(ValidationError, match="blank"):
        JudgeDecision.model_validate(_valid_decision(accepted_decisions=[" "]))


def test_judge_arrays_are_ordered_and_decisions_are_immutable() -> None:
    with pytest.raises(ValidationError, match="ordered"):
        JudgeDecision.model_validate(_valid_decision(accepted_decisions={"one", "two"}))

    decision = JudgeDecision.model_validate(_valid_decision())
    with pytest.raises(ValidationError, match="frozen"):
        decision.verdict = JudgeVerdict.FINALIZE
    with pytest.raises(ValidationError, match="next_round_focus"):
        decision.model_copy(update={"next_round_focus": []})
    assert decision.verdict is JudgeVerdict.CONTINUE
    assert decision.next_round_focus


def test_run_and_round_summaries_are_json_native_and_ordered() -> None:
    started_at = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    decision = JudgeDecision.model_validate(
        _valid_decision(
            verdict="finalize",
            unresolved_issues=[],
            next_round_focus=[],
        )
    )
    round_summary = RoundSummary(
        round_number=1,
        status=RoundStatus.COMPLETED,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
        agent_results=[_valid_result()],
        judge_decision=decision,
    )
    summary = RunSummary(
        run_id="2026.07.26-run",
        status=RunStatus.FINALIZED,
        started_at=started_at,
        updated_at=started_at + timedelta(seconds=3),
        finished_at=started_at + timedelta(seconds=3),
        rounds=[round_summary],
        final_synthesis=decision.synthesis,
    )

    dumped = summary.to_dict()
    assert dumped["status"] == "finalized"
    assert dumped["rounds"][0]["status"] == "completed"
    assert dumped["rounds"][0]["agent_results"][0]["status"] == "success"
    assert isinstance(dumped["started_at"], str)

    timed_out = summary.model_copy(update={"status": RunStatus.TIMED_OUT})
    assert timed_out.model_dump(mode="json")["status"] == "timed_out"


def test_run_summary_rejects_duplicate_or_out_of_order_rounds() -> None:
    now = datetime(2026, 7, 26, tzinfo=UTC)
    rounds = [
        RoundSummary(round_number=2, status="completed", started_at=now),
        RoundSummary(round_number=1, status="completed", started_at=now),
    ]

    with pytest.raises(ValidationError, match="unique, increasing"):
        RunSummary(
            run_id="run-1",
            status="running",
            started_at=now,
            updated_at=now,
            rounds=rounds,
        )
