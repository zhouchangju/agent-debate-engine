from __future__ import annotations

import json
from pathlib import Path

from agent_debate.dashboard import DashboardRepository
from agent_debate.result_document import build_result_document


def _fixture() -> tuple[dict[str, object], dict[str, str]]:
    base = "rounds/001/proposals/architect/call-1"
    manifest: dict[str, object] = {
        "run_id": "run-1",
        "status": "finalized",
        "outcome": "finalized",
        "stop_reason": "stable",
        "round_count": 1,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
        "elapsed_seconds": 60.0,
        "request_artifact": "request.md",
        "final_artifact": "final.md",
        "final_decision": {
            "verdict": "finalize",
            "confidence": 0.9,
            "synthesis": "Choose A.",
            "unresolved_issues": [],
        },
        "invocations": [
            {
                "round_number": 1,
                "stage": "proposals",
                "participant": "architect",
                "invocation_id": "call-1",
                "path": base,
                "status": "success",
                "attempt": 1,
                "invocation_sequence": 1,
            }
        ],
        "judges": [
            {
                "round_number": 1,
                "path": "rounds/001/judge",
                "recorded_at": "2026-01-01T00:01:00Z",
            }
        ],
    }
    artifacts = {
        "request.md": "# Architecture choice\n\nChoose A or B.",
        "final.md": "# Result\n\nChoose A.",
        f"{base}/request.md": "EXACT INPUT",
        f"{base}/final.md": "EXACT OUTPUT",
        f"{base}/stdout.log": "RAW STDOUT",
        f"{base}/stderr.log": "",
        f"{base}/meta.json": json.dumps(
            {
                "agent_id": "codex_architect",
                "role_id": "architect",
                "provider_adapter": "codex",
                "provider_model": "gpt-5.6-sol",
                "session_mode": "fresh",
                "session_enforcement": "codex exec --ephemeral",
                "status": "success",
            }
        ),
        "rounds/001/judge/decision.json": json.dumps(
            {"verdict": "finalize", "confidence": 0.9}
        ),
        "rounds/001/judge/raw.md": '{"verdict":"finalize"}',
    }
    return manifest, artifacts


def test_result_document_normalizes_rounds_roles_and_content() -> None:
    manifest, artifacts = _fixture()

    document = build_result_document(manifest, artifacts.__getitem__)

    assert document["schema_version"] == 1
    assert document["run"]["title"] == "Architecture choice"
    assert document["run"]["invocation_count"] == 1
    assert document["roles"] == [
        {
            "role_id": "architect",
            "agent_id": "codex_architect",
            "adapter": "codex",
            "model": "gpt-5.6-sol",
        }
    ]
    invocation = document["rounds"][0]["invocations"][0]
    assert invocation["content"]["input"] == "EXACT INPUT"
    assert invocation["content"]["output"] == "EXACT OUTPUT"
    assert invocation["session"]["mode"] == "fresh"
    assert document["rounds"][0]["judge"]["decision"]["verdict"] == "finalize"


def test_dashboard_adapts_legacy_manifest_without_result_json(tmp_path: Path) -> None:
    manifest, artifacts = _fixture()
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    for relative, content in artifacts.items():
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    repository = DashboardRepository([tmp_path])

    summaries = repository.list_runs()
    document = repository.get_run("run-1")
    assert summaries[0]["title"] == "Architecture choice"
    assert summaries[0]["providers"] == ["codex"]
    assert document is not None
    assert document["summary"]["decision"]["verdict"] == "finalize"
