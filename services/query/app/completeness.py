"""PostgreSQL authority for frozen, pipeline-complete experiment analyses."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.models.schemas import ExperimentAnalysisDecisionSnapshot


_STREAM_ID_PATTERN = re.compile(r"^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_STREAM_ID_PART = 2**64 - 1


@dataclass(frozen=True)
class ExperimentBoundaryAuthority:
    """One immutable analysis boundary and its current coverage state."""

    state: Literal["pending", "covered", "degraded", "quarantined"]
    marker_stream_id: str | None
    marker_stream_id_parts: tuple[int, int] | None
    snapshot: ExperimentAnalysisDecisionSnapshot | None
    failure_reason: str | None = None


def parse_stream_id(value: str) -> tuple[int, int]:
    """Parse one canonical Redis stream ID without lossy string ordering."""
    if not isinstance(value, str) or _STREAM_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid canonical Redis stream ID")
    milliseconds, sequence = value.split("-", 1)
    parts = int(milliseconds), int(sequence)
    if any(part > _MAX_STREAM_ID_PART for part in parts):
        raise ValueError("Redis stream ID exceeds the canonical unsigned range")
    return parts


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def boundary_token(
    *,
    project_id: str,
    experiment_key: str,
    config_version: int,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Derive the stable identity used to idempotently insert a Redis marker."""
    payload = {
        "config_version": config_version,
        "experiment_key": experiment_key,
        "project_id": project_id,
        "window_end": _utc_iso(window_end),
        "window_start": _utc_iso(window_start),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _row_value(row: Any, field: str) -> Any:
    try:
        return row[field]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"pipeline authority omitted {field}") from exc


