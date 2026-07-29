from __future__ import annotations

import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
MANIFEST = REPOSITORY_ROOT / "MANIFEST.in"


def _workflow() -> dict[str, object]:
    value = yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def _job(workflow: dict[str, object], name: str) -> dict[str, object]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[name]
    assert isinstance(job, dict)
    return job


def _run_commands(job: dict[str, object]) -> set[str]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return {
        step["run"] for step in steps if isinstance(step, dict) and isinstance(step.get("run"), str)
    }


def test_ci_runs_required_quality_and_package_gates() -> None:
    workflow = _workflow()
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    assert {"push", "pull_request", "workflow_dispatch"} <= set(triggers)

    push = triggers["push"]
    assert isinstance(push, dict)
    assert push["tags"] == ["v*"]

    quality = _job(workflow, "quality")
    strategy = quality["strategy"]
    assert isinstance(strategy, dict)
    matrix = strategy["matrix"]
    assert isinstance(matrix, dict)
    assert matrix["python-version"] == ["3.11", "3.12", "3.13"]
    assert {
        "ruff format --check . && ruff check .",
        "mypy src",
        "python -m coverage erase && pytest --cov=agent_debate --cov-report=term-missing",
    } <= _run_commands(quality)

    package = _job(workflow, "package")
    assert "python -m build && python -m twine check dist/*" in _run_commands(package)


def test_ci_is_bounded_read_only_and_pins_external_actions() -> None:
    workflow = _workflow()
    permissions = workflow["permissions"]
    assert permissions == {"contents": "read"}

    for job_name in ("quality", "package"):
        job = _job(workflow, job_name)
        assert int(str(job["timeout-minutes"])) > 0
        steps = job["steps"]
        assert isinstance(steps, list)
        for step in steps:
            if not isinstance(step, dict) or "uses" not in step:
                continue
            action = step["uses"]
            assert isinstance(action, str)
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action)


def test_source_distribution_keeps_linked_public_documentation() -> None:
    manifest_lines = set(MANIFEST.read_text(encoding="utf-8").splitlines())
    assert {"include CODE_OF_CONDUCT.md", "include SUPPORT.md"} <= manifest_lines
    assert "recursive-include docs *.md *.html *.drawio *.png *.svg" in manifest_lines
