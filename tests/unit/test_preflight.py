from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_debate.errors import PreflightError
from agent_debate.preflight import (
    AgentDiagnostic,
    diagnose_agents,
    probe_version,
    require_healthy,
    resolve_executable,
)


@dataclass
class FakeConfig:
    adapter: str
    command: list[str]
    permission: str = "read_only"


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_executable_finds_absolute_python() -> None:
    assert resolve_executable([sys.executable]).samefile(sys.executable)


def test_resolve_executable_uses_runtime_working_directory(tmp_path: Path) -> None:
    executable = tmp_path / "version-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_executable(["./version-tool"], cwd=tmp_path).samefile(executable)


def test_resolve_executable_interprets_relative_path_from_runtime_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    executable = _write_executable(executable_directory / "version-tool", "exit 0\n")
    monkeypatch.setenv("PATH", "bin")

    assert resolve_executable(["version-tool"], cwd=tmp_path).samefile(executable)


def test_resolve_executable_rejects_missing_command() -> None:
    with pytest.raises(PreflightError, match="not on PATH"):
        resolve_executable(["definitely-not-an-agent-debate-command"])


def test_resolve_executable_does_not_expand_environment_or_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _write_executable(tmp_path / "agent", "exit 0\n")
    monkeypatch.setenv("AGENT_DEBATE_TEST_EXECUTABLE", str(executable))
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(PreflightError, match="not on PATH"):
        resolve_executable(["$AGENT_DEBATE_TEST_EXECUTABLE"], cwd=tmp_path)
    with pytest.raises(PreflightError, match="does not exist"):
        resolve_executable(["~/agent"], cwd=tmp_path)


@pytest.mark.asyncio
async def test_probe_version_reads_first_line() -> None:
    version = await probe_version([sys.executable, "-S"])
    assert version.startswith("Python ")


@pytest.mark.asyncio
async def test_probe_version_preserves_prefix_and_runtime_cwd(tmp_path: Path) -> None:
    script = tmp_path / "version_probe.py"
    script.write_text(
        "from pathlib import Path\nprint(Path.cwd())\nprint('ignored second line')\n",
        encoding="utf-8",
    )

    version = await probe_version(
        [sys.executable, "-S", script.name],
        cwd=tmp_path,
    )

    assert version == str(tmp_path)


