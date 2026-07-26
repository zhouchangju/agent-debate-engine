# Safety boundary

The Skill is intentionally narrower than the underlying engine.

## Allowed automatically

- Bundled `technical-review` preset.
- Codex CLI verified by the engine's built-in preflight contract.
- `read_only` permission with denied approvals.
- Repository inspection and analysis-only prompts.
- `quick`, `standard`, and `deep` bounded deliberation budgets.

## Not allowed automatically

- Kimi headless prompt mode, because the verified version auto-approves tools.
- Generic commands, because the engine cannot enforce a portable sandbox.
- `workspace_write` or `danger_full_access`.
- Shell wrappers or provider argv overrides.
- Silent installation, authentication changes, or credential collection.

If the user needs one of these, explain that it requires the explicit YAML
interface, the engine's separate unsafe acknowledgement, and an externally
enforced sandbox such as a container, VM, or restricted account. The
natural-language request does not erase those boundaries.

## Provider cost

`plan` and local validation do not call a model. `run` and `resume` can make
multiple provider calls. Explicitly asking for a multi-agent debate is
sufficient authorization for the safe preset, but an ambiguous single-agent
analysis request is not.

## Evidence limits

Multiple roles using the same Codex installation are not statistically or
organizationally independent. Report them as independent role invocations with
separate prompts and evidence, not as independent ground truth.
