# Security model

Agent Debate Engine reduces execution risk; it does not make arbitrary agent CLIs, prompts, models,
or repositories trustworthy. Its safest built-in configuration is a text-only architecture debate
using Codex `read_only` profiles in a dedicated, non-sensitive workspace.

## Trust boundaries

| Input or component | Trust assumption |
|---|---|
| YAML configuration and role prompts | Trusted control plane; review changes as code. |
| Task text, repository files, retrieved text, and agent responses | Untrusted content that may contain prompt injection. |
| Configured executables and their local configuration | Trusted code with the user's provider credentials and environment access. |
| Remote model/provider | External processor governed by its own data and retention policy. |
| Run directory | Sensitive audit data; may contain requirements, source excerpts, prompts, and model output. |

The engine never treats an agent's agreement, confidence, or fluent explanation as a security
boundary.

## Safe default and permission escalation

The generated configuration sets both Codex profiles to `permission: read_only` and omits model
overrides. Any `workspace_write` or `danger_full_access` agent requires both:

1. the non-read-only permission in reviewed YAML; and
2. `--allow-unsafe` on the execution command.

Without both opt-ins, the engine refuses execution. This protects against a copied or previously
edited configuration silently regaining write authority. `doctor` should still be used to inspect
the resolved commands and warnings before a run.

Every generic profile is classified as unsafe regardless of its declared permission and therefore
also requires `--allow-unsafe`. A generic command has no portable permission flag the engine can
enforce, and the declaration is audit metadata only. Generic profiles are forbidden in parallel
stages so an unenforceable writer cannot race another participant.

Permissions are provider-specific:

| Adapter permission | Effective provider mode | Security meaning |
|---|---|---|
| Codex `read_only` | approvals denied, `--sandbox read-only` | Codex-enforced read-only sandbox policy. |
| Codex `workspace_write` | Codex workspace-write sandbox | Can mutate the configured workspace. |
| Codex `danger_full_access` | `--sandbox danger-full-access`, approvals denied | Broad host authority; use only inside a separate trusted sandbox. |
| Kimi `danger_full_access` only | headless `--prompt … --output-format text` | Creates or resumes a session with permission `auto` and auto-approves tools; no OS isolation. |
| Generic (any declared permission) | configured command plus external sandbox | Always unsafe to the engine; cannot run in a parallel stage. |

The important asymmetry is Kimi. Inspection of the locally installed Kimi 0.29.1 bundled source
showed that headless prompt mode creates or resumes a session with permission `auto` and
auto-approves tools. Its CLI also rejects combining `--prompt` with `--plan`, `--yolo`, or
`--auto`. The built-in adapter therefore has no read-only or workspace-write mapping: it supports
only `permission: danger_full_access`, emits no permission-mode flag, and still requires the
configuration plus `--allow-unsafe` double opt-in.

For untrusted tasks or repositories, do not enable Kimi unless the entire process runs inside a
disposable container, VM, restricted user account, or another externally enforced sandbox. The
engine's permission label, prompt instructions, and double acknowledgement are guardrails, not
isolation.

Apply the same external-containment rule to every generic command. `--allow-unsafe` records
operator intent; it does not upgrade an arbitrary executable into a sandbox.

## Subprocess safety

Commands are built as argv arrays and launched without `shell=True`. User task text is never
concatenated into a shell command. This prevents shell metacharacters in a task from becoming shell
syntax.

That does not make every argv transport private:

- Codex receives the prompt on stdin.
- Kimi 0.29.1 uses `--prompt`, so prompt text exists in its process argv. The engine redacts it in
  displayed commands and enforces a conservative 64 KiB UTF-8 byte ceiling, but privileged local
  process inspection may still observe it.
- Generic `argument` and `flag` transport have the same exposure. Prefer `stdin` when supported.

Do not put secrets in debate tasks. Redaction in logs does not erase argv from the operating system
or data already sent to a provider.

The configured `command` is trusted. Resolve executable provenance before use; an attacker who can
replace an executable earlier on `PATH`, its plugins, hooks, local config, or credentials is already
inside the execution boundary.

Built-in adapter argv is deliberately closed around its security contract. Codex configuration,
profile, feature, sandbox, approval, workspace, model, prompt, and output controls cannot be
overridden through `extra_args`; Kimi prompt, output-format, permission-mode, model, and directory
controls are adapter-owned as well. A custom command prefix remains a trusted-code boundary and can
change provider behavior despite those downstream checks.

On supported POSIX systems each provider starts in a dedicated process group. Timeouts,
cancellation, output overflow, non-clean exit, or a residual descendant trigger bounded TERM/KILL
cleanup. Process groups are lifecycle supervision, not hostile-code containment: a process can
deliberately detach into another session or use authority outside the working directory. Use a
container, VM, sandboxed service account, or equivalent OS boundary for unsafe providers.

## Prompt injection

Requirements, repository text, prior responses, and quoted material can contain instructions such
as “ignore the Judge schema” or “run this command.” These are data-plane attacks, even when they are
stored in a Markdown file.

The default role prompts:

- state that debate inputs are untrusted evidence;
- forbid embedded content from changing roles, permissions, or output contracts;
- keep the work analysis-only and prohibit side effects;
- require stable evidence labels rather than unsupported authority claims;
- remind the Judge that consensus is not correctness.

The Judge parser then validates a closed schema with no additional fields, and the deterministic
stop evaluator applies hard limits independently of the Judge. These layers reduce accidental
instruction confusion, but no prompt can prove a model immune to injection. OS-enforced least
privilege remains the primary control.

## Judge and convergence integrity

