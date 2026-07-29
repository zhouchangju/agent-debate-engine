# Run directory format

## Complete evidence projection

Each terminal run includes a canonical root-level `evidence.md`. It is a
reader-facing projection of the immutable run artifacts, not a replacement for
them. It contains the original task, role/agent/adapter/model map, session
isolation declaration, exact input, final output, raw stdout/stderr, and every
structured Judge decision. The manifest points to it with
`evidence_artifact: evidence.md`.

Each execution owns one collision-safe directory below `run.output_dir`. The directory is the
durable audit and resume boundary; consumers should not infer state from console output.

This document describes run-format schema version 1.

## Directory allocation

Run IDs use a sortable UTC timestamp plus a random UUID:

```text
YYYYMMDDTHHMMSS.ffffffZ-<32 lowercase hex characters>
```

For example:

```text
20260726T143012.123456Z-6d18cc1aaf3e431d8476ba35ee89b1bf
```

The allocator creates the directory exclusively and retries a collision. The manifest `run_id`
must exactly match the directory name.

## Layout

```text
<run>/
├── manifest.json
├── events.jsonl
├── run.lock
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
│           ├── decision.json
│           └── meta.json
├── failures/
│   └── <UTC timestamp>-<uuid>.json
└── final.md
```

Round directories are zero-padded to three digits. Invocation IDs are fresh, lowercase UUID hex
values for each provider attempt. `failures/` appears only when a failure record is needed, and
`final.md` appears only after a final synthesis is written.

Stage and participant IDs are restricted to safe single path components. Absolute paths, `.` and
`..`, path separators, reserved Windows device names, and existing symlink parents are rejected.
The run directory itself cannot be a symlink.

## Root files

### `config.resolved.yaml`

The resolved schema-v1 configuration used to create the run. Known sensitive configuration field
names are recursively replaced with `[REDACTED]` before persistence. Mapping keys that are agent
IDs remain identifiers rather than being interpreted as secret field names. This is a defensive
filter, not a general secret detector; configuration should not contain secrets.

### `request.md`

The exact top-level debate request as recorded by the run creator. It is UTF-8 text and is untrusted
content when read back into a prompt.

### `manifest.json`

The current materialized run index. It is rewritten atomically. Its baseline fields include:

| Field | Meaning |
|---|---|
| `schema_version` | Run-format version, currently `1`. |
| `run_id` | Immutable ID matching the directory name. |
| `status` | Current orchestrator status. |
| `created_at`, `started_at`, `updated_at`, `finished_at` | RFC 3339 UTC lifecycle timestamps. |
| `resumed_at`, `resume_count` | Most recent accepted resume and number of accepted resumes. |
| `config_snapshot`, `request_artifact`, `events_artifact` | Root artifact paths. |
| `event_count` | Highest committed event sequence. |
| `invocations` | Provider invocation indexes, including participant and Judge attempts. |
| `judges` | Judge artifact indexes by round. |
| `artifacts` | Map from relative path to size and digest metadata. |
| `rounds` | Orchestrator round summaries. |
| `final_synthesis`, `final_artifact` | Final result when available. |
| `error`, `error_details`, `failure_artifact` | Failure information when available. |

Fields may be absent until the corresponding lifecycle action occurs. Readers should use
`schema_version` and tolerate additive fields, but must not silently accept an unknown schema
version.

Every `artifacts` value has:

```json
{
  "content_sha256": "<lowercase SHA-256 hex>",
  "sha256": "<same digest, compatibility alias>",
  "size_bytes": 123
}
```

Digests cover exact file bytes.

### `events.jsonl`

An append-only UTF-8 JSON Lines stream. Each line is one object:

```json
{
  "content_sha256": "<digest>",
  "event_id": "<uuid hex>",
  "payload": {},
  "sequence": 1,
  "timestamp": "2026-07-26T14:30:12.123456Z",
  "type": "run_created"
}
```

