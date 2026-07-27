from __future__ import annotations

import json
from pathlib import Path

from agent_debate.adapters.base import CommandSpec
from agent_debate.artifacts import ArtifactStore
from agent_debate.config import StageConfig, StageMode
from agent_debate.reporting import render_evidence_report


def test_independent_sequential_is_a_valid_stage_mode() -> None:
    stage = StageConfig.model_validate(
        {
            "id": "proposals",
            "mode": "independent_sequential",
            "participants": [
                {
                    "id": "architect",
                    "agent": "codex_architect",
                    "prompt": "prompts/architect.md",
                }
            ],
        }
    )

    assert stage.mode is StageMode.INDEPENDENT_SEQUENTIAL


def test_command_spec_session_metadata_is_backward_compatible(tmp_path: Path) -> None:
    spec = CommandSpec(
        argv=("agent",),
        display_argv=("agent",),
        cwd=tmp_path,
    )

    assert spec.session_mode == "unverified"
    assert spec.provider_adapter == "unknown"


def test_complete_evidence_report_contains_exact_model_io() -> None:
    base = "rounds/001/proposals/architect/invocation-000001"
    artifacts = {
        "request.md": "Choose A or B.",
        "final.md": "# Decision\n\nChoose A.",
        f"{base}/request.md": "ARCHITECT INPUT",
        f"{base}/final.md": "ARCHITECT OUTPUT",
        f"{base}/stdout.log": "RAW STDOUT",
        f"{base}/stderr.log": "RAW STDERR",
        f"{base}/meta.json": json.dumps(
            {
                "agent_id": "codex_architect",
                "role_id": "architect",
                "status": "succeeded",
                "provider_adapter": "codex",
                "provider_model": "gpt-5.6-sol",
                "session_mode": "fresh",
                "session_enforcement": "codex exec --ephemeral",
            }
        ),
    }
    manifest = {
        "run_id": "run-1",
        "status": "finalized",
        "stop_reason": "stable decision",
        "round_count": 1,
        "request_artifact": "request.md",
        "final_artifact": "final.md",
        "invocations": [
            {
                "round_number": 1,
                "stage": "proposals",
                "participant": "architect",
                "invocation_id": "invocation-000001",
                "path": base,
                "status": "succeeded",
            }
        ],
        "judges": [],
    }

    report = render_evidence_report(manifest, artifacts.__getitem__)

    assert "gpt-5.6-sol" in report
    assert "ARCHITECT INPUT" in report
    assert "ARCHITECT OUTPUT" in report
    assert "RAW STDOUT" in report
    assert "RAW STDERR" in report
    assert "verified by adapter contracts" in report


def test_store_writes_canonical_evidence_artifact(tmp_path: Path) -> None:
    with ArtifactStore.create(tmp_path, {"schema_version": 1}, "task") as store:
        store.write_evidence("# Evidence\n")

        assert store.read_artifact_text("evidence.md") == "# Evidence\n"
        assert store.manifest["evidence_artifact"] == "evidence.md"
