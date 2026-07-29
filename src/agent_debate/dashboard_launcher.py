"""Best-effort launcher for the local debate dashboard."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote
from urllib.request import urlopen

_HOST = "127.0.0.1"
_PORT_RANGE = range(8765, 8786)


@dataclass(frozen=True, slots=True)
class DashboardLaunch:
    url: str
    reused: bool
    browser_opened: bool


def _artifact_root(run_dir: Path) -> Path:
    resolved = run_dir.expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == ".agent-debate":
            return parent
    return resolved.parent


def _health(port: int) -> dict[str, object] | None:
    try:
        with urlopen(f"http://{_HOST}:{port}/api/health", timeout=0.2) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _covers(health: dict[str, object], run_dir: Path) -> bool:
    roots = health.get("roots")
    if not isinstance(roots, list):
        return False
    resolved_run = run_dir.resolve()
    for value in roots:
        if not isinstance(value, str):
            continue
        try:
            resolved_run.relative_to(Path(value).resolve())
        except ValueError:
            continue
        return True
    return False


def _free_port() -> int:
    for port in _PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((_HOST, port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind((_HOST, 0))
        return int(candidate.getsockname()[1])


def _dashboard_environment() -> dict[str, str]:
    """Make the currently imported package visible to the detached child."""

    environment = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    existing = environment.get("PYTHONPATH")
    paths = existing.split(os.pathsep) if existing else []
    if package_root not in paths:
        paths.insert(0, package_root)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


def _startup_error(process: subprocess.Popen[bytes], diagnostics: BinaryIO) -> RuntimeError:
    exit_code = process.poll()
    if exit_code is None:
        process.terminate()
        try:
            exit_code = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=0.5)

    diagnostics.seek(0)
    stderr = diagnostics.read(8_192).decode("utf-8", errors="replace").strip()
    detail = f"Dashboard service did not become ready; process exited with status {exit_code}"
    if stderr:
        detail = f"{detail}: {stderr}"
    return RuntimeError(detail)


def open_run_dashboard(run_dir: Path) -> DashboardLaunch:
    """Reuse or launch a dashboard, then open the exact run in a browser."""

    resolved_run = run_dir.expanduser().resolve()
    for port in _PORT_RANGE:
        health = _health(port)
        if health is not None and _covers(health, resolved_run):
            url = f"http://{_HOST}:{port}/?run={quote(resolved_run.name)}"
            return DashboardLaunch(url, True, webbrowser.open(url))

    port = _free_port()
    root = _artifact_root(resolved_run)
    with tempfile.TemporaryFile() as diagnostics:
        process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "agent_debate.dashboard",
                "--root",
                str(root),
                "--port",
                str(port),
                "--no-browser",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=diagnostics,
            env=_dashboard_environment(),
            start_new_session=True,
            close_fds=True,
        )
        health = None
        for _ in range(30):
            time.sleep(0.1)
            health = _health(port)
            if health is not None:
                break
        if health is None or not _covers(health, resolved_run):
            raise _startup_error(process, diagnostics)
    url = f"http://{_HOST}:{port}/?run={quote(resolved_run.name)}"
    return DashboardLaunch(url, False, webbrowser.open(url))
