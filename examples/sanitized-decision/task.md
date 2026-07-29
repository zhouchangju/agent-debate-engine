# Synthetic decision request

A five-person team maintains a local Python CLI backed by SQLite. It expects three times its current
usage within one year. Compare:

- keeping SQLite with clearer transaction and recovery boundaries;
- migrating to PostgreSQL and operating a separate database service.

Evaluate correctness, recovery, operating cost, migration risk, observability, and reversibility.
State which evidence would change the recommendation.
