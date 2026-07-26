"""Pure deterministic stopping rules for debate rounds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol


class StopPolicyLike(Protocol):
    """Configuration attributes consumed by :func:`evaluate_stop`."""

    @property
    def min_rounds(self) -> int: ...

    @property
    def max_rounds(self) -> int: ...

    @property
    def confidence_threshold(self) -> float: ...

    @property
    def stable_rounds(self) -> int: ...

    @property
    def max_elapsed_seconds(self) -> float: ...


class DecisionLike(Protocol):
    """Read-only Judge attributes needed by the stop evaluator."""

    @property
    def verdict(self) -> object: ...

    @property
    def confidence(self) -> float: ...

    @property
    def unresolved_issues(self) -> Sequence[object]: ...


class StopOutcome(StrEnum):
    """Possible evaluator outcomes."""

    CONTINUE = "continue"
    FINALIZED = "finalized"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class StopDecision:
    """Deterministic instruction returned to the orchestration engine."""

    should_stop: bool
    outcome: StopOutcome
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("reason must not be empty")
        if self.should_stop == (self.outcome is StopOutcome.CONTINUE):
            raise ValueError("should_stop must be false exactly for the continue outcome")

    @property
    def successful(self) -> bool:
        """Whether this stop represents validated convergence."""

        return self.outcome is StopOutcome.FINALIZED


@dataclass(frozen=True, slots=True)
class _Policy:
    min_rounds: int
    max_rounds: int
    confidence_threshold: float
    stable_rounds: int
    max_elapsed_seconds: float


def _validate_policy(policy: StopPolicyLike) -> _Policy:
    value = _Policy(
        min_rounds=policy.min_rounds,
        max_rounds=policy.max_rounds,
        confidence_threshold=policy.confidence_threshold,
        stable_rounds=policy.stable_rounds,
        max_elapsed_seconds=policy.max_elapsed_seconds,
    )
    if value.min_rounds < 1:
        raise ValueError("min_rounds must be at least 1")
    if value.max_rounds < value.min_rounds:
        raise ValueError("max_rounds must be greater than or equal to min_rounds")
    if not isfinite(value.confidence_threshold) or not (0 <= value.confidence_threshold <= 1):
        raise ValueError("confidence_threshold must be finite and between 0 and 1")
    if value.stable_rounds < 1:
        raise ValueError("stable_rounds must be at least 1")
    if value.stable_rounds > value.max_rounds:
        raise ValueError("stable_rounds must not exceed max_rounds")
    if not isfinite(value.max_elapsed_seconds) or value.max_elapsed_seconds <= 0:
        raise ValueError("max_elapsed_seconds must be finite and greater than zero")
    return value


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _verdict(decision: DecisionLike) -> str:
    return str(_enum_value(decision.verdict))


def _has_critical_issue(decision: DecisionLike) -> bool:
    for issue in decision.unresolved_issues:
        raw_severity = (
            issue.get("severity")
            if isinstance(issue, Mapping)
            else getattr(issue, "severity", None)
        )
        severity = _enum_value(raw_severity)
        if severity == "critical":
            return True
    return False


def _qualifies(decision: DecisionLike, confidence_threshold: float) -> bool:
    return (
        _verdict(decision) == "finalize"
        and decision.confidence >= confidence_threshold
        and not _has_critical_issue(decision)
    )


def _consecutive_qualifying_rounds(
    prior_decisions: Sequence[DecisionLike],
    decision: DecisionLike,
    confidence_threshold: float,
) -> int:
    count = 0
    for candidate in reversed((*prior_decisions, decision)):
        if not _qualifies(candidate, confidence_threshold):
            break
        count += 1
    return count


def _soft_finalize_reason(
    *,
    round_number: int,
    decision: DecisionLike,
    prior_decisions: Sequence[DecisionLike],
    policy: _Policy,
) -> str | None:
    """Return why finalize is not yet safe, or ``None`` when it qualifies."""

    if _verdict(decision) != "finalize":
        return "Judge verdict is continue; another bounded round is required"
    if round_number < policy.min_rounds:
        return f"min_rounds not reached ({round_number}/{policy.min_rounds})"
    if decision.confidence < policy.confidence_threshold:
        return (
            "Judge confidence is below confidence_threshold "
            f"({decision.confidence:.3f} < {policy.confidence_threshold:.3f})"
        )
    if _has_critical_issue(decision):
        return "a critical unresolved issue prevents finalization"

    stable = _consecutive_qualifying_rounds(
        prior_decisions,
        decision,
        policy.confidence_threshold,
    )
    if stable < policy.stable_rounds:
        return f"stable_rounds not reached ({stable}/{policy.stable_rounds})"
    return None


def evaluate_stop(
    round_number: int,
    elapsed_seconds: float,
    decision: DecisionLike,
    prior_decisions: Sequence[DecisionLike] = (),
    policy: StopPolicyLike | None = None,
) -> StopDecision:
    """Evaluate hard limits, blocked state, and soft convergence.

    This function has no I/O and mutates none of its inputs.  ``prior_decisions``
    excludes the current decision and is interpreted in chronological order.
    """

    if policy is None:
        raise TypeError("policy is required")
    limits = _validate_policy(policy)
    if round_number < 1:
        raise ValueError("round_number must be at least 1")
    if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must be finite and non-negative")
    if not isfinite(decision.confidence) or not (0 <= decision.confidence <= 1):
        raise ValueError("decision confidence must be finite and between 0 and 1")

    # Wall-clock exhaustion is independent of, and has priority over, a model
    # recommendation received at the boundary.
    if elapsed_seconds >= limits.max_elapsed_seconds:
        return StopDecision(
            should_stop=True,
            outcome=StopOutcome.TIMED_OUT,
            reason=(
                "maximum elapsed time reached "
                f"({elapsed_seconds:.3f}s/{limits.max_elapsed_seconds:.3f}s)"
            ),
        )

    verdict = _verdict(decision)
    soft_failure = _soft_finalize_reason(
        round_number=round_number,
        decision=decision,
        prior_decisions=prior_decisions,
        policy=limits,
    )

    if verdict == "blocked":
        return StopDecision(
            should_stop=True,
            outcome=StopOutcome.BLOCKED,
            reason="Judge reported a critical blocking issue",
        )

    # The final round may still produce a genuinely qualifying finalize or a
    # valid blocked decision.  Every other result is explicit exhaustion.
    if round_number >= limits.max_rounds:
        if soft_failure is None:
            return StopDecision(
                should_stop=True,
                outcome=StopOutcome.FINALIZED,
                reason="Judge finalize decision satisfies all deterministic stop criteria",
            )
        return StopDecision(
            should_stop=True,
            outcome=StopOutcome.EXHAUSTED,
            reason=(f"maximum rounds reached ({round_number}/{limits.max_rounds}); {soft_failure}"),
        )

    if soft_failure is None:
        return StopDecision(
            should_stop=True,
            outcome=StopOutcome.FINALIZED,
            reason="Judge finalize decision satisfies all deterministic stop criteria",
        )
    return StopDecision(
        should_stop=False,
        outcome=StopOutcome.CONTINUE,
        reason=soft_failure,
    )


# Compatibility name for callers that prefer noun-oriented evaluator APIs.
evaluate_stopping = evaluate_stop


__all__ = [
    "DecisionLike",
    "StopDecision",
    "StopOutcome",
    "StopPolicyLike",
    "evaluate_stop",
    "evaluate_stopping",
]
