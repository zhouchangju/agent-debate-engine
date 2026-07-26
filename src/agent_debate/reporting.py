"""Deterministic Markdown reporting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class IssueLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def severity(self) -> object: ...

    @property
    def summary(self) -> str: ...


class DecisionLike(Protocol):
    @property
    def verdict(self) -> object: ...

    @property
    def confidence(self) -> float: ...

    @property
    def rationale(self) -> str: ...

    @property
    def synthesis(self) -> str: ...

    @property
    def accepted_decisions(self) -> Sequence[str]: ...

    @property
    def rejected_options(self) -> Sequence[str]: ...

    @property
    def unresolved_issues(self) -> Sequence[IssueLike]: ...

    @property
    def next_round_focus(self) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class FinalReportData:
    run_id: str
    status: str
    stop_reason: str
    round_count: int
    request: str
    decision: DecisionLike | None


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _list_section(title: str, items: Sequence[str], *, empty: str = "None recorded.") -> str:
    body = "\n".join(f"- {item}" for item in items) if items else empty
    return f"## {title}\n\n{body}"


def render_final_report(data: FinalReportData) -> str:
    """Render a reader-facing report without claiming false convergence."""

    lines = [
        "# Agent debate result",
        "",
        f"- Run: `{data.run_id}`",
        f"- Status: **{data.status}**",
        f"- Stop reason: {data.stop_reason}",
        f"- Completed rounds: {data.round_count}",
        "",
        "## Request",
        "",
        data.request.strip(),
    ]

    decision = data.decision
    if decision is None:
        lines.extend(
            [
                "",
                "## Result",
                "",
                "No valid Judge decision was produced. Inspect the invocation artifacts and stderr "
                "before retrying.",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    if data.status != "finalized":
        lines.extend(
            [
                "",
                "> **Not converged.** The synthesis below is the latest provisional Judge output, "
                "not a consensus claim.",
            ]
        )

    lines.extend(
        [
            "",
            "## Current best synthesis",
            "",
            decision.synthesis.strip(),
            "",
            "## Judge assessment",
            "",
            f"- Verdict: `{_enum_value(decision.verdict)}`",
            f"- Confidence: `{decision.confidence:.3f}`",
            f"- Rationale: {decision.rationale.strip()}",
            "",
            _list_section("Accepted decisions", decision.accepted_decisions),
            "",
            _list_section("Rejected options", decision.rejected_options),
            "",
            "## Unresolved issues",
            "",
        ]
    )

    if decision.unresolved_issues:
        lines.extend(
            f"- **{_enum_value(issue.severity)}** `{issue.id}` — {issue.summary}"
            for issue in decision.unresolved_issues
        )
    else:
        lines.append("None recorded.")

    lines.extend(
        [
            "",
            _list_section("Suggested next-round focus", decision.next_round_focus),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
