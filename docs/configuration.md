# Configuration reference

`debate.yaml` uses schema version 1. `agent-debate init` writes the bundled template and role
prompts; `agent-debate validate --config PATH` checks the file without calling a model.

Version 0.1 officially supports POSIX platforms (Linux and macOS). Windows is not supported.

## Safe embedded preset

Natural-language integrations can use
`agent_debate.presets.build_technical_review_config(workspace, depth=...)` instead of generating
YAML. The preset is intentionally closed:

- two Codex roles;
- `read_only` permission and denied approvals;
- parallel primary and alternative proposals;
- sequential critique and revision;
- one structured Judge;
- no Kimi, Generic adapter, extra provider argv, or unsafe acknowledgement.

Depth controls only bounded deliberation:

| Depth | Minimum rounds | Maximum rounds | Stable decisions | Time budget |
|---|---:|---:|---:|---:|
| `quick` | 1 | 1 | 1 | 3,600 seconds |
| `standard` | 1 | 3 | 1 | 10,800 seconds |
| `deep` | 2 | 5 | 2 | 21,600 seconds |

Every bundled Codex participant has a one-hour per-attempt timeout. The larger run-level budgets
preserve the existing depth ratios and leave sequential critique, revision, and Judge stages time
after the parallel proposals finish. These values are ceilings; completed calls return
immediately.

The bundled Skill uses a unique output root per request, which lets an error response identify the
request-owned run without selecting a potentially unrelated "newest" directory.

Unknown keys are rejected. Relative paths are resolved against the directory containing the YAML,
not the caller's current directory. This applies to the workspace, output directory, participant
prompts, and Judge prompt.

## Top-level shape

```yaml
schema_version: 1
run: {}
agents: {}
workflow: {}
context: {}
failure: {}
```

All five sections are typed. The complete generated example is
[`src/agent_debate/templates/debate.yaml`](../src/agent_debate/templates/debate.yaml).

## `run`

| Field | Type | Loader default | Meaning |
|---|---:|---:|---|
| `output_dir` | path | `.agent-debate/runs` | Parent directory for collision-safe run directories. |
| `workspace` | path | `.` | Working directory passed to agent CLIs. |
| `max_parallel` | integer from 1 to 32 | `4` | Global cap on concurrent participant processes. |
| `stream` | boolean | `true` | Stream progress while still retaining run artifacts. |

The generated template explicitly uses `max_parallel: 2`, matching its two proposal participants.
Concurrency never crosses a stage barrier.

## `agents`

`agents` maps a local identifier to one execution profile and is capped at 64 profiles:

```yaml
agents:
  codex_primary:
    adapter: codex
    command: ["codex"]
    model: gpt-5.6-sol
    permission: read_only
    extra_args: []
    timeout: 300
    max_output: 100000
    max_final_output: 20000
    retries: 0
  codex_alternative:
    adapter: codex
    command: ["codex"]
    model: gpt-5.6-sol
    permission: read_only
    extra_args: []
    timeout: 300
    max_output: 100000
    max_final_output: 20000
    retries: 0
```

| Field | Type | Default | Meaning |
|---|---:|---:|---|
| `adapter` | `codex`, `kimi`, or `generic` | required | Adapter contract to use. |
| `command` | non-empty list of strings | required | Executable argv; built-in adapters require exactly one item, while generic may use a trusted prefix. |
| `model` | string or null | null | Optional CLI model alias. Omit to inherit the CLI's configured default or adapter fallback (`gpt-5.6-sol` for Codex, `k3` for Kimi). |
| `model_reasoning_effort` | string or null | null | Optional Codex `model_reasoning_effort` override passed as `--config model_reasoning_effort=<value>`. If omitted, `medium` is used. |
| `reasoning_effort` | string or null | null | Optional Kimi thinking-effort override passed as `KIMI_MODEL_THINKING_EFFORT=<value>`. If omitted, `high` is used. `standard` is normalized to `high` by the adapter for compatibility with UI naming. |
| `permission` | permission enum | `read_only` | Requested provider permission; see below. |
| `extra_args` | list of strings | `[]` | Additional trusted argv for generic only; built-in adapters reject non-empty values. |
| `timeout` | positive number | `300` | Per-attempt timeout in seconds. |
| `max_output` | integer from 1 to 10000000 | `100000` | Maximum combined captured stdout and stderr transport evidence in characters. Adapters without a separate authoritative output fail on overflow. Codex invocations using managed `-o` output keep draining but stop retaining transport text at this boundary and record the truncation. |
| `max_final_output` | integer from 1 to 10000000, or null | inherits `max_output` | Maximum authoritative final-output artifact in characters when an adapter provides one. New templates set `20000`; omission preserves the legacy shared limit. |
| `retries` | integer from 0 to 5 | `0` | Additional process attempts after an invocation failure. |

