"""Fail-closed capability gates for experiment decision readiness."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.config_client import assert_experiment_analysis_capability


REQUIRED_POSTGRES_MIGRATION = (
    38,
    "038_experiment_data_completeness.sql",
)
REQUIRED_CLICKHOUSE_MIGRATION = (
    16,
    "016_personal_data_retention.sql",
)
REQUIRED_POSTGRES_COLUMNS = frozenset(
    {
        ("event_pipeline_watermarks", "project_id"),
        ("event_pipeline_watermarks", "stream_key"),
        ("event_pipeline_watermarks", "provenance_start_stream_id"),
        ("event_pipeline_watermarks", "contiguous_stream_id"),
        ("event_pipeline_watermarks", "status"),
        ("event_pipeline_watermarks", "failure_reason"),
        ("experiment_analysis_boundaries", "project_id"),
        ("experiment_analysis_boundaries", "experiment_key"),
        ("experiment_analysis_boundaries", "config_version"),
        ("experiment_analysis_boundaries", "stream_key"),
        ("experiment_analysis_boundaries", "window_start"),
        ("experiment_analysis_boundaries", "window_end"),
        ("experiment_analysis_boundaries", "marker_token"),
        ("experiment_analysis_boundaries", "marker_stream_id"),
        ("experiment_analysis_boundaries", "requested_at"),
        ("experiment_analysis_boundaries", "marked_at"),
        ("experiment_analysis_snapshots", "project_id"),
        ("experiment_analysis_snapshots", "experiment_key"),
        ("experiment_analysis_snapshots", "config_version"),
        ("experiment_analysis_snapshots", "boundary_stream_id"),
        ("experiment_analysis_snapshots", "snapshot_payload"),
        ("experiment_analysis_snapshots", "snapshot_sha256"),
    }
)
REQUIRED_CLICKHOUSE_COLUMNS = frozenset(
    {
        ("events", "project_id"),
        ("events", "message_id"),
        ("events", "event_name"),
        ("events", "timestamp"),
        ("events", "received_at"),
        ("events", "properties"),
        ("events", "source_stream"),
        ("events", "source_stream_id"),
        ("events", "source_stream_id_ms"),
        ("events", "source_stream_id_seq"),
        ("events", "event_date"),
        ("experiment_event_deliveries", "project_id"),
        ("experiment_event_deliveries", "message_id"),
        ("experiment_event_deliveries", "event_type"),
        ("experiment_event_deliveries", "event_name"),
        ("experiment_event_deliveries", "user_id"),
        ("experiment_event_deliveries", "anonymous_id"),
        ("experiment_event_deliveries", "session_id"),
        ("experiment_event_deliveries", "timestamp"),
        ("experiment_event_deliveries", "received_at"),
        ("experiment_event_deliveries", "properties"),
        ("experiment_event_deliveries", "source_stream"),
        ("experiment_event_deliveries", "source_stream_id"),
        ("experiment_event_deliveries", "source_stream_id_ms"),
        ("experiment_event_deliveries", "source_stream_id_seq"),
        ("experiment_event_deliveries", "event_date"),
        ("feature_flag_exposures", "project_id"),
        ("feature_flag_exposures", "flag_key"),
        ("feature_flag_exposures", "reason"),
        ("feature_flag_exposures", "config_version"),
        ("feature_flag_exposures", "first_exposure"),
        ("feature_flag_exposures", "source_stream"),
        ("feature_flag_exposures", "source_stream_id"),
        ("feature_flag_exposures", "source_stream_id_ms"),
        ("feature_flag_exposures", "source_stream_id_seq"),
        ("feature_flag_exposures", "event_date"),
        ("identity_alias_assertions", "project_id"),
        ("identity_alias_assertions", "source_stream"),
        ("identity_alias_assertions", "source_stream_id"),
        ("identity_alias_assertions", "source_stream_id_ms"),
        ("identity_alias_assertions", "source_stream_id_seq"),
    }
)
REQUIRED_CLICKHOUSE_ENGINES = {
    "events": "ReplacingMergeTree",
    "experiment_event_deliveries": "ReplacingMergeTree",
    "feature_flag_exposures": "ReplacingMergeTree",
    "identity_alias_assertions": "ReplacingMergeTree",
}


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"schema capability row is missing {key}") from exc


async def assert_postgres_decision_schema(pool: Any) -> None:
    """Prove Query can use every PostgreSQL completeness authority."""

    async with pool.acquire() as connection:
        ledger_exists = await connection.fetchval(
            "SELECT to_regclass('public.apdl_schema_migrations') IS NOT NULL"
        )
        if ledger_exists is not True:
            raise RuntimeError("PostgreSQL migration ledger is missing")

        version, migration_name = REQUIRED_POSTGRES_MIGRATION
        applied_name = await connection.fetchval(
            "SELECT name FROM apdl_schema_migrations WHERE version = $1",
            version,
        )
        if applied_name != migration_name:
            raise RuntimeError(
                f"required PostgreSQL migration is not applied: {migration_name}"
            )

        tables = sorted({table for table, _ in REQUIRED_POSTGRES_COLUMNS})
        rows = await connection.fetch(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            tables,
        )
        available = {
            (_row_value(row, "table_name"), _row_value(row, "column_name"))
            for row in rows
        }
        missing = sorted(REQUIRED_POSTGRES_COLUMNS - available)
        if missing:
            formatted = ", ".join(f"{table}.{column}" for table, column in missing)
            raise RuntimeError(
                f"PostgreSQL decision schema is incomplete: {formatted}"
            )

        privileges_ready = await connection.fetchval(
            """
            SELECT
                has_table_privilege(
                    current_user,
                    'public.event_pipeline_watermarks',
                    'SELECT'
                )
                AND has_table_privilege(
                    current_user,
                    'public.experiment_analysis_boundaries',
                    'SELECT'
                )
                AND has_table_privilege(
                    current_user,
                    'public.experiment_analysis_boundaries',
                    'INSERT'
                )
                AND has_table_privilege(
                    current_user,
                    'public.experiment_analysis_snapshots',
                    'SELECT'
                )
                AND has_table_privilege(
                    current_user,
                    'public.experiment_analysis_snapshots',
                    'INSERT'
                )
            """
        )
        if privileges_ready is not True:
            raise RuntimeError(
                "PostgreSQL decision tables are not usable by the Query principal"
            )


async def assert_clickhouse_decision_schema(client: Any) -> None:
    """Prove the delivery and provenance schemas required by final decisions."""

    version, migration_name = REQUIRED_CLICKHOUSE_MIGRATION
    ledger_rows = await client.execute(
        """
        SELECT name
        FROM apdl_schema_migrations FINAL
        WHERE version = %(migration_version)s
        """,
        {"migration_version": version},
    )
    if ledger_rows != [{"name": migration_name}]:
        raise RuntimeError(
            f"required ClickHouse migration is not applied: {migration_name}"
        )

    column_rows = await client.execute(
        """
        SELECT table, name
        FROM system.columns
        WHERE database = currentDatabase()
          AND table IN (
              'events',
              'experiment_event_deliveries',
              'feature_flag_exposures',
              'identity_alias_assertions'
          )
        """,
        {},
    )
    available = {
        (_row_value(row, "table"), _row_value(row, "name")) for row in column_rows
    }
    missing = sorted(REQUIRED_CLICKHOUSE_COLUMNS - available)
    if missing:
        formatted = ", ".join(f"{table}.{column}" for table, column in missing)
        raise RuntimeError(f"ClickHouse decision schema is incomplete: {formatted}")

    engine_rows = await client.execute(
        """
        SELECT name, engine
        FROM system.tables
        WHERE database = currentDatabase()
          AND name IN (
              'events',
              'experiment_event_deliveries',
              'feature_flag_exposures',
              'identity_alias_assertions'
          )
        """,
        {},
    )
    engines = {
        _row_value(row, "name"): _row_value(row, "engine") for row in engine_rows
    }
    if engines != REQUIRED_CLICKHOUSE_ENGINES:
        raise RuntimeError("ClickHouse decision table engines are incompatible")


async def assert_decision_dependencies_ready(
    clickhouse_client: Any,
    postgres_pool: Any,
) -> None:
    """Fail startup unless every final-decision dependency is compatible."""

    await asyncio.gather(
        assert_clickhouse_decision_schema(clickhouse_client),
        assert_postgres_decision_schema(postgres_pool),
        assert_experiment_analysis_capability(),
    )
