from __future__ import annotations

import json
from typing import Any

import pytest

from agent_debate.errors import JudgeProtocolError
from agent_debate.judge import JudgeDecision, parse_judge_response


def _decision(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "verdict": "continue",
        "confidence": 0.75,
        "rationale": "The migration still needs a rollback measurement.",
        "synthesis": "Use a staged migration with a measured rollback.",
        "accepted_decisions": ["Keep the rollout reversible."],
        "rejected_options": ["Do not perform an irreversible cut-over."],
        "unresolved_issues": [
            {
                "id": "ISSUE-001",
                "severity": "major",
                "summary": "Rollback duration is not measured.",
            }
        ],
        "next_round_focus": ["Measure rollback duration."],
    }
    value.update(overrides)
    return value


def test_parses_pure_json_as_strict_judge_decision() -> None:
    decision = parse_judge_response(json.dumps(_decision()))

    assert isinstance(decision, JudgeDecision)
    assert decision.schema_version == 1
    assert decision.verdict == "continue"
    assert decision.unresolved_issues[0].id == "ISSUE-001"


@pytest.mark.parametrize("language", ["json", "JSON", ""])
def test_parses_one_markdown_fence(language: str) -> None:
    output = f"```{language}\n{json.dumps(_decision())}\n```"

    assert parse_judge_response(output).verdict == "continue"


def test_extracts_one_json_object_from_surrounding_noise() -> None:
    payload = _decision(
        rationale="Braces inside a string are data: {not another object}.",
    )
    output = f"Judge draft follows.\n{json.dumps(payload)}\nEnd of response."

    decision = parse_judge_response(output)

    assert decision.rationale == payload["rationale"]


def test_rejects_multiple_json_objects_instead_of_guessing() -> None:
    output = f"{json.dumps(_decision())}\n{json.dumps(_decision(confidence=0.9))}"

    with pytest.raises(JudgeProtocolError, match="exactly one JSON object"):
        parse_judge_response(output)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "There is no structured decision here.",
        '```json\n{"schema_version": 1\n```',
        "[]",
    ],
)
def test_rejects_missing_or_malformed_object(output: str) -> None:
    with pytest.raises(JudgeProtocolError):
        parse_judge_response(output)


def test_rejects_unknown_keys() -> None:
    payload = _decision(debug="not part of v1")

    with pytest.raises(JudgeProtocolError, match="Judge v1"):
        parse_judge_response(json.dumps(payload))


def test_rejects_duplicate_object_keys() -> None:
    ordinary = json.dumps(_decision())
    duplicated = '{"schema_version": 1,' + ordinary[1:]

    with pytest.raises(JudgeProtocolError, match="duplicate JSON object key"):
        parse_judge_response(duplicated)


def test_excessive_json_nesting_is_a_protocol_error() -> None:
    depth = 10_000
    output = '{"x":' * depth + "0" + "}" * depth

    with pytest.raises(JudgeProtocolError, match="nesting"):
        parse_judge_response(output)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"confidence": 1.01},
        {"confidence": "0.9"},
        {"rationale": "   "},
        {"next_round_focus": None},
    ],
)
def test_rejects_schema_violations(overrides: dict[str, Any]) -> None:
    with pytest.raises(JudgeProtocolError, match="Judge v1"):
        parse_judge_response(json.dumps(_decision(**overrides)))


def test_finalize_requires_nonempty_synthesis_and_no_critical_issue() -> None:
    critical = [
        {
            "id": "ISSUE-CRITICAL",
            "severity": "critical",
            "summary": "A destructive migration remains possible.",
        }
    ]

    with pytest.raises(JudgeProtocolError, match="Judge v1"):
        parse_judge_response(
            json.dumps(
                _decision(
                    verdict="finalize",
                    synthesis="   ",
                    unresolved_issues=[],
                    next_round_focus=[],
                )
            )
        )
    with pytest.raises(JudgeProtocolError, match="Judge v1"):
        parse_judge_response(
            json.dumps(
                _decision(
                    verdict="finalize",
                    unresolved_issues=critical,
                    next_round_focus=[],
                )
            )
        )


def test_continue_requires_next_round_focus() -> None:
    with pytest.raises(JudgeProtocolError, match="Judge v1"):
        parse_judge_response(json.dumps(_decision(next_round_focus=[])))


def test_blocked_requires_a_critical_issue() -> None:
    with pytest.raises(JudgeProtocolError, match="Judge v1"):
        parse_judge_response(
            json.dumps(
                _decision(
                    verdict="blocked",
                    unresolved_issues=[],
                    next_round_focus=[],
                )
            )
        )


def test_blocked_with_critical_issue_is_valid() -> None:
    payload = _decision(
        verdict="blocked",
        unresolved_issues=[
            {
                "id": "AUTH-001",
                "severity": "critical",
                "summary": "An owner decision is required.",
            }
        ],
        next_round_focus=[],
    )

    assert parse_judge_response(json.dumps(payload)).verdict == "blocked"
