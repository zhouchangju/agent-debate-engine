# Codex + Kimi standard debate

| Role | Provider |
|---|---|
| Architect | Codex |
| Alternative | Kimi |
| Critic | Codex |
| Reviewer | Kimi |
| Judge | Codex |

Architect and Alternative use `independent_sequential`: both prompts are frozen
from the same pre-stage context, but provider processes run one at a time. Every
Codex call uses `codex exec --ephemeral`; every Kimi call starts a new prompt
session without resume/continue flags.

Kimi headless mode is write-capable. Run this only in an externally contained
workspace and acknowledge the unsafe provider explicitly:

```bash
agent-debate run \
  --config examples/codex-kimi-standard/debate.yaml \
  --task-file examples/codex-kimi-standard/task.md \
  --allow-unsafe
```

The run directory contains `final.md` and a complete `evidence.md` transcript.
