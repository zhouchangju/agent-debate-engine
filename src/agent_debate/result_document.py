"""Versioned, deterministic reader contract for one complete debate run."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

RESULT_SCHEMA_VERSION = 1


def _read_text(read_artifact: Callable[[str], str], path: str) -> str:
    try:
        return read_artifact(path)
    except Exception:
        return ""


def _read_object(
    read_artifact: Callable[[str], str],
    path: str,
) -> dict[str, Any]:
    try:
        value = json.loads(read_artifact(path))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _title(request: str) -> str:
    for line in request.splitlines():
        candidate = line.strip().lstrip("#-* ").strip()
        if candidate:
            return candidate[:160]
    return "Untitled debate"


def build_result_document(
    manifest: Mapping[str, Any],
    read_artifact: Callable[[str], str],
) -> dict[str, Any]:
    """Build the canonical v1 dashboard/export document from run artifacts."""

    request_path = str(manifest.get("request_artifact") or "request.md")
    request = _read_text(read_artifact, request_path)
    final_path = manifest.get("final_artifact")
    final_markdown = (
        _read_text(read_artifact, final_path)
        if isinstance(final_path, str)
        else ""
    )

    rounds: dict[int, dict[str, Any]] = {}
    roles: list[dict[str, Any]] = []
    role_keys: set[tuple[str, str]] = set()
    invocations = manifest.get("invocations")
    invocation_rows = invocations if isinstance(invocations, list) else []
    for sequence, item in enumerate(invocation_rows, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        base = item["path"]
        meta = _read_object(read_artifact, f"{base}/meta.json")
        round_number = item.get("round_number")
        if type(round_number) is not int or round_number < 1:
            continue
        role_id = str(meta.get("role_id") or item.get("participant") or "unknown")
        agent_id = str(meta.get("agent_id") or "unknown")
        adapter = str(meta.get("provider_adapter") or "unknown")
        model = meta.get("provider_model")
        role_key = (role_id, agent_id)
        if role_key not in role_keys:
            role_keys.add(role_key)
            roles.append(
                {
                    "role_id": role_id,
                    "agent_id": agent_id,
                    "adapter": adapter,
                    "model": model,
                }
            )

        round_record = rounds.setdefault(
            round_number,
            {
                "number": round_number,
                "invocations": [],
                "judge": None,
            },
        )
        round_record["invocations"].append(
            {
                "sequence": item.get("invocation_sequence", sequence),
                "invocation_id": item.get("invocation_id"),
                "stage": item.get("stage"),
                "role_id": role_id,
                "agent_id": agent_id,
                "adapter": adapter,
                "model": model,
                "status": meta.get("status") or item.get("status"),
                "attempt": item.get("attempt", 1),
                "session": {
                    "mode": meta.get("session_mode", "unverified"),
                    "enforcement": meta.get(
                        "session_enforcement",
                        "not declared",
                    ),
                },
                "timing": {
                    "started_at": meta.get("started_at"),
                    "finished_at": meta.get("finished_at"),
                    "duration_seconds": meta.get("duration_seconds"),
                },
                "execution": {
                    "exit_code": meta.get("exit_code"),
                    "timed_out": meta.get("timed_out", False),
                    "truncated": meta.get("truncated", False),
                    "display_command": meta.get("display_command", []),
                    "input_hash": meta.get("input_hash"),
                    "output_hash": meta.get("output_hash"),
                },
                "content": {
                    "input": _read_text(read_artifact, f"{base}/request.md"),
                    "output": _read_text(read_artifact, f"{base}/final.md"),
                    "stdout": _read_text(read_artifact, f"{base}/stdout.log"),
                    "stderr": _read_text(read_artifact, f"{base}/stderr.log"),
                },
                "artifacts": {
                    "base": base,
                    "request": f"{base}/request.md",
                    "output": f"{base}/final.md",
                    "stdout": f"{base}/stdout.log",
                    "stderr": f"{base}/stderr.log",
                    "meta": f"{base}/meta.json",
                },
            }
        )

    judges = manifest.get("judges")
    judge_rows = judges if isinstance(judges, list) else []
    for item in judge_rows:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        round_number = item.get("round_number")
        if type(round_number) is not int or round_number < 1:
            continue
        base = item["path"]
        round_record = rounds.setdefault(
            round_number,
            {
                "number": round_number,
                "invocations": [],
                "judge": None,
            },
        )
        round_record["judge"] = {
            "decision": _read_object(read_artifact, f"{base}/decision.json"),
            "raw": _read_text(read_artifact, f"{base}/raw.md"),
            "recorded_at": item.get("recorded_at"),
            "artifacts": {
                "request": f"{base}/request.md",
                "raw": f"{base}/raw.md",
                "decision": f"{base}/decision.json",
                "meta": f"{base}/meta.json",
            },
        }

    decision = manifest.get("final_decision")
    final_decision = decision if isinstance(decision, dict) else None
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run": {
            "id": manifest.get("run_id"),
            "title": _title(request),
            "status": manifest.get("status"),
            "outcome": manifest.get("outcome"),
            "stop_reason": manifest.get("stop_reason"),
            "round_count": manifest.get("round_count", len(rounds)),
            "invocation_count": sum(
                len(round_record["invocations"])
                for round_record in rounds.values()
            ),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "elapsed_seconds": manifest.get("elapsed_seconds"),
        },
        "request": {
            "markdown": request,
            "artifact": request_path,
        },
        "summary": {
            "final_markdown": final_markdown,
            "decision": final_decision,
        },
        "roles": roles,
        "rounds": [rounds[number] for number in sorted(rounds)],
        "artifacts": {
            "manifest": "manifest.json",
            "result": "result.json",
            "final": final_path,
            "evidence": manifest.get("evidence_artifact"),
        },
    }
