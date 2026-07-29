"""Deterministic Markdown reporting."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


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


def _fenced(text: str, language: str = "text") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text.rstrip()}\n{fence}"


def _read_json_artifact(
    read_artifact: Callable[[str], str],
    relative_path: str,
) -> Mapping[str, Any] | None:
    try:
        value = json.loads(read_artifact(relative_path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def render_evidence_report(
    manifest: Mapping[str, Any],
    read_artifact: Callable[[str], str],
) -> str:
    """Render one complete, reader-facing record of every model invocation."""

    invocations = manifest.get("invocations")
    invocation_rows = invocations if isinstance(invocations, list) else []
    details: list[tuple[Mapping[str, Any], Mapping[str, Any], str]] = []
    for item in invocation_rows:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        base = item["path"]
        meta = _read_json_artifact(read_artifact, f"{base}/meta.json") or {}
        details.append((item, meta, base))

    fresh_count = sum(meta.get("session_mode") == "fresh" for _, meta, _ in details)
    isolation = (
        "verified by adapter contracts"
        if details and fresh_count == len(details)
        else "contains unverified invocation(s)"
    )
    lines = [
        "# Agent debate evidence",
        "",
        f"- Run: `{manifest.get('run_id', 'unknown')}`",
        f"- Status: **{manifest.get('status', 'unknown')}**",
        f"- Stop reason: {manifest.get('stop_reason') or 'Not recorded.'}",
        f"- Completed rounds: {manifest.get('round_count', 0)}",
        f"- Invocations declaring fresh sessions: `{fresh_count}/{len(details)}`",
        f"- Session isolation: **{isolation}**",
        "",
        "## Role and model map",
        "",
        "| # | Round | Stage | Role | Agent | Adapter | Model | Session |",
        "|---:|---:|---|---|---|---|---|---|",
    ]
    for index, (item, meta, _base) in enumerate(details, start=1):
        lines.append(
            "| {index} | {round_number} | {stage} | {role} | {agent} | "
            "{adapter} | {model} | {session} |".format(
                index=index,
                round_number=item.get("round_number", "?"),
                stage=item.get("stage", "?"),
                role=meta.get("role_id", item.get("participant", "?")),
                agent=meta.get("agent_id", "?"),
                adapter=meta.get("provider_adapter", "unknown"),
                model=meta.get("provider_model") or "provider default",
                session=meta.get("session_mode", "unverified"),
            )
        )

    request_path = manifest.get("request_artifact", "request.md")
    try:
        request_text = read_artifact(str(request_path))
    except (OSError, UnicodeError):
        request_text = "Request artifact unavailable."
    lines.extend(["", "## Original task", "", _fenced(request_text, "markdown")])

    final_path = manifest.get("final_artifact")
    if isinstance(final_path, str):
        try:
            final_text = read_artifact(final_path)
        except (OSError, UnicodeError):
            final_text = "Final report artifact unavailable."
        lines.extend(["", "## Final report", "", final_text.rstrip()])

    lines.extend(["", "## Invocation transcript"])
    for index, (item, meta, base) in enumerate(details, start=1):
        role = meta.get("role_id", item.get("participant", "unknown"))
        agent = meta.get("agent_id", "unknown")
        lines.extend(
            [
                "",
                f"### {index}. {role} via {agent}",
                "",
                f"- Invocation: `{item.get('invocation_id', 'unknown')}`",
                f"- Round/stage: `{item.get('round_number', '?')}` / `{item.get('stage', '?')}`",
                f"- Adapter/model: `{meta.get('provider_adapter', 'unknown')}` / "
                f"`{meta.get('provider_model') or 'provider default'}`",
                f"- Status: `{meta.get('status', item.get('status', 'unknown'))}`",
                f"- Session mode: `{meta.get('session_mode', 'unverified')}`",
                f"- Session enforcement: {meta.get('session_enforcement', 'not declared')}",
                "",
                "#### Exact input",
                "",
                _fenced(read_artifact(f"{base}/request.md"), "markdown"),
                "",
                "#### Exact final output",
                "",
                _fenced(read_artifact(f"{base}/final.md"), "markdown"),
                "",
                "#### Raw stdout",
                "",
                _fenced(read_artifact(f"{base}/stdout.log")),
                "",
                "#### Raw stderr",
                "",
                _fenced(read_artifact(f"{base}/stderr.log")),
            ]
        )

    judges = manifest.get("judges")
    judge_rows = judges if isinstance(judges, list) else []
    if judge_rows:
        lines.extend(["", "## Structured Judge decisions"])
    for judge in judge_rows:
        if not isinstance(judge, dict) or not isinstance(judge.get("path"), str):
            continue
        relative = f"{judge['path']}/decision.json"
        try:
            payload = read_artifact(relative)
        except (OSError, UnicodeError):
            payload = json.dumps(judge, ensure_ascii=False, indent=2)
        lines.extend(
            [
                "",
                f"### Round {judge.get('round_number', '?')}",
                "",
                _fenced(payload, "json"),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
