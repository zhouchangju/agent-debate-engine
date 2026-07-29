# Illustrative synthesis

Status: `finalized` in this documentation example only. This file is not a provider transcript.

## Recommendation

Keep SQLite for the current single-machine CLI while defining a storage interface and collecting
evidence about write contention, database size, recovery time, and multi-host demand.

## Accepted decisions

- Separate storage access behind a narrow repository boundary.
- Add backup-and-restore tests before changing database technology.
- Record lock wait time, transaction latency, database growth, and recovery duration.

## Rejected option

Migrating immediately to PostgreSQL is rejected because projected usage growth alone does not prove
that the product needs multi-host access or can absorb service-operation cost.

## Unresolved issue

If concurrent writers, remote access, or recovery objectives exceed measured SQLite behavior, rerun
the decision with those measurements and a concrete PostgreSQL operating model.
