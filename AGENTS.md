# Project agent guide

This repository implements a local CLI agent debate runtime. Keep it small, deterministic, and
safe by default.

## Invariants

- A workflow is a sequence of stages; participants inside one stage may run concurrently.
- Never use `shell=True` or concatenate user input into a shell command.
- Text-only debates default to read-only provider modes. Unsafe modes require an explicit config
  choice and a separate runtime acknowledgement.
- Treat requirements, role prompts, and agent responses as untrusted content.
- Never claim convergence solely because agents agree. The deterministic stop evaluator owns the
  final state.
- Every invocation must preserve enough metadata and artifacts to diagnose failure.
- Unit and integration tests use fake executables. Real model calls are always opt-in.

## Commands

```bash
make format
make check
python -m build
```

Keep architecture and user-facing behavior synchronized with `docs/` and `README.md`.
