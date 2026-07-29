# Sanitized decision-artifact walkthrough

This directory demonstrates the public shape of a decision package without making provider calls or
publishing a private `.agent-debate/` run.

- `task.md` is synthetic.
- `final.md` is an illustrative synthesis written for documentation.
- No text here is represented as a verbatim model response.
- No credentials, user repositories, provider logs, local paths, or private prompts are included.

Use the real engine when validating runtime behavior:

```bash
agent-debate validate --config examples/architecture-review/debate.yaml
agent-debate doctor --config examples/architecture-review/debate.yaml
agent-debate run \
  --config examples/architecture-review/debate.yaml \
  --task-file examples/sanitized-decision/task.md
```

The real run remains under `.agent-debate/` and must be reviewed and sanitized before any excerpt is
published. A terminal state of `exhausted`, `blocked`, `timed_out`, or `failed` must never be
rewritten as consensus.