def _same_instant(left: datetime, right: datetime) -> bool:
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def _canonical_snapshot_json(snapshot: ExperimentAnalysisDecisionSnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_snapshot_payload(
    value: Any,
    expected_sha256: Any,
) -> ExperimentAnalysisDecisionSnapshot:
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise RuntimeError("experiment snapshot digest is invalid")
    if isinstance(value, str):
        snapshot = ExperimentAnalysisDecisionSnapshot.model_validate_json(value)
    else:
        snapshot = ExperimentAnalysisDecisionSnapshot.model_validate(value)
    observed_sha256 = hashlib.sha256(
        _canonical_snapshot_json(snapshot).encode()
    ).hexdigest()
    if observed_sha256 != expected_sha256:
        raise RuntimeError("experiment snapshot digest does not match its payload")
    return snapshot


async def get_or_create_experiment_boundary(
    pool,
    *,
    project_id: str,
    experiment_key: str,
    config_version: int,
    window_start: datetime,
    window_end: datetime,
) -> ExperimentBoundaryAuthority:
    """Freeze a boundary request, then evaluate it against the ACK frontier."""
    stream_key = f"events:raw:{project_id}"
    marker_token = boundary_token(
        project_id=project_id,
        experiment_key=experiment_key,
        config_version=config_version,
        window_start=window_start,
        window_end=window_end,
    )

    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO experiment_analysis_boundaries (
                    project_id,
                    experiment_key,
                    config_version,
                    stream_key,
                    window_start,
                    window_end,
                    marker_token
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (project_id, experiment_key, config_version)
                DO NOTHING
                """,
                project_id,
                experiment_key,
                config_version,
                stream_key,
                window_start,
                window_end,
                marker_token,
            )
            boundary = await connection.fetchrow(
                """
                SELECT
                    stream_key,
                    window_start,
                    window_end,
                    marker_token,
                    marker_stream_id,
                    marker_publish_state,
                    marker_publish_failure_code
                FROM experiment_analysis_boundaries
                WHERE project_id = $1
                  AND experiment_key = $2
                  AND config_version = $3
                FOR SHARE
                """,
                project_id,
                experiment_key,
                config_version,
            )
            if boundary is None:
                raise RuntimeError("experiment boundary insert was not observable")
            if (
                _row_value(boundary, "stream_key") != stream_key
                or not _same_instant(_row_value(boundary, "window_start"), window_start)
                or not _same_instant(_row_value(boundary, "window_end"), window_end)
                or _row_value(boundary, "marker_token") != marker_token
            ):
                raise RuntimeError(
                    "experiment metadata conflicts with its immutable boundary"
                )

            snapshot_row = await connection.fetchrow(
                """
                SELECT boundary_stream_id, snapshot_payload, snapshot_sha256
                FROM experiment_analysis_snapshots
                WHERE project_id = $1
                  AND experiment_key = $2
                  AND config_version = $3
                """,
                project_id,
                experiment_key,
                config_version,
            )
            marker_stream_id = _row_value(boundary, "marker_stream_id")
            marker_publish_state = _row_value(
                boundary,
                "marker_publish_state",
            )
            marker_publish_failure_code = _row_value(
                boundary,
                "marker_publish_failure_code",
            )
            if snapshot_row is not None:
                if (
                    marker_publish_state != "published"
                    or marker_stream_id is None
                    or (
                        _row_value(snapshot_row, "boundary_stream_id")
                        != marker_stream_id
                    )
                ):
                    raise RuntimeError(
                        "experiment snapshot conflicts with its stream boundary"
                    )
                snapshot = _validate_snapshot_payload(
                    _row_value(snapshot_row, "snapshot_payload"),
                    _row_value(snapshot_row, "snapshot_sha256"),
                )
                return ExperimentBoundaryAuthority(
                    state="covered",
                    marker_stream_id=marker_stream_id,
                    marker_stream_id_parts=parse_stream_id(marker_stream_id),
                    snapshot=snapshot,
                )

            if marker_publish_state == "quarantined":
                if (
                    marker_stream_id is not None
                    or not isinstance(marker_publish_failure_code, str)
                    or not marker_publish_failure_code
                ):
                    raise RuntimeError(
                        "quarantined experiment boundary authority is invalid"
                    )
                return ExperimentBoundaryAuthority(
                    state="quarantined",
                    marker_stream_id=None,
                    marker_stream_id_parts=None,
                    snapshot=None,
                    failure_reason=marker_publish_failure_code,
                )
            if marker_publish_state == "pending":
                if marker_stream_id is not None:
                    raise RuntimeError(
                        "pending experiment boundary has a stream marker"
                    )
                return ExperimentBoundaryAuthority(
                    state="pending",
                    marker_stream_id=None,
                    marker_stream_id_parts=None,
                    snapshot=None,
                )
            if marker_publish_state != "published" or marker_stream_id is None:
                raise RuntimeError(
                    "experiment boundary publication state is invalid"
                )
            marker_parts = parse_stream_id(marker_stream_id)

            watermark = await connection.fetchrow(
                """
                SELECT
                    stream_key,
                    provenance_start_stream_id,
                    contiguous_stream_id,
                    status,
                    failure_reason
                FROM event_pipeline_watermarks
                WHERE project_id = $1
                """,
                project_id,
            )
            if watermark is None:
                return ExperimentBoundaryAuthority(
                    state="pending",
                    marker_stream_id=marker_stream_id,
                    marker_stream_id_parts=marker_parts,
                    snapshot=None,
                )
            if _row_value(watermark, "stream_key") != stream_key:
                raise RuntimeError("pipeline watermark stream authority is invalid")
            if _row_value(watermark, "status") == "degraded":
                return ExperimentBoundaryAuthority(
                    state="degraded",
                    marker_stream_id=marker_stream_id,
                    marker_stream_id_parts=marker_parts,
                    snapshot=None,
                    failure_reason=_row_value(watermark, "failure_reason"),
                )
            if _row_value(watermark, "status") != "healthy":
                raise RuntimeError("pipeline watermark status is invalid")

            provenance_start = parse_stream_id(
                _row_value(watermark, "provenance_start_stream_id")
            )
            contiguous = parse_stream_id(_row_value(watermark, "contiguous_stream_id"))
            if provenance_start > marker_parts:
                return ExperimentBoundaryAuthority(
                    state="degraded",
                    marker_stream_id=marker_stream_id,
                    marker_stream_id_parts=marker_parts,
                    snapshot=None,
                    failure_reason="pipeline_provenance_unavailable",
                )
            return ExperimentBoundaryAuthority(
                state="covered" if contiguous >= marker_parts else "pending",
                marker_stream_id=marker_stream_id,
                marker_stream_id_parts=marker_parts,
                snapshot=None,
            )


async def persist_experiment_snapshot(
    pool,
    *,
    project_id: str,
    experiment_key: str,
    config_version: int,
    boundary_stream_id: str,
    snapshot: ExperimentAnalysisDecisionSnapshot,
) -> ExperimentAnalysisDecisionSnapshot:
    """Insert exactly one immutable snapshot and return the winning payload."""
    parse_stream_id(boundary_stream_id)
    encoded = _canonical_snapshot_json(snapshot)
    digest = hashlib.sha256(encoded.encode()).hexdigest()

    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO experiment_analysis_snapshots (
                    project_id,
                    experiment_key,
                    config_version,
                    boundary_stream_id,
                    snapshot_payload,
                    snapshot_sha256
                )
                SELECT $1, $2, $3, $4, $5::jsonb, $6
                FROM experiment_analysis_boundaries AS boundary
                INNER JOIN event_pipeline_watermarks AS watermark
                    ON watermark.project_id = boundary.project_id
                   AND watermark.stream_key = boundary.stream_key
                WHERE boundary.project_id = $1
                  AND boundary.experiment_key = $2
                  AND boundary.config_version = $3
                  AND boundary.marker_stream_id = $4
                  AND watermark.status = 'healthy'
                  AND (
                      split_part(watermark.contiguous_stream_id, '-', 1)::numeric
                          > split_part(boundary.marker_stream_id, '-', 1)::numeric
                      OR (
                          split_part(watermark.contiguous_stream_id, '-', 1)::numeric
                              = split_part(boundary.marker_stream_id, '-', 1)::numeric
                          AND split_part(
                              watermark.contiguous_stream_id,
                              '-',
                              2
                          )::numeric >= split_part(
                              boundary.marker_stream_id,
                              '-',
                              2
                          )::numeric
                      )
                  )
                FOR SHARE OF boundary, watermark
                ON CONFLICT (project_id, experiment_key, config_version)
                DO NOTHING
                """,
                project_id,
                experiment_key,
                config_version,
                boundary_stream_id,
                encoded,
                digest,
            )
            stored = await connection.fetchrow(
                """
                SELECT boundary_stream_id, snapshot_payload, snapshot_sha256
                FROM experiment_analysis_snapshots
                WHERE project_id = $1
                  AND experiment_key = $2
                  AND config_version = $3
                FOR SHARE
                """,
                project_id,
                experiment_key,
                config_version,
            )
            if stored is None:
                raise RuntimeError("verified snapshot was not persisted")
            if _row_value(stored, "boundary_stream_id") != boundary_stream_id:
                raise RuntimeError("stored snapshot uses a different boundary")
            return _validate_snapshot_payload(
                _row_value(stored, "snapshot_payload"),
                _row_value(stored, "snapshot_sha256"),
            )
