from __future__ import annotations

from dataclasses import dataclass

from agent_debate.reporting import FinalReportData, render_final_report


@dataclass
class Issue:
    id: str
    severity: str
    summary: str


@dataclass
class Decision:
    verdict: str = "continue"
    confidence: float = 0.8
    rationale: str = "One material issue remains."
    synthesis: str = "Use the smallest reliable design."
    accepted_decisions: tuple[str, ...] = ("Use argv arrays.",)
    rejected_options: tuple[str, ...] = ()
    unresolved_issues: tuple[Issue, ...] = (Issue("risk-1", "major", "Needs evidence."),)
    next_round_focus: tuple[str, ...] = ("Test the risky boundary.",)


def test_non_final_report_is_explicitly_provisional() -> None:
    report = render_final_report(
        FinalReportData(
            run_id="run-1",
            status="exhausted",
            stop_reason="maximum rounds reached",
            round_count=3,
            request="Design it.",
            decision=Decision(),
        )
    )

    assert "Not converged" in report
    assert "latest provisional" in report
    assert "risk-1" in report


def test_missing_judge_decision_is_not_reported_as_success() -> None:
    report = render_final_report(
        FinalReportData("run-2", "failed", "judge protocol error", 1, "Design it.", None)
    )

    assert "No valid Judge decision" in report
    assert "Status: **failed**" in report
