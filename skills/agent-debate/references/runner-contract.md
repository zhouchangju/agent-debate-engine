# Runner contract

The Skill runner is a deterministic adapter around the public
`agent_debate.engine` API. It prints one UTF-8 JSON object to stdout.

## Plan response

`plan` makes no provider calls:

```json
{
  "ok": true,
  "mode": "plan",
  "provider_calls": false,
  "preset": "technical-review",
  "depth": "standard",
  "workspace": "/absolute/workspace",
  "permission": "read_only",
  "agents": ["codex_primary", "codex_alternative"],
  "stages": ["proposals", "critique", "revision"],
  "max_rounds": 3,
  "max_elapsed_seconds": 900.0
}
```

## Run or resume response

Terminal engine states are valid results even when they did not converge:

```json
{
  "ok": true,
  "mode": "run",
  "preset": "technical-review",
  "depth": "standard",
  "status": "finalized",
  "converged": true,
  "run_id": "20260726T...",
  "run_dir": "/absolute/run",
  "rounds_completed": 2,
  "stop_reason": "convergence criteria satisfied",
  "final_report": "...",
  "manifest_path": "/absolute/run/manifest.json",
  "final_path": "/absolute/run/final.md"
}
```

`depth` is omitted for resume because the snapshotted configuration, not the
current conversation, controls resumed execution.

## Error response

Errors use a non-zero process exit and:

```json
{
  "ok": false,
  "error_type": "PreflightError",
  "error": "actionable message",
  "run_dir": "/absolute/run-if-one-was-created",
  "manifest_path": "/absolute/run-if-one-was-created/manifest.json"
}
```

`run_dir` and `manifest_path` are optional when failure happened before artifact
creation. Do not guess a run directory by selecting the newest global run.

## Recovery

- Resume only the exact `run_dir` returned by the runner or supplied by the
  user.
- Use `--retry-failed` only for an explicit retry request.
- Never edit `manifest.json`, hashes, Judge decisions, or invocation artifacts
  to force resume eligibility.
- If strict resume rejects the run, preserve it for inspection and report the
  integrity or lifecycle error.