`sequence` starts at 1 and increases monotonically for the exclusive writer. The event digest is
computed over the canonical JSON encoding of the other five fields. The manifest also records the
current digest and size of the entire event file.

Strict resume verifies the whole-file hash and size, the exact line count against `event_count`,
contiguous sequence numbers, valid JSON objects, and every per-event digest. These checks detect
accidental or uncoordinated modification; they are not signatures or an external transparency log.
An attacker able to recompute and rewrite the event file, all event digests, and the manifest can
still forge them.

Events carry state transitions and artifact pointers. Full prompts and provider streams belong in
their dedicated files rather than being duplicated into event payloads.

### `run.lock`

The artifact store holds a non-blocking exclusive lock for its lifetime. It opens `run.lock`
without following symlinks, then verifies that the opened object is the same regular, single-link
file visible at the expected path before truncating diagnostic metadata. The file contains PID,
hostname, run ID, and acquisition or release timestamps. A second writer receives a locked-run
error instead of racing. The file may remain after release; the OS lock, not file existence,
determines ownership.

The store also binds the opened run-directory inode to a stable directory file descriptor. Every
artifact read, append, atomic replacement, and manifest replacement is relative to that descriptor.
If the visible run-directory path is renamed, replaced, or changed into a link while the store is
open, later operations fail; they never redirect a write into the replacement directory.

## Participant invocation

`rounds/001/<stage>/<participant>/<invocation-id>/` contains:

- `request.md`: the fully assembled role request sent to the adapter;
- `stdout.log`: captured standard output, bounded by the transport evidence budget;
- `stderr.log`: captured standard error, bounded by the transport evidence budget;
- `final.md`: normalized final response selected by the adapter;
- `meta.json`: invocation ID, monotonic invocation sequence, kind, attempt, status, round, stage,
  participant, recording time, normalized adapter execution metadata, and
  `transport_truncated` / `transport_observed_chars` audit fields.

For adapters whose stdout is authoritative, crossing the transport budget is an `output_limit`
failure. Codex invocations with a separately managed final artifact instead keep draining the
provider JSON stream after the evidence budget is full. Their retained logs are bounded,
`transport_truncated` is true, and `transport_observed_chars` records the full decoded volume.

The manifest indexes each invocation by ID, monotonic `invocation_sequence`, `kind`
(`participant` or `judge_attempt`), attempt, status, round, stage, participant, path, recording
time, and the five artifact records. Internal Judge calls are discriminated by `kind`, not by
reserving user ID prefixes. Strict resume cross-checks the index identity and status against the
verified `meta.json` and requires sequences to be ordered and contiguous. The index is append-only
for invocation evidence. A retry or resumed incomplete round always gets a new invocation ID and
directory; it never replaces an earlier attempt. When reconstructing a completed round, the newest
attempt for each participant supersedes every older attempt even if the newest attempt failed; only
then is successful evidence admitted. Raw streams are retained even when the invocation fails so
diagnosis does not depend on terminal output.

Provider-owned Codex output files are first written into a newly created private per-invocation
directory below the engine's POSIX state root, never the workspace or system temporary directory.
The adapter accepts only a stable regular file with one link and does not follow symlinks. The
scratch directory is outside the canonical artifact namespace and is removed after validated bytes
are copied into the immutable invocation directory. Consumers must not rely on scratch paths or
treat them as run artifacts. This protects the built-in `read_only` and `workspace_write` modes;
`danger_full_access` still requires an external containment boundary. A process or host hard-kill
can leave an orphaned private scratch directory; it is not resumable evidence and may be removed
after confirming that no engine process is using it.

## Judge invocation

`rounds/001/judge/` separates raw model output from parsed protocol data:

- `request.md`: complete Judge request;
- `raw.md`: raw Judge response;
- `decision.json`: validated Judge schema-v1 object;
- `meta.json`: round, recording time, and adapter execution metadata.

