# Repository Guidelines

A Python CLI for structured agent debates. Keep changes deterministic, focused, and diagnosable.

## Project Structure

- `src/agent_debate/` contains runtime code, provider adapters, the CLI and dashboard, schemas, and
  templates.
- `tests/unit/` and `tests/integration/` cover isolated behavior and orchestration; shared data
  belongs in `tests/fixtures/`.
- `examples/` holds configurations, `docs/` explains architecture and formats, and `skills/`
  contains agent-facing workflows.

## Build, Test, and Development Commands

- `make format` formats code and applies Ruff fixes.
- `make check` runs Ruff, strict mypy, and the coverage-enabled pytest suite.
- `make test` runs tests only; `make build` or `python -m build` creates distributions.
- Run locally with `agent-debate --help` after `pip install -e '.[dev]'`.

## Coding Style and Naming

Target Python 3.11+, use four-space indentation, full type annotations, and a 100-character line
limit. Ruff owns formatting and linting; mypy runs in strict mode. Use `snake_case` for modules and
functions and `PascalCase` for classes. Match nearby patterns instead of adding a one-use
abstraction.

## Working Principles

Think before coding: state material assumptions, expose ambiguity and tradeoffs, and ask when
different interpretations would change the result. Prefer the smallest implementation that meets
the request; avoid speculative features, configurability, and defensive branches for impossible
states.

Make surgical changes. Do not reformat, refactor, or remove unrelated code. Clean up only imports,
variables, or functions made obsolete by your change. Every changed line should trace to the
requested outcome.

Define success in verifiable terms. For bugs, reproduce the failure in a test before fixing it.
For refactors, establish passing checks before and after. For multi-step work, pair each step with a
specific check and continue until it passes.

## Testing and Safety

Name tests `test_<behavior>` and preserve at least 80% branch coverage. Tests must use fake
executables; real provider calls remain opt-in. Never use `shell=True` or interpolate user input
into commands. Workflows run stages sequentially; participants within a stage may run
concurrently. Treat prompts and responses as untrusted. Unsafe provider modes require explicit
configuration and runtime acknowledgement. Only the deterministic stop evaluator may declare
convergence. Every invocation must retain diagnostic metadata and artifacts.

## Commits and Pull Requests

Follow the repository’s Conventional Commit pattern, such as `feat:`, `fix:`, and `docs:`. Keep
commits focused. Pull requests should explain the motivation, summarize behavior changes, link
issues, and report `make check` results. Include screenshots for dashboard changes. Update
`README.md` and `docs/` whenever public behavior or architecture changes.
