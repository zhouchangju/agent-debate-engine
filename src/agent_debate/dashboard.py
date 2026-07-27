"""Local, read-only dashboard for versioned debate result documents."""

from __future__ import annotations

import argparse
import json
import re
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agent_debate.dashboard_ui import DASHBOARD_HTML
from agent_debate.result_document import RESULT_SCHEMA_VERSION, build_result_document

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class RunEntry:
    key: str
    run_dir: Path
    manifest: dict[str, Any]


class DashboardRepository:
    """Contained reader over one or more explicit artifact roots."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = tuple(root.expanduser().resolve() for root in roots)

    def _entries(self) -> list[RunEntry]:
        entries: list[RunEntry] = []
        seen: set[str] = set()
        for root in self.roots:
            if not root.is_dir():
                continue
            for manifest_path in root.rglob("manifest.json"):
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    continue
                run_dir = manifest_path.parent.resolve()
                if not _contained(root, run_dir):
                    continue
                try:
                    manifest = _read_json(manifest_path)
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    continue
                run_id = manifest.get("run_id")
                if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
                    continue
                key = run_id
                suffix = 2
                while key in seen:
                    key = f"{run_id}.{suffix}"
                    suffix += 1
                seen.add(key)
                entries.append(RunEntry(key, run_dir, manifest))
        return sorted(
            entries,
            key=lambda entry: str(entry.manifest.get("started_at") or ""),
            reverse=True,
        )

    @staticmethod
    def _artifact_reader(entry: RunEntry):
        run_root = entry.run_dir.resolve()

        def read(relative: str) -> str:
            candidate = (run_root / relative).resolve()
            if not _contained(run_root, candidate):
                raise ValueError("artifact path escapes run directory")
            if candidate.is_symlink() or not candidate.is_file():
                raise FileNotFoundError(relative)
            return candidate.read_text(encoding="utf-8")

        return read

    def _document(self, entry: RunEntry) -> dict[str, Any]:
        result_path = entry.run_dir / "result.json"
        if result_path.is_file() and not result_path.is_symlink():
            document = _read_json(result_path)
            if document.get("schema_version") == RESULT_SCHEMA_VERSION:
                return document
        return build_result_document(
            entry.manifest,
            self._artifact_reader(entry),
        )

    def list_runs(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for entry in self._entries():
            try:
                document = self._document(entry)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            run = document.get("run")
            summary = document.get("summary")
            request = document.get("request")
            run_data = run if isinstance(run, dict) else {}
            summary_data = summary if isinstance(summary, dict) else {}
            request_data = request if isinstance(request, dict) else {}
            decision = summary_data.get("decision")
            decision_data = decision if isinstance(decision, dict) else {}
            request_text = str(request_data.get("markdown") or "")
            summaries.append(
                {
                    **run_data,
                    "key": entry.key,
                    "request_preview": request_text[:240],
                    "verdict": decision_data.get("verdict"),
                    "confidence": decision_data.get("confidence"),
                    "unresolved_count": len(
                        decision_data.get("unresolved_issues", [])
                        if isinstance(decision_data.get("unresolved_issues"), list)
                        else []
                    ),
                    "providers": sorted(
                        {
                            str(role.get("adapter"))
                            for role in document.get("roles", [])
                            if isinstance(role, dict) and role.get("adapter")
                        }
                    ),
                }
            )
        return summaries

    def get_run(self, key: str) -> dict[str, Any] | None:
        for entry in self._entries():
            if entry.key == key:
                return self._document(entry)
        return None


class DashboardHandler(BaseHTTPRequestHandler):
    repository: DashboardRepository
    server_version = "AgentDebateDashboard/1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_bytes(
                DASHBOARD_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if parsed.path == "/api/runs":
            self._send_json({"schema_version": 1, "runs": self.repository.list_runs()})
            return
        if parsed.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "agent-debate-dashboard",
                    "roots": [str(root) for root in self.repository.roots],
                }
            )
            return
        if parsed.path.startswith("/api/runs/"):
            key = unquote(parsed.path.removeprefix("/api/runs/"))
            if _RUN_ID_RE.fullmatch(key) is None:
                self._send_json({"error": "invalid run id"}, HTTPStatus.BAD_REQUEST)
                return
            document = self.repository.get_run(key)
            if document is None:
                self._send_json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(document)
            return
        if parsed.path == "/api/schema":
            schema_path = Path(__file__).with_name("schemas") / "result-v1.json"
            self._send_bytes(
                schema_path.read_bytes(),
                "application/schema+json; charset=utf-8",
            )
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Browse local Agent Debate Engine run history.",
    )
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        help="Artifact root to scan recursively; repeat for multiple roots.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Required before binding to a non-loopback host.",
    )
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_remote:
        raise SystemExit(
            "Refusing to expose private debate artifacts remotely without --allow-remote"
        )
    roots = args.root or [Path.cwd() / ".agent-debate"]
    repository = DashboardRepository(roots)
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {"repository": repository},
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"Agent Debate Dashboard: {url}")
    print("Roots:")
    for root in repository.roots:
        print(f"  {root}")
    if not args.no_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
