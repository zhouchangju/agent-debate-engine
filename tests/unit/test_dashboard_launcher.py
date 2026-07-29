from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from agent_debate import dashboard_launcher


def test_dashboard_environment_exposes_current_source_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(("/existing/one", "/existing/two")))

    environment = dashboard_launcher._dashboard_environment()

    paths = environment["PYTHONPATH"].split(os.pathsep)
    assert paths[0] == str(Path(dashboard_launcher.__file__).resolve().parents[1])
    assert paths[1:] == ["/existing/one", "/existing/two"]


def test_dashboard_startup_failure_reports_child_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / ".agent-debate" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}

    class FailedProcess:
        def poll(self) -> int:
            return 1

    def fake_popen(*args: object, **kwargs: object) -> FailedProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        diagnostics = kwargs["stderr"]
        diagnostics.write(b"No module named 'agent_debate'\n")
        diagnostics.flush()
        return FailedProcess()

    monkeypatch.setattr(dashboard_launcher, "_PORT_RANGE", range(20_001, 20_002))
    monkeypatch.setattr(dashboard_launcher, "_health", lambda _port: None)
    monkeypatch.setattr(dashboard_launcher, "_free_port", lambda: 20_001)
    monkeypatch.setattr(dashboard_launcher.subprocess, "Popen", fake_popen)

    with pytest.raises(
        RuntimeError,
        match="process exited with status 1: No module named 'agent_debate'",
    ):
        dashboard_launcher.open_run_dashboard(run_dir)

    environment = captured["kwargs"]["env"]
    assert str(Path(dashboard_launcher.__file__).resolve().parents[1]) in environment["PYTHONPATH"]
