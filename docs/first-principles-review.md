# First-principles design review

This review records why version 0.1 is shaped as a small, auditable local runtime and which tempting
features were rejected. It is a decision record, not a claim that the design is complete forever.

## Start with the actual problem

The problem is not “make several models talk.” A shell script can do that. The useful outcome is:

> Given an untrusted design task and locally authenticated agent CLIs, produce a bounded,
> inspectable decision process whose dependencies, failures, evidence, permissions, and stopping
> state can be explained after the process exits.

That statement yields five non-negotiable properties:

1. A later opinion must not race ahead of evidence it is supposed to review.
2. Model output, including Judge output, is untrusted data.
3. Failure and exhaustion must remain distinguishable from agreement.
4. Execution authority and resource use must be bounded before model calls begin.
5. The durable result must support review and resume without relying on terminal scrollback.

## Decisions derived from those properties

### A linear stage list, with bounded parallelism inside a stage

A general DAG scheduler is unnecessary for proposal → critique → revision → judgment. It adds graph
validation, partial-order resume semantics, and a larger failure surface without improving the
default debate.

A stage list makes dependencies visible in YAML. Parallel proposals reduce latency and encourage
independence. A barrier then gives the critic a complete proposal set. Critique and synthesis
revision are sequential because each exists to consume the preceding evidence.

The generated policy makes four participant calls plus one Judge call per round. It may finalize
after one qualified round and caps at three. Requiring two or more rounds by default would create a
high minimum token bill before evidence showed that another round was useful. Users seeking
independent confirmation can raise `min_rounds` and `stable_rounds`.

### Typed adapters, not shell composition

The runtime needs process control, timeout, output limits, version probing, and normalized results.
An argv-building adapter is the smallest boundary that supplies them. It also keeps task text out of
shell syntax. Built-in adapters own their provider configuration and security argv; allowing a
caller to inject Codex profiles/features or replace Kimi prompt/permission arguments would
invalidate the permission contract.

The built-in surface was locally inspected with:

```text
codex-cli 0.145.0
0.29.1
```

Local CLI contracts and Kimi's installed bundled source were checked; no real model call was
required. This is honest compatibility evidence for one environment, not a promise about all CLI
releases.

Kimi 0.29.1 documents prompt mode through `--prompt`, so the adapter uses argv, redacts the prompt
from displayed commands, and enforces a conservative UTF-8 byte ceiling. The installed bundle
shows that headless prompt mode creates or resumes a session with permission `auto` and
auto-approves tools, while the CLI rejects combining `--prompt` with `--plan`, `--yolo`, or
`--auto`. The adapter therefore supports only `danger_full_access`, emits no permission-mode flag,
and relies on the engine's configuration plus `--allow-unsafe` double opt-in. Calling this
read-only would be a false security guarantee.

### Provider defaults, not hardcoded models

A model alias is deployment-specific and can disappear or point to different capabilities.
Hardcoding one in a distributable template would improve neither correctness nor reproducibility
unless the provider configuration were also controlled. The template omits `model` by default to keep
defaults portable across environments. The adapter layer currently applies stable fallback defaults
(`gpt-5.6-sol`/`medium` for Codex, `k3`/`high` for Kimi) so unconfigured profiles still get
predictable runtime behavior while avoiding a locked model in the template.

### Read-only by default, with two-part escalation

Text debate does not require mutation. Write authority would increase blast radius without
improving the reasoning artifact. The generated workflow therefore uses two independent Codex
profiles, both configured as `read_only`; it does not include Kimi.

Any non-read-only configuration also needs `--allow-unsafe` at runtime. Two independent opt-ins
address two different failure modes: the YAML records durable intent, while the command-line
acknowledgement prevents a stale or copied file from silently exercising it.

This policy is still only as strong as the provider. Codex exposes a read-only sandbox flag. Kimi
headless prompts auto-approve tools, and a generic command supplies its own enforcement. Every
generic profile is therefore classified as unsafe regardless of its declared permission, requires
`--allow-unsafe`, and is excluded from parallel stages. Kimi and generic execution over untrusted
inputs require an external container, VM, restricted account, or equivalent boundary.

### A strict Judge protocol plus a deterministic stop rule

Natural-language “looks good” is not a state machine. Judge schema v1 has a closed set of fields,
bounded confidence, enumerated verdicts and severities, and explicit unresolved issues. The prompt
requires evidence labels and warns that agreement is not correctness.

Schema validity is necessary but insufficient. The engine, not the Judge, applies minimum and
maximum rounds, elapsed time, confidence, critical-issue, and stability rules. That separation
prevents a prompt-injected Judge from redefining convergence. A continuing debate at its round
limit is `exhausted`, not “consensus.”

