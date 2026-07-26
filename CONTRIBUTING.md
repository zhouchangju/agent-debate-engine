# Contributing

Agent Debate Engine is intentionally a small runtime rather than a general-purpose agent
framework. Changes should preserve the invariants in `AGENTS.md`.

Version 0.1 officially supports POSIX platforms (Linux and macOS). Windows behavior is outside the
release contract; adding it requires explicit process-tree, locking, link-safety, permission, and
durability designs with platform-specific tests.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
make check
```

For a lockfile-reproducible environment, use `uv sync --all-extras --locked` instead. Update
`uv.lock` whenever dependency constraints change.

Tests must not invoke paid models or rely on installed Codex/Kimi credentials. Use fake executable
fixtures for adapter and orchestration behavior. Add a regression test for every bug fix.

Before opening a change, run:

```bash
make format
make check
python -m build
```

Security-sensitive changes—process execution, permissions, environment handling, artifact
redaction, resume validation, locking, provider scratch space, or prompt transport—need an explicit
threat-oriented test. Preserve immutable invocation attempts and keep built-in provider argv
controls closed to configuration/profile/feature overrides. Generic commands must remain
unsafe-acknowledged and sequential unless a future design introduces an engine-verifiable external
containment contract.