`command` is an argv array so spaces and punctuation remain data. Do not write
`command: "codex ..."` and do not place task content in it. The runtime does not invoke a shell and
therefore does not expand `$VARIABLE`, `${VARIABLE}`, or `~` in command entries; write the intended
executable path explicitly. The executable and any fixed prefix are trusted code; custom prefixes
can change provider behavior and must be reviewed as part of the control plane.

`permission: read_only` is the schema-level default, but adapter compatibility is validated
separately. A Kimi profile must set `permission: danger_full_access` explicitly; omitting the field
or selecting `read_only`/`workspace_write` fails before a model call.

### Model selection

The bundled template defines two Codex profiles and sets `model: gpt-5.6-sol` with
`model_reasoning_effort: medium`, matching the OpenAI Codex configuration used by the
safe preset in this repository. In Codex mode this produces a CLI argument sequence that includes
`--config model_reasoning_effort=medium`.

For Kimi, the adapter-level defaults are `model: k3` and `reasoning_effort: high` when
the field is omitted in config. If you explicitly set one of these values, it takes precedence.
The `standard` mode in Kimi UX maps to `high` on the current CLI effort API,
so users typically set `reasoning_effort: high` directly.

Codex and Kimi profiles may also set `model` when the local provider supports the ID and
reproducibility is more important than portability. The resolved configuration snapshot records whether
each model-related override was supplied.

Built-in adapters own the ordered argv that controls prompt transport, model selection, workspace,
structured/final output, permission mode, provider configuration, profiles, and feature toggles.
`extra_args` cannot override those controls. In particular, a Codex profile cannot use `-c`,
`--config`, `--profile`, or feature flags to weaken the adapter's sandbox contract, and Kimi cannot
replace its prompt, output-format, permission-mode, model, or directory arguments. Use a generic
profile only when a different trusted wrapper contract is genuinely required.

For Kimi defaults, note that Kimi model IDs are not version labels; use ID values from the official
model list such as `k3`, `k3-256k`, `kimi-for-coding`, or `kimi-for-coding-highspeed`. In K3, the
default reasoning effort is `high` when effort is unspecified, and supported effort mappings are
`low`/`minimum`/`light`, `high`/`medium`, and `ultra`/`max`/`xhigh` per the Kimi docs.

### Permissions

Allowed values are:

- `read_only`: generated default for text-only debates;
- `workspace_write`: permits the provider agent to change the configured workspace;
- `danger_full_access`: requests the provider's broadest supported execution mode.

Any configured agent using `workspace_write` or `danger_full_access` requires a separate
`--allow-unsafe` acknowledgement at execution time. Every generic profile requires the same
acknowledgement regardless of its declared permission, because the engine cannot enforce a generic
executable's permission label. Configuration and runtime acknowledgement are the two independent
opt-ins. Copying an old YAML file is therefore insufficient to silently widen permissions.

Provider names are not interchangeable security guarantees. Codex `read_only` maps to its
read-only sandbox and denied approvals. The built-in Kimi 0.29.1 adapter supports only
`danger_full_access`: headless `--prompt` mode creates or resumes a session with permission `auto`
and auto-approves tools, and the CLI rejects combining `--prompt` with `--plan`, `--yolo`, or
`--auto`. The adapter emits `--prompt … --output-format text` with no permission-mode flag.
Generic commands must be contained by their deployment environment; their declared permission is
audit metadata, not enforcement. Generic profiles are also rejected from parallel stages, where an
unenforceable writer could race another participant. See [security.md](security.md).

### Generic prompt transport

Only `adapter: generic` accepts these fields:

| Field | Values | Behavior |
|---|---|---|
| `prompt_transport` | `stdin` | Write the prompt to standard input. |
|  | `argument` | Append the prompt as one positional argv element. |
|  | `flag` | Append `prompt_flag` and then the prompt. |
| `prompt_flag` | string beginning with `-` | Required only for `flag`; forbidden for the other modes. |

`prompt_transport` is required for a generic agent and forbidden for the built-in Codex and Kimi
agents, whose transport is adapter-owned. Argument and flag modes can expose prompt text to local
process inspection and are subject to platform argv-size limits. All generic modes require
`--allow-unsafe`; prefer a dedicated external sandbox even for `stdin` transport.

`agent-debate doctor` deliberately does not run a generic command, including with `--version`.
There is no portable guarantee that an arbitrary command treats that argument as a read-only
version probe. Doctor checks generic executable resolution and executable permission only and
reports the missing version probe as a warning. Built-in probes accept only a single executable
named `codex` or `kimi` and fail closed when the reported product/version does not match the
verified adapter contract.

## `workflow`

```yaml
workflow:
  stages:
    - id: proposals
      mode: parallel
      participants:
        - id: architect
          agent: codex_primary
          prompt: prompts/architect.md
        - id: alternative
          agent: codex_alternative
          prompt: prompts/alternative.md
    - id: critique
      mode: sequential
      participants:
        - id: critic
          agent: codex_primary
          prompt: prompts/critic.md
  judge:
    agent: codex_primary
    prompt: prompts/judge.md
  stop: {}
```

