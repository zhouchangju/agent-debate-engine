# Design request: crash-safe local job queue

Design the first version of a local job queue for a single-user Python 3.11 CLI application.

Constraints:

- Use the Python standard library and SQLite; do not require a server or background daemon.
- Preserve submitted jobs and their terminal results across process crashes.
- Workers claim jobs using at-least-once delivery. A killed worker's claim must eventually expire.
- Duplicate execution is possible, so the API must make idempotency expectations explicit.
- Support 10,000 queued jobs and up to four worker processes on one machine.
- Operators must be able to inspect queue state, retry a failed job, and explain why a job is stuck.
- Treat job payloads as sensitive. Do not place payload data in routine logs.
- Schema evolution must be forward-only and recoverable from a documented backup.

The result should include component boundaries, the state machine and transaction boundaries,
failure recovery, security considerations, migration strategy, observability, and executable
acceptance tests. Identify assumptions rather than silently filling gaps.
