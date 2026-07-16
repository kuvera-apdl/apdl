# Experimental v2 SQL

These SQL files belong to the unsupported ETL design prototype. They are not
release migrations, are not applied by `make migrate-clickhouse`, and have no
supported producer, loader, replay command, reconciliation job, or query
consumer.

The APDL 0.3.0 developer-preview event contract is the strict flat ingestion
schema persisted by `pipeline/redis/clickhouse_writer.py` to the `events` table.
Do not move these files into `pipeline/clickhouse/migrations/` or wire a runtime
producer until an explicit cutover includes backfill/replay, atomic publication,
query migration, parity reconciliation, rollback, and upgrade tests.