@pytest.mark.asyncio
async def test_probe_version_timeout_terminates_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "probe-child-survived"
    script = tmp_path / "slow_version.py"
    script.write_text(
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-S',\n"
            "    '-c',\n"
            f'    "import time; from pathlib import Path; time.sleep(0.6); '
            f"Path({str(marker)!r}).write_text('alive')\",\n"
            "])\n"
            "time.sleep(60)\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(PreflightError, match="timed out"):
        await probe_version(
            [sys.executable, "-S", script.name],
            cwd=tmp_path,
            timeout_seconds=0.1,
        )

    await asyncio.sleep(0.7)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_probe_version_cancellation_propagates_and_cleans_up(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cancelled-probe-child-survived"
    script = tmp_path / "cancelled_version.py"
    script.write_text(
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-S',\n"
            "    '-c',\n"
            f'    "import time; from pathlib import Path; time.sleep(0.6); '
            f"Path({str(marker)!r}).write_text('alive')\",\n"
            "])\n"
            "time.sleep(60)\n"
        ),
        encoding="utf-8",
    )
    probe = asyncio.create_task(
        probe_version(
            [sys.executable, "-S", script.name],
            cwd=tmp_path,
            timeout_seconds=5,
        )
    )
    await asyncio.sleep(0.1)

    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    await asyncio.sleep(0.7)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_diagnose_reports_provider_warnings_without_running_generic(
    tmp_path: Path,
) -> None:
    kimi = _write_executable(tmp_path / "kimi", "printf '0.29.1\\n'\n")
    marker = tmp_path / "generic-was-executed"
    generic = _write_executable(
        tmp_path / "generic-agent",
        f"touch {marker}\nexit 0\n",
    )

    diagnostics = await diagnose_agents(
        {
            "kimi": FakeConfig(
                "kimi",
                [str(kimi)],
                "danger_full_access",
            ),
            "generic": FakeConfig(
                "generic",
                [str(generic), "--may-have-side-effects"],
                "workspace_write",
            ),
        },
        cwd=tmp_path,
    )

    assert all(item.ok for item in diagnostics)
    assert diagnostics[0].version == "0.29.1"
    assert any("danger_full_access" in warning for warning in diagnostics[0].warnings)
    assert any("auto-approves tools" in warning for warning in diagnostics[0].warnings)
    assert any("delegated" in warning for warning in diagnostics[1].warnings)
    assert any("workspace_write" in warning for warning in diagnostics[1].warnings)
    assert any("does not execute generic" in warning for warning in diagnostics[1].warnings)
    assert diagnostics[1].version is None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_generic_preflight_never_executes_configured_command(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "generic-probe-side-effect"
    executable = _write_executable(
        tmp_path / "no-version-contract",
        f"touch {marker}\nexit 17\n",
    )

    diagnostics = await diagnose_agents(
        {
            "generic": FakeConfig(
                "generic",
                [str(executable), "--version-causes-a-write"],
            ),
        },
        cwd=tmp_path,
        probe_timeout_seconds=0.5,
    )

    assert diagnostics[0].ok
    assert diagnostics[0].executable is not None
    assert diagnostics[0].version is None
    assert diagnostics[0].error is None
    assert any("side-effect-free version probe" in item for item in diagnostics[0].warnings)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_builtin_version_probe_failure_is_unhealthy(tmp_path: Path) -> None:
    executable = _write_executable(tmp_path / "codex", "exit 19\n")

    diagnostics = await diagnose_agents(
        {"codex": FakeConfig("codex", [str(executable)])},
        cwd=tmp_path,
    )

    assert not diagnostics[0].ok
    assert diagnostics[0].executable is not None
    assert diagnostics[0].version is None
    assert diagnostics[0].error is not None
    assert "status 19" in diagnostics[0].error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_version", "expected_error"),
    [
        ("not-codex 0.145.0", "Unsupported codex version response"),
        ("codex-cli 0.146.0", "expected the verified codex-cli 0.145.x contract"),
    ],
)
async def test_builtin_version_contract_fails_closed(
    tmp_path: Path,
    reported_version: str,
    expected_error: str,
) -> None:
    executable = _write_executable(
        tmp_path / "codex",
        f"printf '{reported_version}\\n'\n",
    )

    diagnostics = await diagnose_agents(
        {"codex": FakeConfig("codex", [str(executable)])},
        cwd=tmp_path,
    )

    assert not diagnostics[0].ok
    assert diagnostics[0].error is not None
    assert expected_error in diagnostics[0].error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        ["not-codex"],
        ["codex", "--profile", "untrusted"],
    ],
)
async def test_builtin_preflight_rejects_untrusted_name_or_prefix_without_execution(
    tmp_path: Path,
    command: list[str],
) -> None:
    marker = tmp_path / "builtin-probe-side-effect"
    executable = _write_executable(
        tmp_path / command[0],
        f"touch {marker}\nprintf 'codex-cli 0.145.0\\n'\n",
    )
    configured = [str(executable), *command[1:]]

    diagnostics = await diagnose_agents(
        {"codex": FakeConfig("codex", configured)},
        cwd=tmp_path,
    )

    assert not diagnostics[0].ok
    assert diagnostics[0].error is not None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_missing_generic_executable_is_unhealthy() -> None:
    diagnostics = await diagnose_agents(
        {
            "generic": FakeConfig(
                "generic",
                ["definitely-not-an-agent-debate-command"],
            ),
        }
    )

    assert not diagnostics[0].ok
    assert diagnostics[0].executable is None
    assert diagnostics[0].error is not None


def test_require_healthy_aggregates_errors() -> None:
    diagnostics = [
        AgentDiagnostic("missing", "generic", None, None, False, error="not found"),
    ]
    with pytest.raises(PreflightError, match="missing: not found"):
        require_healthy(diagnostics)
