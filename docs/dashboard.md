# Local debate dashboard

The dashboard is a dependency-free, read-only browser for local debate history.
It recursively discovers run directories below the explicit artifact root,
prefers the versioned `result.json` contract, and adapts older v1 manifests in
memory when that file is absent.

## Start

From the repository root:

```bash
uv run agent-debate-dashboard --root .agent-debate
```

The default address is `http://127.0.0.1:8765/`. The command opens the browser
unless `--no-browser` is supplied.

Repeat `--root` to combine histories stored in different locations:

```bash
uv run agent-debate-dashboard \
  --root .agent-debate \
  --root /path/to/another/private/run-root
```

The service refuses non-loopback binding unless `--allow-remote` is supplied.
Prompts, model outputs, and logs can contain private repository information, so
remote exposure is intentionally not the default.

## Reader contract

New terminal runs write:

- `result.json`: canonical machine-readable result, schema version 1;
- `final.md`: compact reader-facing conclusion;
- `evidence.md`: complete Markdown transcript;
- `manifest.json`: integrity index and resumable engine state.

The JSON Schema is
`src/agent_debate/schemas/result-v1.json`. The document groups invocations by
round and records role, agent, adapter, model, fresh-session contract, timing,
exact input, final output, stdout, stderr, hashes, and the structured Judge
decision.

Dashboard APIs:

- `GET /api/health`: service identity and covered roots;
- `GET /api/runs`: history summaries;
- `GET /api/runs/<run-id>`: complete normalized result;
- `GET /api/schema`: `result-v1` JSON Schema.

## Skill integration

The bundled natural-language Skill invokes `run` and `resume` with
`--open-dashboard`. After a terminal result, the runner reuses a compatible
local dashboard or starts one in the background, opens a deep link to the exact
run, and returns `dashboard_url`, `dashboard_opened`, and `dashboard_reused` in
its single JSON response. Direct CLI and runner consumers remain side-effect
free unless they explicitly pass the flag.
