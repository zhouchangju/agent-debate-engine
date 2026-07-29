# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use semantic versioning.

## [Unreleased]

### Added

- Versioned `agent-debate` Skill for natural-language technical reviews, previews, and resume.
- Safe `quick`, `standard`, and `deep` technical-review presets for embedding callers.
- A structured JSON Skill runner with request-owned artifact roots and actionable failure output.
- Independent transport and authoritative-final-output budgets, with legacy `max_output`
  configurations retaining their previous shared-limit behavior.
- Bounded Codex JSON-event capture: managed final-output invocations now continue after transport
  evidence reaches its limit while recording truncation and total observed characters.
- Successful managed-output Codex invocations now clean up helper processes without discarding the
  completed answer; commands without that explicit contract still fail closed on residual children.
- Source-tree Skill runs now propagate the package import path to detached Dashboard processes and
  surface child exit diagnostics when startup fails.
- One-hour technical-review participant timeouts, with proportionally larger quick, standard, and
  deep run budgets for repository-wide analysis.
- Fail-closed CI contract tests, pinned GitHub Actions, bounded jobs, and tag/manual triggers.
- Public support, provenance, release, contribution, issue, and pull-request boundaries.
- A sanitized decision-artifact walkthrough that contains no provider or local runtime data.

### Fixed

- Dashboard lint and strict-mypy failures that kept every public quality-matrix job red.

## [0.1.0] - 2026-07-26

### Added

- Initial typed Python package and command-line application.
- Version-documented Codex, Kimi, and generic CLI adapters with bounded, cancellable subprocess
  execution and fail-closed permission contracts.
- Safe two-profile Codex starter workflow; Kimi 0.29.1 is exposed only as an explicit,
  externally sandboxed full-access option.
- Sequential-stage/parallel-participant debate workflow.
- Structured Judge v1 protocol and deterministic convergence rules.
- Auditable, resumable run artifacts with immutable invocation attempts and verified checkpoints.

### Security

- Generic commands always require an unsafe runtime acknowledgement and cannot execute in parallel
  stages because their permission labels are not engine-enforceable.
- Resume acquires the run lock and verifies the strict schema-v1 manifest, contained paths, ordered
  indexes, artifact hashes, metadata cross-links, event sequence, lifecycle, and Judge barriers
  before loading persisted configuration.
- Canonical artifact I/O is bound to a stable run-directory descriptor, preventing directory
  replacement from redirecting writes.
- Generic diagnostics never execute arbitrary configured commands; built-in probes validate the
  locally verified Codex 0.145.x and Kimi 0.29.1 product contracts.
- Codex final-output scratch space is outside workspace and system-temporary writable roots, with
  no-follow, single-link, stable-inode validation before ingestion.
- Project initialization rejects symbolic-link components and rolls back every interruption,
  retaining recovery backups if rollback itself cannot finish.
- Built-in adapter argv prevents configuration/profile/feature flags from overriding permission,
  prompt, workspace, and output controls.
- Version 0.1 officially supports POSIX platforms only; Windows is not supported.