`decision.json` is evidence supplied to the deterministic stop evaluator. Its presence does not by
itself mean the run finalized. The Judge index contains only decisions that passed schema and
semantic validation. Invalid output and exhausted repair attempts remain in their immutable
provider invocation artifacts; they do not create `decision.json`, do not enter the Judge index,
and do not complete the round barrier.

## Failure and final result

`failures/<timestamp>-<uuid>.json` records an error type, message, timestamp, and optional structured
details. The manifest points to the latest failure artifact and the event log records the
transition. It persists cumulative elapsed time atomically with every completed provider
invocation, at round transitions, on controlled failure, and after successful resume preflight.
Repeated resume therefore cannot reset already checkpointed work. An abrupt host loss during an
active provider call can lose that uncheckpointed fraction; use an external supervisor when an
absolute calendar-time deadline must survive hard kills.

`final.md` is the user-facing synthesis when one is available. Terminal meaning comes from the
manifest status and stop record, not from the existence or wording of this file. In particular,
exhausting a limit must not be interpreted as consensus.

## Durability protocol

Directories and files use private POSIX modes on a best-effort basis:

- directories: `0700`;
- files: `0600`.

Files that replace prior state—including the manifest and JSON artifacts—use this sequence,
relative to the stable run-directory descriptor:

1. create a same-directory temporary file exclusively;
2. write UTF-8 or bytes;
3. flush and `fsync` the file;
4. apply private permissions;
5. `os.replace` the target;
6. `fsync` the parent directory where supported.

Events use descriptor-relative `O_APPEND`, complete-line writes, and `fsync`. The store serializes
updates within the process and relies on `run.lock` for cooperative cross-process exclusivity.

A power loss can still occur between updates to two different files. Readers therefore use the
manifest hashes and event sequence instead of assuming a multi-file transaction. Integrity
mismatch is a resume error requiring inspection, not permission to guess which copy is canonical.

## Resume protocol

Opening a run for resume treats both `manifest.json` and every manifest-selected path as untrusted.
It performs these checks, in order, before configuration loading, provider probing, or new work:

1. the target exists as a non-symlink directory;
2. its run ID is a safe path component;
3. the process safely opens and acquires the non-blocking exclusive lock;
4. `manifest.json` is a JSON object with exactly supported schema version `1`, a known lifecycle
   status, and a `run_id` matching the directory;
5. fixed root pointers have their canonical values, and every artifact/index path is a safe
   relative path contained by this run;
6. invocation and Judge indexes have valid types, identities, canonical path shapes, and ordering;
7. every manifest-indexed artifact exists as the expected regular, single-link file and matches
   its recorded size and SHA-256 digest;
8. invocation indexes match their verified metadata, and the event-log count, sequence, JSON, and
   per-event digests agree;
9. lifecycle eligibility is checked, then the completed Judge rounds must form a contiguous prefix
   with one schema-valid decision artifact per indexed round;
10. only then is `config.resolved.yaml` loaded, external prompt hashes checked, and provider
    preflight run;
11. after resume eligibility is established, the store increments `resume_count`, sets
    `resumed_at`, appends `run_resumed`, and starts the incomplete round.

Failed verification, concurrent ownership, an incompatible schema, or an ineligible lifecycle
state refuses resume without trusting a manifest-selected external path. Provider preflight failure
does not start debate work. Existing invocation directories remain immutable throughout resume.

Do not manually edit a run that may later be resumed. Copy it first if human annotation or
forensics are required.

## Portability and confidentiality

JSON is deterministic UTF-8 with sorted keys; JSONL is one compact object per line; YAML and
Markdown are UTF-8. Version 0.1 officially supports POSIX platforms only (Linux and macOS). Windows
is not supported because the process-group, advisory-locking, link, private-mode, and directory
durability contract does not have equivalent behavior in this release.

The directory contains sensitive prompt and response material and is not encrypted. A run
directory may be copied for archival after it is closed, but a ZIP archive is a transport copy, not
the canonical live run or a substitute for integrity verification.
