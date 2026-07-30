# Agent Debate Engine

Repository: [github.com/zhouchangju/agent-debate-engine](https://github.com/zhouchangju/agent-debate-engine)

Agent Debate Engine runs structured, multi-stage debates through local coding-agent CLIs. It gives
independent agent profiles distinct roles, preserves the dependency between proposal, criticism,
revision, and judgment, and writes an auditable run instead of returning an opaque blob of text.

The engine is deliberately a small Python runtime—not another general agent framework. Its core
properties are:

- stages execute in order while independent participants inside a stage can run concurrently;
- Codex, Kimi, and generic commands are invoked through explicit adapter contracts without a
  shell;
- prompts, stdout, stderr, exit status, timing, hashes, and Judge decisions are retained;
- prompt and output growth is bounded;
- a strict Judge protocol feeds deterministic stopping rules;
- non-zero exits, timeouts, malformed Judge output, and output overflow fail explicitly;
- the generated debate uses two read-only Codex profiles, and unsafe modes require a second CLI
  opt-in.

## Quick start

Requirements:

- Python 3.11 or newer;
- a POSIX operating system (Linux or macOS);
- at least one configured local agent CLI;
- authenticated CLI sessions managed by the provider.

Windows is not supported in version 0.1. The process-group, file-locking, private-permission, and
durability guarantees described by this release depend on POSIX behavior.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

agent-debate init
agent-debate validate --config debate.yaml
agent-debate doctor --config debate.yaml
agent-debate run --config debate.yaml "Design a durable agent memory subsystem"
```

The `debate` command is an alias for `agent-debate`.

`init` writes an editable `debate.yaml` and role prompts. The bundled template defaults Codex to
model `gpt-5.6-sol` with `model_reasoning_effort: medium`, and each agent runs under `read_only`.
Every run receives a
collision-safe directory below
`.agent-debate/runs/`. Initialization never follows a symbolic link in the destination path: the
destination and every existing ancestor must be real directories.

## Configure local agent runtimes

Agent profiles in `debate.yaml` determine which local CLI runs each debate role. The engine has
built-in contracts for Codex and Kimi, plus a Generic adapter for other argv-based agent CLIs.

Start with the [agent runtime configuration guide](docs/agent-runtimes.md). It contains copyable
Codex, Kimi, and Generic examples; model and reasoning-effort settings; role mapping; validation
commands; and the permission differences that affect how a run must be contained. The exhaustive
field reference remains in [configuration.md](docs/configuration.md).

## Natural-language Skill

The versioned [Agent Debate Skill](skills/agent-debate/README.md) turns the safe technical-review
workflow into a natural-language interface. Install it once by linking the repository copy into
the global Skill directory:

```bash
ln -s \
  "$(pwd)/skills/agent-debate" \
  ~/.agents/skills/agent-debate
```

Then ask Codex naturally:

```text
Use agent-debate to review whether this repository should migrate from SQLite
to PostgreSQL. Focus on recovery risk and operational cost, standard depth.
```

The Skill selects the current workspace, writes a bounded task file, invokes the read-only Codex
preset through a JSON runner, and returns the synthesis, terminal status, unresolved issues, and
exact audit path. `quick`, `standard`, and `deep` map to bounded one-, three-, and five-round
budgets. A request for a preview uses `plan` and makes no provider calls.

The Skill deliberately cannot enable Kimi, Generic commands, write access, or
`danger_full_access`. Those modes remain available only through an explicit YAML configuration,
runtime acknowledgement, and external containment. Ordinary analysis requests without clear
multi-agent intent do not silently spend provider tokens.

## How a debate runs

```text
validate + preflight
        │
        ▼
  proposal stage ── independent participants run concurrently
        │
        ▼
  critique stage ── sees every successful proposal
        │
        ▼
  revision stage ── sees the critique
        │
        ▼
  structured Judge
        │
        ├── deterministic stop rule satisfied ──► finalized
        └── otherwise ───────────────────────────► next round / exhausted
```

In the generated technical-review workflow, `Architect` and `Alternative` are separate provider
invocations built from the same pre-stage evidence set. They share the task and prior-round
evidence candidates, subject to each prompt's context budget, but receive different role prompts
and cannot see each other's current-round response.
Only after both proposal calls reach the stage barrier does `Critic` receive both proposals.
`Reviewer` then receives the proposals plus the critique, and the Judge receives every
current-round output.

Reusing an agent profile later in the workflow does not resume a hidden conversation: each
participant is a new invocation whose context is assembled explicitly by the engine. Participants
may inspect the same configured workspace, but their prompt contexts remain separate. If the Judge
requests another round, both proposal roles receive the Judge ledger, open issues, next-round
focus, and eligible bounded prior-round evidence, then produce their new proposals independently
again.

The Judge reports a verdict, confidence, current synthesis, accepted decisions, rejected options,
and unresolved issues. The engine—not the model—decides whether the configured stopping conditions
are satisfied. Reaching the round limit without convergence produces an explicit `exhausted` run;
it is never mislabeled as consensus.

## Commands

```text
agent-debate init [DIRECTORY]
agent-debate validate --config PATH
agent-debate doctor --config PATH
agent-debate run [TASK] --config PATH [--task-file PATH|-]
agent-debate resume RUN_DIRECTORY [--retry-failed]
agent-debate schema
```

Use `--allow-unsafe` only after choosing a non-read-only permission in trusted configuration. The
two-part acknowledgement prevents an old or copied configuration from silently enabling broad
agent permissions.

## Supported adapters

The built-in contracts were verified locally against:

| Adapter | Verified CLI | Prompt transport | Supported permission |
|---|---|---|---|
| Codex | `>=0.145.0,<1.0.0` (`0.145.x`–`0.146.x` verified) | stdin to `codex exec ... -` | `read_only`, `workspace_write`, or `danger_full_access`; approvals denied |
| Kimi | `kimi 0.29.1` | `--prompt` argument | `danger_full_access` only |
| Generic | user-defined argv | stdin, argument, or flag | externally enforced; always unsafe to the engine |

Built-in Codex and Kimi adapters own prompt transport, workspace, model, output, provider
configuration, and permission argv. Configuration cannot override those controls through
`extra_args`. A generic executable has no portable, engine-enforceable sandbox contract, so every
generic profile requires `--allow-unsafe` regardless of its declared permission and cannot
participate in a parallel stage. Run generic commands inside an externally enforced sandbox.
See [Configuring agent runtimes](docs/agent-runtimes.md) for setup examples.

`doctor` executes `--version` only for allowlisted built-in executable names. Codex releases from
`0.145.0` through the remaining `0.x` line are accepted; releases newer than the locally verified
`0.146.x` surface produce a warning, while older or new-major releases fail closed. It never
executes a generic command: for generic profiles it checks only that the executable can be resolved
and executed, then reports that no portable side-effect-free version probe exists.

Kimi 0.29.1 headless `--prompt` mode creates or resumes a session with permission `auto` and
auto-approves tool calls. The same CLI rejects combining `--prompt` with `--plan`, `--yolo`, or
`--auto`, so there is no honest read-only mapping for the built-in adapter. It emits
`--prompt … --output-format text` without a permission flag, accepts only
`permission: danger_full_access`, and requires `--allow-unsafe`. Prompt content is redacted from
displayed commands and bounded, but remains visible in process argv. Use an external sandbox and
read [the security model](docs/security.md) before enabling it.

## Run artifacts

A successful or failed run keeps:

```text
<run>/
├── manifest.json
├── events.jsonl
├── config.resolved.yaml
├── request.md
├── rounds/
│   └── 001/
│       ├── <stage>/<participant>/<invocation-id>/
│       │   ├── request.md
│       │   ├── stdout.log
│       │   ├── stderr.log
│       │   ├── final.md
│       │   └── meta.json
│       └── judge/
│           ├── request.md
│           ├── raw.md
│           └── decision.json
└── final.md
```

Invocation directories are immutable and uniquely named, so a retry or resumed incomplete round
preserves the earlier attempt instead of overwriting it. Resume locks the run, validates the
schema-v1 manifest and all indexed paths, verifies hashes, metadata cross-links, and the event
sequence, then checks lifecycle eligibility and Judge barriers before loading persisted
configuration or running provider preflight. Only a schema-valid Judge decision is a completed
round checkpoint.

Transport logs remain bounded. Providers whose stdout is authoritative fail when that budget is
crossed. Codex calls with a managed final-output artifact keep draining oversized JSON event
streams, retain only the configured transport evidence budget, and record truncation plus the
total observed character count in `meta.json`; the final response still has its own strict limit.

Writes are atomic where replacement is required, run directories and files are private on
supported POSIX systems, and an append-only event log preserves state transitions. Codex-owned
output is first collected below the engine's private state root, outside the workspace and system
temporary directory, then link-validated, copied into the canonical artifact directory, and
removed. `danger_full_access` still requires external containment. Details are in
[the run-format reference](docs/run-format.md).

## Configuration

The generated YAML is the canonical example. Paths are resolved relative to the configuration file
and unknown keys are rejected. A workflow is a linear list of stages; participants inside a stage
may be parallel or sequential. This small model expresses real debate dependencies without
introducing a DAG framework.

See:

- [configuration reference](docs/configuration.md)
- [architecture](docs/architecture.md)
- [security and trust boundaries](docs/security.md)
- [first-principles design review](docs/first-principles-review.md)
- [project purpose, onboarding rationale, and Multica positioning](docs/project-purpose-and-positioning.md)
- [onboarding presentation (HTML)](docs/agent-debate-engine-onboarding.html)
- [source and dependency provenance policy](docs/provenance.md)
- [release process](docs/releasing.md)
- [support and maintenance boundaries](SUPPORT.md)
- [code of conduct](CODE_OF_CONDUCT.md)

For a safe public walkthrough without provider calls or private run data, use the
[sanitized decision example](examples/sanitized-decision/README.md). It is an explicitly illustrative
artifact contract, not a claimed model transcript.

## Development

```bash
python -m pip install -e ".[dev]"
make format
make check
python -m build
```

The default test suite uses fake executables and never spends model tokens or requires provider
credentials. Real CLI smoke tests, when present, must remain explicitly opt-in.

## Codex + Kimi role-mapped workflow

[`examples/codex-kimi-standard/debate.yaml`](examples/codex-kimi-standard/debate.yaml)
assigns Architect, Critic, and Judge to Codex and Alternative and Reviewer to
Kimi. Its proposal stage uses `independent_sequential`: sibling prompts are
frozen from the same context before either provider runs, while provider
processes execute serially. This prevents sibling-output leakage without
concurrently launching write-capable Kimi.

Every terminal run writes `evidence.md` beside `final.md`. It contains the
original task, role/model mapping, fresh-session declaration, exact prompt,
final output, raw stdout/stderr, and structured Judge decisions for every
invocation. The example remains outside the safe Skill boundary and requires
`--allow-unsafe` plus external containment.

## Local history dashboard

Every terminal run now writes a versioned `result.json` alongside `final.md`,
`evidence.md`, and `manifest.json`. Browse all histories below `.agent-debate`
without opening individual files:

```bash
uv run agent-debate-dashboard --root .agent-debate
```

The local-only interface provides history search, status filters, final
decisions, unresolved risks, role/model mapping, Round timelines, and every
invocation's exact input, output, stdout, and stderr. Existing v1 runs without
`result.json` are adapted in memory. See
[the dashboard guide](docs/dashboard.md) and
[the result schema](src/agent_debate/schemas/result-v1.json).

## Project status

Version `0.1.0` establishes the runtime and artifact contracts. The design intentionally leaves
distributed execution, a web UI, Agent OS integration, and concurrent code-writing worktrees out of
scope. Those features should be added only after concrete use cases justify their trust and
operational cost. Windows support is also out of scope for 0.1; use Linux or macOS, and place
unsafe providers inside an external containment boundary such as a container, VM, or restricted
account.

Licensed under the [MIT License](LICENSE).
