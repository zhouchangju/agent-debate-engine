"""Deterministic executable used by integration tests.

This file intentionally uses only the standard library so it can be launched as a real child
process from a clean virtual environment.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="participant")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument(
        "--verdict",
        choices=("continue", "finalize", "blocked"),
        default="finalize",
    )
    parser.add_argument("--confidence", type=float, default=0.97)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stderr-chars", type=int, default=0)
    parser.add_argument("--prompt")
    parser.add_argument("--trace-file")
    parser.add_argument("--barrier-count", type=int, default=0)
    parser.add_argument("--fail-once-file")
    parser.add_argument("--invalid-judge-attempts", type=int, default=0)
    parser.add_argument("--invalid-judge-state-file")
    parser.add_argument("--fail-on-invocation", type=int, default=0)
    parser.add_argument("--invocation-state-file")
    return parser.parse_args()


def trace(path: str | None, event: str, identity: str) -> None:
    if path is None:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(f"{event}:{identity}:{time.time_ns()}\n")


def wait_for_trace_barrier(path: str | None, count: int) -> None:
    if path is None or count <= 0:
        return
    deadline = time.monotonic() + 5
    trace_path = Path(path)
    while time.monotonic() < deadline:
        try:
            lines = trace_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        if sum(line.startswith("start:") for line in lines) >= count:
            return
        time.sleep(0.01)
    raise TimeoutError(f"trace barrier did not reach {count} starts")


def main() -> int:
    args = parse_args()
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    trace(args.trace_file, "start", args.id)
    wait_for_trace_barrier(args.trace_file, args.barrier_count)
    if args.fail_once_file:
        marker = Path(args.fail_once_file)
        if not marker.exists():
            marker.write_text("failed", encoding="utf-8")
            return 23
    if args.invocation_state_file:
        state_path = Path(args.invocation_state_file)
        invocation = int(state_path.read_text(encoding="utf-8")) + 1 if state_path.exists() else 1
        state_path.write_text(str(invocation), encoding="utf-8")
        if invocation == args.fail_on_invocation:
            return 24
    if args.delay:
        time.sleep(args.delay)
    if args.stderr_chars:
        sys.stderr.write("e" * args.stderr_chars)
        sys.stderr.flush()
    if args.exit_code:
        return args.exit_code

    if args.judge:
        if args.invalid_judge_state_file:
            state_path = Path(args.invalid_judge_state_file)
            attempts = int(state_path.read_text(encoding="utf-8")) if state_path.exists() else 0
            state_path.write_text(str(attempts + 1), encoding="utf-8")
            if attempts < args.invalid_judge_attempts:
                sys.stdout.write("{invalid Judge JSON")
                sys.stdout.flush()
                return 0
        issue = (
            [{"id": "blocked", "severity": "critical", "summary": "Fixture is blocked."}]
            if args.verdict == "blocked"
            else []
        )
        decision = {
            "schema_version": 1,
            "verdict": args.verdict,
            "confidence": args.confidence,
            "rationale": "Deterministic fixture decision.",
            "synthesis": f"Fixture synthesis after observing {len(prompt)} prompt characters.",
            "accepted_decisions": ["Keep the workflow deterministic."],
            "rejected_options": [],
            "unresolved_issues": issue,
            "next_round_focus": (
                ["Resolve the remaining fixture issue."] if args.verdict == "continue" else []
            ),
        }
        sys.stdout.write(json.dumps(decision))
    else:
        markers = [
            marker
            for marker in ("proposal-a", "proposal-b", "critique", "revision")
            if marker in prompt
        ]
        sys.stdout.write(
            f"{args.id}\nPROMPT_CHARS={len(prompt)}\nOBSERVED={','.join(markers)}\n"
            f"fixture-response-{args.id}"
        )
    sys.stdout.flush()
    trace(args.trace_file, "end", args.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
