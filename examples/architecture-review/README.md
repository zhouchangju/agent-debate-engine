# Architecture review example

This example is self-contained: its configuration resolves the adjacent `requirement.md` and
`prompts/` files without relying on an installed package path.

From the repository root:

```bash
agent-debate validate --config examples/architecture-review/debate.yaml
agent-debate doctor --config examples/architecture-review/debate.yaml
agent-debate run \
  --config examples/architecture-review/debate.yaml \
  --task-file examples/architecture-review/requirement.md
```

The example deliberately defines two independent Codex profiles and omits `model`, so both use the
locally configured default model. Both profiles run with Codex's `read_only` sandbox and denied
approvals. The bundled safe example does not use Kimi: Kimi 0.29.1 headless prompt mode
auto-approves tools and is available only through the engine's explicit
`danger_full_access` configuration plus `--allow-unsafe`.