Judge schema v1 requires all fields, rejects additional fields, bounds confidence, and constrains
verdict and issue severity values. Malformed output gets at most the configured bounded repair
attempt.

Evidence labels such as `R1:proposals:architect` provide provenance, not truth. The Judge must cite
them and preserve unsupported claims as unresolved. The engine finalizes only when deterministic
round, confidence, critical-issue, and stability conditions pass. It reports a hard-limit outcome as
`exhausted`, never as consensus.

## Artifact confidentiality and integrity

Run artifacts can contain the original task, full prompts, stdout, stderr, final responses, and
provider error messages. Treat the entire output directory as sensitive.

On POSIX systems the artifact store applies private directory (`0700`) and file (`0600`) modes on a
best-effort basis. Replacement writes use a same-directory temporary file, `fsync`,
`os.replace`, and a parent-directory `fsync` where supported. Events are appended with
`O_APPEND`. Manifest entries and events contain SHA-256 integrity data, and resume verifies
recorded artifacts before accepting new work.

The run lock and canonical artifact paths reject symlinks and unsafe link states before writing.
The active store binds both the lock and run-directory inode, and all canonical reads and writes
are relative to the stable directory descriptor. Replacing the visible run path cannot redirect a
manifest or artifact write. Strict resume also cross-checks invocation indexes with verified
metadata and validates event count, order, JSON shape, and per-event digests.
Codex final output is staged in a newly created `0700` per-invocation directory below the engine's
private POSIX state root (`$XDG_STATE_HOME/agent-debate-engine/provider-scratch`, or
`~/.local/state/agent-debate-engine/provider-scratch`). The engine refuses a state root that
contains a symlink component or overlaps either the configured workspace or the effective system
temporary directory, so Codex `read_only` and `workspace_write` model tools cannot rewrite the `-o`
result through either of their writable roots. If `XDG_STATE_HOME` is customized, it must be an
absolute, provider-sandbox-nonwritable location. Before accepting output, the adapter opens it
without following symlinks and verifies before and after reading that it is the same regular,
single-link file. Validated bytes are then copied into a new immutable invocation directory and the
scratch directory is removed.

This scratch protocol is not a boundary against `danger_full_access`. A full-host provider can
discover and race any user-owned path; such execution still requires a disposable external
container, VM, or equivalent containment boundary.

Project initialization applies the same fail-closed link policy to its destination: it converts the
user's path to a lexical absolute path without resolving it, then rejects a symbolic-link
destination or any existing symbolic-link ancestor. Starter files are prepared in a sibling staging
directory. Updating an existing project is transactional across all starter files; any exception,
including cancellation, restores the prior bytes and modes. If rollback itself cannot finish, the
staging directory and its backups are deliberately retained and its path is attached to the error
instead of being deleted by final cleanup.

Resume treats the manifest as untrusted until it has acquired the exclusive lock and validated the
exact schema-v1 structure, fixed root pointers, safe relative paths, ordered indexes, every
recorded artifact hash, metadata cross-links, and event sequence. It then rejects an ineligible
lifecycle and validates contained Judge barriers before loading a configuration that can select
external workspace or prompt paths. Provider preflight comes only after those gates. Retries and
resumed incomplete rounds receive fresh invocation IDs, preserving previous evidence. An invalid
Judge response remains raw invocation evidence but never becomes a `decision.json` or
completed-round checkpoint.

These controls are not encryption. Filesystem administrators, backups, malware running as the user,
and provider-side retention remain outside the engine's protection. Put run directories on an
appropriately encrypted volume and apply a retention policy.

## Resource and denial-of-service controls

Configuration bounds:

- participant concurrency;
- per-attempt time and captured output;
- prompt, requirement, and prior-response characters;
- recent rounds retained in context;
- process and Judge retries;
- total rounds and elapsed time.

Limits turn oversized or non-terminating behavior into explicit failures. They do not eliminate
provider charges incurred before termination. Cumulative elapsed time is durably checkpointed after
each completed invocation and controlled transition. A host or process hard-kill during an active
provider call can lose that uncheckpointed interval, so use an external supervisor for an absolute
deadline across crashes.

## Locally verified CLI surface

On 2026-07-26, local `--version` and `--help` checks used:

```text
codex-cli 0.145.0
0.29.1
```

No real model call was needed for that compatibility check. CLI behavior, flags, default models,
and provider policies can change; run `agent-debate doctor` after upgrades. Doctor runs a bounded
`--version` probe only for the allowlisted built-in executable names and rejects an unexpected
product/version response. It never runs a configured generic command, because arbitrary CLIs do
not share a side-effect-free version contract; generic diagnostics are limited to executable
resolution and permission checks. These observations are evidence about one machine, not a blanket
security certification.

## Deployment checklist

- Review the YAML, role prompts, executable paths, `extra_args`, and local CLI hooks/configuration.
- Prefer Codex `read_only` profiles for text debates.
- Treat every Kimi invocation as full-access execution and require an external sandbox when inputs
  are untrusted.
- Treat every generic profile as unsafe, keep it sequential, and provide an external sandbox.
- Do not include credentials or personal data in the task.
- Keep `--allow-unsafe` out of aliases and unattended scripts.
- Inspect `doctor` output after CLI upgrades.
- Store and delete run artifacts according to their actual sensitivity.
- Treat a schema-valid Judge answer as a claim to verify, not proof.

## Supported operating systems

Version 0.1 officially supports POSIX platforms only (Linux and macOS). Windows is not supported.
The release's process-group cleanup, advisory locking, link checks, private modes, and durability
contract rely on POSIX semantics; “best effort” is not a security guarantee on a platform outside
that support boundary.