`stages` is a non-empty ordered list capped at 32 entries. Each stage has:

- a unique `id`;
- `mode: parallel`, `mode: sequential`, or `mode: independent_sequential`
  (default `parallel`);
- one to 32 `participants`.

Each participant has an `id` unique within its stage, an `agent` reference, and a readable `prompt`
path. `label` is an optional display name. A participant ID becomes part of evidence labels and
artifact paths, so keep it stable when resuming or comparing runs.

Stages always execute in list order. In a parallel stage, participants start concurrently subject
to `run.max_parallel`; the next stage waits for all required participants. In a sequential stage,
participants execute in list order. Generic profiles are never permitted in a parallel stage,
regardless of the permission label recorded in YAML.

In an `independent_sequential` stage, every participant prompt is built from the
same pre-stage evidence before execution begins, then participants run one at a
time. Use it when independent opinions are required but concurrent provider
processes are unsafe or undesirable.

The complete Codex/Kimi role mapping is available in
`examples/codex-kimi-standard/debate.yaml`. It intentionally requires
`--allow-unsafe` because Kimi headless mode is write-capable.

`workflow.judge.agent` references an agent profile and `workflow.judge.prompt` points to a Judge
prompt. The bundled prompt requires exactly one Judge schema-v1 object.

### `workflow.stop`

| Field | Type | Loader default | Meaning |
|---|---:|---:|---|
| `min_rounds` | positive integer | `2` | Earliest round eligible for soft finalization. |
| `max_rounds` | integer from 1 to 100 | `6` | Hard round ceiling. |
| `confidence_threshold` | number from 0 to 1 | `0.8` | Minimum Judge confidence for soft finalization. |
| `stable_rounds` | positive integer | `2` | Consecutive qualifying decisions required. |
| `max_elapsed_seconds` | number greater than 0 and at most 86400 | `1800` | Cumulative engine wall-clock ceiling, durably checkpointed after completed calls and controlled transitions. |

The generated template overrides these loader defaults with a lower-cost policy:

```yaml
min_rounds: 1
max_rounds: 3
confidence_threshold: 0.85
stable_rounds: 1
max_elapsed_seconds: 1800
```

The engine evaluates stopping deterministically. A Judge `finalize` is insufficient unless the
round, confidence, critical-issue, and stability conditions pass. `blocked` stops immediately.
Reaching a hard limit while the Judge says `continue` yields `exhausted`.

For higher assurance, increase `min_rounds` and `stable_rounds`; this directly increases model calls
and latency.

## `context`

| Field | Type | Default | Meaning |
|---|---:|---:|---|
| `max_prompt_chars` | integer from 1 to 1000000 | `24000` | Total context budget for one invocation. |
| `max_requirement_chars` | integer from 1 to 1000000 | `8000` | Combined task and immutable role-text ceiling; cannot exceed `max_prompt_chars`. |
| `max_response_chars` | integer from 11 to 1000000 | `8000` | Initial ceiling for each included response body; cannot exceed `max_prompt_chars`. |
| `keep_recent_rounds` | integer from 0 to 100 | `2` | Number of recent evidence rounds retained in detail. |

The context builder prioritizes the task, current role, Judge ledger, open issues, next-round focus,
current-round outputs, and recent evidence. Required content that cannot fit is an explicit budget
failure; budgets are not permission to silently rewrite requirements.

Kimi's adapter also enforces a conservative 64 KiB UTF-8 argv prompt ceiling because Kimi 0.29.1 uses
`--prompt` rather than stdin. Its only supported permission is `danger_full_access`, with the
configuration plus `--allow-unsafe` double opt-in. Character budgets and byte limits are related
but distinct.

## `failure`

| Field | Values | Default | Meaning |
|---|---|---|---|
| `on_agent_error` | `abort`, `continue` | `abort` | Stage behavior after participant failure. |
| `on_judge_error` | `abort`, `retry` | `retry` | Behavior after malformed or invalid Judge output. |
| `require_all_participants` | boolean | `true` | Require every configured participant before advancing. |
| `schema_repair_attempts` | `0` or `1` | `1` | Maximum bounded Judge schema-repair attempt. |

`continue` is useful only when the workflow can reason from partial evidence. With
`require_all_participants: true`, a missing required result still prevents a valid stage. Retries
repeat provider calls and may have cost; keep write-capable agents idempotent if permissions are
ever widened.

## Local compatibility evidence

The built-in command contracts were checked on the development machine with:

```text
codex-cli 0.145.0
codex-cli 0.146.0
0.29.1
```

The check inspected local CLI output and, for Kimi's headless permission behavior, the installed
bundled source. Codex preflight accepts `>=0.145.0,<1.0.0`; versions newer than the locally
verified `0.146.x` surface continue with a warning. It did not make real model calls and does not
establish compatibility with every CLI release. Run `agent-debate doctor --config debate.yaml` in
the target environment before spending tokens.
