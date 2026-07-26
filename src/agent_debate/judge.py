"""Strict parsing for the Judge schema v1 wire protocol."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .errors import JudgeProtocolError
from .models import IssueSeverity, JudgeDecision, JudgeVerdict, UnresolvedIssue

_FENCED_OBJECT = re.compile(
    r"\A\s*```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*\s*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)
_JUDGE_V1_FIELDS = frozenset(
    {
        "schema_version",
        "verdict",
        "confidence",
        "rationale",
        "synthesis",
        "accepted_decisions",
        "rejected_options",
        "unresolved_issues",
        "next_round_focus",
    }
)


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"duplicate JSON object key: {key!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError(key)
        value[key] = item
    return value


_STRICT_DECODER = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)


def _advance_json_string(character: str, escaped: bool) -> tuple[bool, bool]:
    if escaped:
        return True, False
    if character == "\\":
        return True, True
    if character == '"':
        return False, False
    return True, False


def _json_object_candidates(text: str) -> list[dict[str, Any]]:
    """Return non-overlapping JSON objects using one linear brace scan."""

    candidates: list[dict[str, Any]] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if depth and in_string:
            in_string, escaped = _advance_json_string(character, escaped)
            continue
        if depth and character == '"':
            in_string = True
            continue
        if character == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if character != "}" or depth == 0:
            continue
        depth -= 1
        if depth != 0 or start is None:
            continue
        candidate_text = text[start : index + 1]
        try:
            value = _STRICT_DECODER.decode(candidate_text)
        except _DuplicateKeyError as exc:
            raise JudgeProtocolError(str(exc)) from exc
        except RecursionError as exc:
            raise JudgeProtocolError("Judge JSON nesting exceeds the parser limit") from exc
        except json.JSONDecodeError:
            start = None
            continue
        if isinstance(value, dict):
            candidates.append(value)
        start = None
    return candidates


def extract_judge_object(output: str) -> dict[str, Any]:
    """Extract exactly one top-level JSON object from a Judge response.

    The protocol tolerates the two common recoverable wrappers—one Markdown
    fence or surrounding prose—but never guesses when multiple objects exist.
    """

    if not isinstance(output, str):
        raise JudgeProtocolError("Judge output must be text")
    stripped = output.strip()
    if not stripped:
        raise JudgeProtocolError("Judge output is empty")

    fence_match = _FENCED_OBJECT.fullmatch(stripped)
    candidate_text = fence_match.group("body").strip() if fence_match else stripped

    try:
        whole_value = _STRICT_DECODER.decode(candidate_text)
    except _DuplicateKeyError as exc:
        raise JudgeProtocolError(str(exc)) from exc
    except RecursionError as exc:
        raise JudgeProtocolError("Judge JSON nesting exceeds the parser limit") from exc
    except json.JSONDecodeError:
        whole_value = None
    if isinstance(whole_value, dict):
        return whole_value
    if whole_value is not None:
        raise JudgeProtocolError("Judge output must contain a JSON object, not another JSON value")

    candidates = _json_object_candidates(candidate_text)
    if len(candidates) != 1:
        raise JudgeProtocolError(
            f"Judge output must contain exactly one JSON object; found {len(candidates)}"
        )
    return candidates[0]


def _validation_summary(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        details.append(f"{location}: {item['msg']}")
    return "; ".join(details)


def parse_judge_response(output: str) -> JudgeDecision:
    """Parse and semantically validate one Judge v1 response."""

    value = extract_judge_object(output)
    keys = frozenset(value)
    if keys != _JUDGE_V1_FIELDS:
        missing = sorted(_JUDGE_V1_FIELDS - keys)
        extra = sorted(keys - _JUDGE_V1_FIELDS)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise JudgeProtocolError(f"invalid Judge v1 object keys ({', '.join(details)})")

    try:
        return JudgeDecision.model_validate(value)
    except ValidationError as exc:
        raise JudgeProtocolError(f"invalid Judge v1 decision: {_validation_summary(exc)}") from exc


# Compatibility name for integrations that call provider text an "output".
parse_judge_output = parse_judge_response


__all__ = [
    "IssueSeverity",
    "JudgeDecision",
    "JudgeVerdict",
    "UnresolvedIssue",
    "extract_judge_object",
    "parse_judge_output",
    "parse_judge_response",
]
