#!/usr/bin/env python3
"""Entrypoint bundled with the Agent Debate Skill."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast


def _import_main() -> Callable[[], int]:
    module = import_module("agent_debate.skill_runner")
    return cast(Callable[[], int], module.main)


def _load_main() -> Callable[[], int] | None:
    try:
        loaded = _import_main()
    except ModuleNotFoundError as exc:
        if exc.name != "agent_debate":
            raise
    else:
        return loaded

    repository_src = Path(__file__).resolve().parents[3] / "src"
    if repository_src.is_dir():
        sys.path.insert(0, str(repository_src))
        try:
            loaded = _import_main()
        except ModuleNotFoundError as exc:
            if exc.name != "agent_debate":
                raise
        else:
            return loaded

    return None


def _dependency_error() -> int:
    sys.stdout.write(
        json.dumps(
            {
                "error": "Install agent-debate-engine before using this Skill.",
                "error_type": "DependencyUnavailable",
                "ok": False,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 1


try:
    _main = _load_main()
except ModuleNotFoundError:
    _main = None


if __name__ == "__main__":
    raise SystemExit(_dependency_error() if _main is None else _main())