### A directory protocol, not terminal-only output

A final paragraph cannot explain a timeout, malformed Judge response, truncated process, retry, or
resume. The run directory retains:

- resolved configuration and original request;
- every participant request, stream, final response, and execution metadata in a uniquely
  identified immutable invocation directory;
- raw and parsed Judge output;
- an atomic manifest, append-only events, and per-file hashes;
- an exclusive resume lock and explicit failure artifacts.

This is the minimum evidence needed to diagnose a run without replaying paid or nondeterministic
calls. Retries and resumed incomplete rounds append fresh invocation records rather than replacing
earlier evidence. Their manifest order is explicit and monotonic; the newest attempt supersedes an
older attempt before successful evidence is selected, so a failed rerun cannot revive a discarded
answer. Only a schema-valid Judge decision completes a round; invalid raw Judge output remains
evidence but never becomes a checkpoint.

Resume also follows the trust boundary rather than convenience: acquire the lock, validate the
schema-v1 manifest and contained paths, verify every indexed hash plus metadata/event cross-links,
reject an ineligible lifecycle, and validate contained Judge barriers before loading a persisted
configuration that can select external paths or probing a provider. Otherwise a crafted manifest
could turn “resume” into an arbitrary local-file reader before integrity checks ran.

## Why ZIP was overturned

A ZIP archive was considered as a primary handoff format and rejected from first principles.

The durable objects have different canonical forms:

- source code is a reviewable repository tree;
- an installable Python release is a reproducible wheel and source distribution;
- a live debate is a locked run directory with an atomically updated manifest and append-only event
  stream.

ZIP solves file transport, not any of those correctness problems. Making it canonical would make
the system worse in several ways:

1. **Review:** an opaque archive hides the normal file diff and encourages review after extraction,
   disconnected from version-control provenance.
2. **Reproducibility:** archive contents can silently include caches, virtual environments,
   absolute-path residue, credentials, or omit package data. `python -m build` has a defined Python
   packaging contract; an ad hoc ZIP does not.
3. **Run consistency:** archiving an active run can capture `events.jsonl` and `manifest.json` at
   different logical moments, bypassing the lock and integrity protocol.
4. **Resume safety:** an extracted archive can change permissions, timestamps, symlink behavior, or
   directory identity. Treating it as a live run would weaken the explicit resume checks.
5. **Duplication:** committing both a source tree and hand-built archive creates two candidates for
   “the latest version,” guaranteeing drift.

The decision is therefore not “ZIP is always bad.” A closed, integrity-verified run may be copied or
archived for transport, and a release system may offer a ZIP as a derived convenience artifact.
But an archive must be generated from a reviewed source or closed run, never replace the canonical
tree, build output, lock, hashes, or manifest. Version 0.1 does not add an export feature until a
concrete transport requirement defines redaction, consistency, and verification semantics.

## Other rejected shortcuts

| Shortcut | Why it was rejected |
|---|---|
| “Stop when agents agree” | Correlated models can agree on the same unsupported claim. |
| Unbounded rounds | Converts uncertainty into unpredictable cost and latency. |
| Full write access by default | Text analysis does not justify workspace mutation. |
| One giant prompt with all history | Context grows without bound and buries current issues. |
| Silent truncation | Can remove the constraint that makes a design unsafe. |
| Retry forever | Repeats cost and can duplicate side effects. |
| Generic agent framework | Enlarges the API and trust surface before concrete use cases exist. |
| Distributed or Agent OS execution | Introduces identity, transport, secret, and consistency problems absent from a local process. |
| Parallel code-writing agents | Requires worktree isolation and merge policy; it is not needed for text debate. |
| Windows support in 0.1 | POSIX process groups, locking, link checks, private modes, and durability are part of the current contract; emulating them partially would overstate safety. |

## Assumptions and falsifiers

The design assumes a single user, one POSIX machine, trusted local CLI installations, modest text
artifacts, and a workflow expressible as a stage list. Revisit the architecture when evidence shows:

- a real workflow needs branching or joins that a linear stage list cannot express;
- runs must move between machines while preserving signed provenance;
- multiple writers need coordinated ownership;
- Kimi gains a documented noninteractive prompt transport with an enforceable sandbox contract;
- provider events, not captured text, become necessary for faithful replay;
- artifact volume requires a database or content-addressed object store;
- a concrete safe export requirement justifies a verified archive format;
- Windows lifecycle, locking, link, and durability semantics can meet an explicit, tested support
  contract.

Until one of those falsifiers appears, the smaller design has fewer states to secure, test, and
explain.
