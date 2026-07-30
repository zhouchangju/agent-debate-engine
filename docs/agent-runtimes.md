# Configuring agent runtimes

An agent runtime is the local CLI process used for a debate role. Configure runtimes under
`agents` in `debate.yaml`, then reference those profile names from workflow participants and the
Judge.

The engine supports:

| Adapter | Use it for | Engine-enforced permission |
|---|---|---|
| `codex` | Codex CLI | `read_only`, `workspace_write`, or `danger_full_access` |
| `kimi` | Kimi Code CLI 0.29.1 | `danger_full_access` only |
| `generic` | Another non-interactive argv-based CLI or trusted wrapper | None; the permission is audit metadata |

Provider installation, authentication, account access, and model availability remain owned by the
local CLI. Agent Debate Engine does not store provider API keys.

## Configure a runtime

Generate a starting workflow:

```bash
agent-debate init
```

Edit an entry under `agents`:

```yaml
agents:
  codex_architect:
    adapter: codex
    command: [codex]
    model: gpt-5.6-sol
    model_reasoning_effort: medium
    permission: read_only
    timeout: 600
    max_output: 100000
    max_final_output: 20000
    retries: 0
```

The important fields are:

- `adapter`: selects the engine's invocation and safety contract.
- `command`: an argv array. Built-in adapters accept exactly one executable; an absolute path is
  allowed.
- `model`: the model ID understood by that local CLI.
- `model_reasoning_effort`: Codex reasoning effort.
- `reasoning_effort`: Kimi reasoning effort.
- `permission`: requested execution authority.
- `timeout`, output limits, and `retries`: per-provider-call resource bounds.

Do not put a prompt, credentials, or shell expression in `command`. The engine invokes argv
directly and does not expand `~`, environment variables, pipes, or redirects.

## Codex

```yaml
agents:
  codex_reviewer:
    adapter: codex
    command: [codex]
    model: gpt-5.6-sol
    model_reasoning_effort: medium
    permission: read_only
    timeout: 600
    max_output: 100000
    max_final_output: 20000
    retries: 0
```

The adapter passes the model with `--model` and the effort through Codex configuration. If omitted,
the adapter falls back to `gpt-5.6-sol` and `medium`. The built-in adapter owns all remaining Codex
arguments, so `extra_args` must be empty.

Prefer `read_only` for analysis. `workspace_write` and `danger_full_access` require the
configuration choice plus `--allow-unsafe` when running.

## Kimi

```yaml
agents:
  kimi_alternative:
    adapter: kimi
    command: [kimi]
    model: k3
    reasoning_effort: high
    permission: danger_full_access
    timeout: 600
    max_output: 100000
    max_final_output: 20000
    retries: 0
```

If omitted, the adapter falls back to model `k3` and reasoning effort `high`. The alias `standard`
is normalized to `high`. Other model IDs may be used when the installed Kimi CLI and account
support them.

Kimi 0.29.1 headless prompt mode auto-approves tool calls and has no honest read-only mapping.
Consequently, the profile must declare `danger_full_access`, every run must include
`--allow-unsafe`, and the process should run inside an external container, VM, restricted account,
or equivalent boundary. Kimi also receives the prompt through argv, which may be visible to local
process inspection.

## Other CLIs with the Generic adapter

Use `generic` when a CLI has a stable non-interactive command and can receive one complete prompt.
For a CLI that reads the prompt from stdin:

```yaml
agents:
  local_reviewer:
    adapter: generic
    command: [/absolute/path/to/my-agent, --model, my-model]
    prompt_transport: stdin
    permission: read_only
    timeout: 600
    max_output: 100000
    retries: 0
```

The other transports are:

```yaml
# Append the prompt as one positional argument.
prompt_transport: argument
```

```yaml
# Append: --prompt "<complete prompt>"
prompt_transport: flag
prompt_flag: --prompt
```

For Generic profiles, `model` is recorded as audit metadata but is not translated into a CLI
argument. Put fixed, non-secret model flags in `command` or `extra_args`. Prefer `stdin`: argument
and flag transport expose the prompt through process argv.

The engine cannot prove the Generic command's sandbox, permission behavior, fresh-session
semantics, or version contract. Every Generic profile therefore requires `--allow-unsafe`, cannot
run in a `parallel` stage, and should be externally sandboxed. Use
`independent_sequential` when multiple independent, unsafe profiles must see the same pre-stage
evidence without running concurrently.

## Assign runtimes to roles

The profile name—not the adapter name—is referenced by the workflow:

```yaml
workflow:
  stages:
    - id: proposals
      mode: independent_sequential
      participants:
        - id: architect
          agent: codex_architect
          prompt: prompts/architect.md
        - id: alternative
          agent: kimi_alternative
          prompt: prompts/alternative.md
  judge:
    agent: codex_judge
    prompt: prompts/judge.md
```

Profiles may use different models, efforts, timeouts, and providers. Reusing a profile does not
resume a hidden provider conversation; every participant is a fresh invocation assembled from
explicit engine evidence.

## Validate before spending tokens

```bash
agent-debate validate --config debate.yaml
agent-debate doctor --config debate.yaml
```

`validate` checks the schema and adapter compatibility without invoking a provider. `doctor`
checks executable availability and performs allowlisted version probes for built-in adapters; it
does not send a debate prompt.

Run a read-only Codex workflow:

```bash
agent-debate run --config debate.yaml "Compare option A with option B"
```

Run a workflow containing Kimi, Generic, or another unsafe profile only inside the intended
external boundary:

```bash
agent-debate run --config debate.yaml --allow-unsafe "Compare option A with option B"
```

For a complete mixed-provider workflow, copy
[`examples/codex-kimi-standard/debate.yaml`](../examples/codex-kimi-standard/debate.yaml). For every
schema field and workflow option, see [configuration.md](configuration.md). Review
[security.md](security.md) before enabling an unsafe runtime.
