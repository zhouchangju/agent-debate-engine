from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_debate.models import JudgeDecision
from agent_debate.stop import StopOutcome, evaluate_stop


@dataclass(frozen=True)
class Policy:
    min_rounds: int = 2
    max_rounds: int = 5
    confidence_threshold: float = 0.8
    stable_rounds: int = 2
    max_elapsed_seconds: float = 60.0


def _decision(
    verdict: str = "finalize",
    *,
    confidence: float = 0.9,
) -> JudgeDecision:
    critical = (
        [
            {
                "id": "BLOCKED-001",
                "severity": "critical",
                "summary": "An authority decision is required.",
            }
        ]
        if verdict == "blocked"
        else []
    )
    return JudgeDecision.model_validate(
        {
            "schema_version": 1,
            "verdict": verdict,
            "confidence": confidence,
            "rationale": "Deterministic test decision.",
            "synthesis": "Use a reversible migration.",
            "accepted_decisions": [],
            "rejected_options": [],
            "unresolved_issues": critical,
            "next_round_focus": ["Measure rollback."] if verdict == "continue" else [],
        }
    )


def test_elapsed_limit_has_priority_over_model_verdict() -> None:
    result = evaluate_stop(
        round_number=3,
        elapsed_seconds=60.0,
        decision=_decision("blocked"),
        prior_decisions=(),
        policy=Policy(),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.TIMED_OUT
    assert "elapsed" in result.reason


def test_continue_at_final_round_is_exhausted_not_successful() -> None:
    result = evaluate_stop(
        round_number=5,
        elapsed_seconds=20,
        decision=_decision("continue", confidence=0.99),
        prior_decisions=(_decision(),),
        policy=Policy(),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.EXHAUSTED
    assert result.successful is False


def test_underqualified_finalize_at_final_round_is_exhausted() -> None:
    result = evaluate_stop(
        round_number=5,
        elapsed_seconds=20,
        decision=_decision(confidence=0.79),
        prior_decisions=(_decision(confidence=0.79),),
        policy=Policy(),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.EXHAUSTED


def test_qualified_finalize_at_final_round_remains_successful() -> None:
    current = _decision()
    result = evaluate_stop(
        round_number=5,
        elapsed_seconds=20,
        decision=current,
        prior_decisions=(current,),
        policy=Policy(),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.FINALIZED
    assert result.successful is True


def test_blocked_stops_immediately_even_before_min_rounds() -> None:
    result = evaluate_stop(
        round_number=1,
        elapsed_seconds=1,
        decision=_decision("blocked"),
        policy=Policy(),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.BLOCKED


def test_finalize_cannot_soft_stop_before_min_rounds() -> None:
    result = evaluate_stop(
        round_number=1,
        elapsed_seconds=1,
        decision=_decision(),
        prior_decisions=(_decision(),),
        policy=Policy(),
    )

    assert result.should_stop is False
    assert result.outcome is StopOutcome.CONTINUE
    assert "min_rounds" in result.reason


def test_finalize_requires_confidence_threshold() -> None:
    result = evaluate_stop(
        round_number=3,
        elapsed_seconds=1,
        decision=_decision(confidence=0.79),
        prior_decisions=(_decision(confidence=0.79),),
        policy=Policy(),
    )

    assert result.should_stop is False
    assert result.outcome is StopOutcome.CONTINUE
    assert "confidence" in result.reason


def test_finalize_requires_consecutive_qualifying_stable_rounds() -> None:
    result = evaluate_stop(
        round_number=3,
        elapsed_seconds=1,
        decision=_decision(),
        prior_decisions=(_decision("continue"),),
        policy=Policy(),
    )

    assert result.should_stop is False
    assert result.outcome is StopOutcome.CONTINUE
    assert "stable_rounds" in result.reason


def test_consecutive_qualifying_finalize_decisions_soft_stop() -> None:
    current = _decision()
    history = (current,)

    result = evaluate_stop(
        round_number=3,
        elapsed_seconds=1,
        decision=current,
        prior_decisions=history,
        policy=Policy(),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.FINALIZED
    assert result.successful is True
    assert history == (current,)


def test_critical_issue_prevents_soft_finalize_for_duck_typed_decision() -> None:
    invalid_finalize = SimpleNamespace(
        verdict="finalize",
        confidence=0.99,
        unresolved_issues=(SimpleNamespace(severity="critical"),),
    )
    result = evaluate_stop(
        round_number=3,
        elapsed_seconds=1,
        decision=invalid_finalize,
        prior_decisions=(invalid_finalize,),
        policy=Policy(),
    )

    assert result.should_stop is False
    assert result.outcome is StopOutcome.CONTINUE
    assert "critical" in result.reason


def test_mapping_shaped_critical_issue_also_prevents_soft_finalize() -> None:
    invalid_finalize = SimpleNamespace(
        verdict="finalize",
        confidence=0.99,
        unresolved_issues=({"severity": "critical"},),
    )

    result = evaluate_stop(
        round_number=3,
        elapsed_seconds=1,
        decision=invalid_finalize,
        prior_decisions=(invalid_finalize,),
        policy=Policy(),
    )

    assert result.should_stop is False
    assert result.outcome is StopOutcome.CONTINUE
    assert "critical" in result.reason


def test_stable_rounds_one_allows_first_qualified_finalize() -> None:
    result = evaluate_stop(
        round_number=1,
        elapsed_seconds=0,
        decision=_decision(),
        policy=Policy(min_rounds=1, stable_rounds=1),
    )

    assert result.should_stop is True
    assert result.outcome is StopOutcome.FINALIZED


@pytest.mark.parametrize(
    "policy",
    [
        Policy(min_rounds=0),
        Policy(min_rounds=3, max_rounds=2),
        Policy(confidence_threshold=float("nan")),
        Policy(confidence_threshold=1.1),
        Policy(stable_rounds=0),
        Policy(max_rounds=2, stable_rounds=3),
        Policy(max_elapsed_seconds=0),
    ],
)
def test_rejects_invalid_duck_typed_policy(policy: Policy) -> None:
    with pytest.raises(ValueError):
        evaluate_stop(1, 0, _decision(), policy=policy)
