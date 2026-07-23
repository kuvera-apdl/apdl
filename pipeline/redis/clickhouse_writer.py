"""
Redis Streams to ClickHouse event writer.
Reads events from Redis Streams and batch-inserts into ClickHouse.

Each project's events live in a separate stream keyed as events:raw:{project_id}.
This writer uses consumer groups for reliable, at-least-once delivery with
automatic retry on ClickHouse flush failures.

Usage:
    REDIS_URL=redis://localhost:6379 \
    CLICKHOUSE_NATIVE_URL=clickhouse://apdl:apdl_dev@localhost:9000/apdl \
    python clickhouse_writer.py
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import redis.asyncio as redis
from clickhouse_driver import Client as ClickHouseClient
from clickhouse_driver.errors import TypeMismatchError

logger = logging.getLogger(__name__)

STREAM_PREFIX = "events:raw:"
DLQ_STREAM_PREFIX = "events:dlq:"
CONSUMER_GROUP = "clickhouse-writer"
CONSUMER_GROUP_START_ID = "0-0"
DEFAULT_DLQ_MAXLEN = 10_000
FLUSH_RETRY_BASE_SECONDS = 1.0
FLUSH_RETRY_MAX_SECONDS = 30.0
PENDING_CLAIM_IDLE_MS = 60_000
PENDING_CLAIM_INTERVAL_SECONDS = 30.0
CLICKHOUSE_CONNECT_TIMEOUT_SECONDS = 5.0
CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS = 30.0
CLICKHOUSE_SYNC_REQUEST_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
EVENT_STREAM_MAX_ENTRIES = 1_000_000
EVENT_STREAM_ALERT_ENTRIES = 750_000
EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS = 30.0
MAX_EVENT_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_EVENT_FUTURE_SKEW_SECONDS = 5 * 60
STREAM_DISCOVERY_INTERVAL_SECONDS = 5.0
BOUNDARY_MARKER_POLL_INTERVAL_SECONDS = 1.0
BOUNDARY_MARKER_MAX_PUBLISH_ATTEMPTS = 5
BOUNDARY_MARKER_RETRY_BASE_SECONDS = 1
BOUNDARY_MARKER_RETRY_MAX_SECONDS = 30
BOUNDARY_MARKER_POSTGRES_MIGRATION_VERSION = 41
BOUNDARY_MARKER_POSTGRES_MIGRATION_NAME = (
    "041_boundary_marker_retry_quarantine.sql"
)
BOUNDARY_MARKER_POSTGRES_MIGRATION_SHA256 = (
    "4ea72fa9dd3589f85ee2077db439bf34611c5d9055b1664e9f06de5f9a21efa2"
)
BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256 = {
    "experiment_analysis_boundaries_observed_stream_identity": (
        "6a8b46cf5323467989510364cbf402f1fb40a4e206e5c5e9399a902b59249e82"
    ),
    "experiment_analysis_boundaries_publish_attempts_check": (
        "a930d5e0fbe3bc2272dde1020c60fd964f013d845f7b637448efbaa2c44de9be"
    ),
    "experiment_analysis_boundaries_publish_failure_check": (
        "dfc4f92db0bbfcae89d9f7fee981952a1414803702d9178ab27ef9cf1f681a47"
    ),
    "experiment_analysis_boundaries_publish_history_check": (
        "d6eef08f418bde62750f9459dc8bf1370ffb36a22730e3909c5b22556bec50b8"
    ),
    "experiment_analysis_boundaries_publish_observed_id_check": (
        "5a8c83223f1ea621195fbd023cea4d8988928a7cf96a489204653908c41b4135"
    ),
    "experiment_analysis_boundaries_publish_state_check": (
        "fd7314c5e9244bb399ab84d7f46502eb69117cbeed76f51e3425c8f68d2421d6"
    ),
}
BOUNDARY_MARKER_POSTGRES_TRIGGER_NAME = (
    "experiment_analysis_boundaries_immutable"
)
BOUNDARY_MARKER_POSTGRES_TRIGGER_DEFINITION = (
    "CREATE TRIGGER experiment_analysis_boundaries_immutable "
    "BEFORE DELETE OR UPDATE ON public.experiment_analysis_boundaries "
    "FOR EACH ROW EXECUTE FUNCTION "
    "enforce_experiment_analysis_boundary_immutability()"
)
BOUNDARY_MARKER_POSTGRES_FUNCTION_NAME = (
    "enforce_experiment_analysis_boundary_immutability"
)
BOUNDARY_MARKER_POSTGRES_FUNCTION_SHA256 = (
    "b36fa821b1282b925e46cdc2de883d0bf07dedebdc487cbf1df2c1099eb8a55e"
)
BOUNDARY_MARKER_OBSERVED_IDENTITY_CONSTRAINT = (
    "experiment_analysis_boundaries_observed_stream_identity"
)
DURABLE_ACK_AUTHORITY_TIMEOUT_SECONDS = 5.0
REDIS_MEMORY_ALERT_RATIO = 0.75
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084
WRITER_SINGLETON_LOCK_ID = 4_158_044_085
MAINTENANCE_INHIBITOR_LOCK_IDS = tuple(
    sorted((MAINTENANCE_INHIBITOR_LOCK_ID, MAINTENANCE_GUARD_LOCK_ID))
)
MAINTENANCE_HEARTBEAT_SECONDS = 1.0
EVENT_INSERT_COLUMNS = (
    "project_id",
    "message_id",
    "event_type",
    "event_name",
    "user_id",
    "anonymous_id",
    "group_id",
    "session_id",
    "timestamp",
    "received_at",
    "properties",
    "traits",
    "context",
    "ip",
    "country",
    "device_type",
    "browser",
    "source_stream",
    "source_stream_id",
    "source_stream_id_ms",
    "source_stream_id_seq",
)
EVENT_EXTERNAL_TABLE_NAME = "apdl_runtime_input"
EVENT_INPUT_STRUCTURE = (
    ("project_id", "String"),
    ("message_id", "String"),
    ("event_type", "LowCardinality(String)"),
    ("event_name", "LowCardinality(String)"),
    ("user_id", "String"),
    ("anonymous_id", "String"),
    ("group_id", "String"),
    ("session_id", "String"),
    ("timestamp", "DateTime64(3)"),
    ("received_at", "DateTime64(3)"),
    ("properties", "String"),
    ("traits", "String"),
    ("context", "String"),
    ("ip", "String"),
    ("country", "LowCardinality(String)"),
    ("device_type", "LowCardinality(String)"),
    ("browser", "LowCardinality(String)"),
    ("source_stream", "String"),
    ("source_stream_id", "String"),
    ("source_stream_id_ms", "UInt64"),
    ("source_stream_id_seq", "UInt64"),
)
EVENT_INSERT_QUERY = (
    f"INSERT INTO events ({', '.join(EVENT_INSERT_COLUMNS)}) "
    f"SELECT {', '.join(EVENT_INSERT_COLUMNS)} "
    f"FROM {EVENT_EXTERNAL_TABLE_NAME} "
    "WHERE throwIf((SELECT (count() = 0) OR "
    "(argMax(writes_blocked, generation) != 0) "
    "FROM apdl_maintenance_gate "
    "WHERE authority = 'runtime-writes'), 'maintenance') = 0"
)


async def _acquire_writer_singleton(connection) -> None:
    """Fail closed unless this backend owns the only writer authority."""
    acquired = await connection.fetchval(
        "SELECT pg_try_advisory_lock($1)",
        WRITER_SINGLETON_LOCK_ID,
    )
    if acquired is not True:
        raise RuntimeError(
            "Another ClickHouse writer owns the singleton consumer-group authority"
        )
    logger.info("Acquired singleton ClickHouse writer authority")


async def _assert_boundary_marker_schema(connection) -> None:
    """Require exact H-10 state before taking singleton runtime authority."""
    ledger_exists = await connection.fetchval(
        "SELECT to_regclass('public.apdl_schema_migrations') IS NOT NULL"
    )
    if ledger_exists is not True:
        raise RuntimeError("PostgreSQL migration ledger is missing")

    migration = await connection.fetchrow(
        """
        SELECT name, checksum
        FROM public.apdl_schema_migrations
        WHERE version = $1
        """,
        BOUNDARY_MARKER_POSTGRES_MIGRATION_VERSION,
    )
    if (
        migration is None
        or migration["name"] != BOUNDARY_MARKER_POSTGRES_MIGRATION_NAME
        or migration["checksum"] != BOUNDARY_MARKER_POSTGRES_MIGRATION_SHA256
    ):
        raise RuntimeError(
            "Required boundary marker PostgreSQL migration is not exact"
        )

    required_columns = {
        "marker_publish_state": ("text", "NO", "'pending'::text"),
        "marker_publish_attempts": ("int2", "NO", "0"),
        "marker_publish_next_attempt_at": (
            "timestamptz",
            "YES",
            "now()",
        ),
        "marker_publish_failure_code": ("text", "YES", None),
        "marker_publish_last_error_at": ("timestamptz", "YES", None),
        "marker_publish_quarantined_at": ("timestamptz", "YES", None),
        "marker_publish_observed_stream_id": ("text", "YES", None),
    }
    column_rows = await connection.fetch(
        """
        SELECT column_name, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'experiment_analysis_boundaries'
          AND column_name = ANY($1::text[])
        """,
        sorted(required_columns),
    )
    observed_columns = {
        row["column_name"]: (
            row["udt_name"],
            row["is_nullable"],
            row["column_default"],
        )
        for row in column_rows
    }
    if observed_columns != required_columns:
        raise RuntimeError("Boundary marker PostgreSQL columns are not exact")

    constraint_rows = await connection.fetch(
        """
        SELECT
            constraint_record.conname,
            constraint_record.contype::text AS contype,
            constraint_record.condeferrable,
            constraint_record.condeferred,
            constraint_record.convalidated,
            pg_get_constraintdef(
                constraint_record.oid,
                false
            ) AS definition
        FROM pg_catalog.pg_constraint AS constraint_record
        WHERE constraint_record.conrelid =
            'public.experiment_analysis_boundaries'::regclass
          AND constraint_record.conname = ANY($1::text[])
        """,
        sorted(BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256),
    )
    if {
        row["conname"] for row in constraint_rows
    } != set(BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256):
        raise RuntimeError(
            "Boundary marker PostgreSQL constraints are incomplete"
        )
    for row in constraint_rows:
        constraint_name = row["conname"]
        expected_type = (
            "u"
            if constraint_name
            == BOUNDARY_MARKER_OBSERVED_IDENTITY_CONSTRAINT
            else "c"
        )
        definition = row["definition"]
        definition_sha256 = (
            hashlib.sha256(definition.encode("utf-8")).hexdigest()
            if isinstance(definition, str)
            else None
        )
        if (
            row["contype"] != expected_type
            or row["condeferrable"] is not False
            or row["condeferred"] is not False
            or row["convalidated"] is not True
            or definition_sha256
            != BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256[constraint_name]
        ):
            raise RuntimeError(
                "Boundary marker PostgreSQL state constraint is not exact"
            )

    trigger = await connection.fetchrow(
        """
        SELECT
            trigger_record.tgname,
            trigger_record.tgenabled::text AS tgenabled,
            trigger_record.tgtype,
            trigger_record.tgisinternal,
            pg_get_triggerdef(trigger_record.oid, false)
                AS trigger_definition,
            function_namespace.nspname AS function_schema,
            function_record.proname AS function_name,
            function_record.prokind::text AS prokind,
            function_record.prosecdef,
            function_record.proleakproof,
            function_record.provolatile::text AS provolatile,
            function_record.proparallel::text AS proparallel,
            function_record.proconfig,
            function_record.pronargs,
            function_record.prorettype::regtype::text AS return_type,
            pg_get_functiondef(function_record.oid)
                AS function_definition
        FROM pg_catalog.pg_trigger AS trigger_record
        JOIN pg_catalog.pg_proc AS function_record
          ON function_record.oid = trigger_record.tgfoid
        JOIN pg_catalog.pg_namespace AS function_namespace
          ON function_namespace.oid = function_record.pronamespace
        WHERE trigger_record.tgrelid =
            'public.experiment_analysis_boundaries'::regclass
          AND trigger_record.tgname = $1
          AND NOT trigger_record.tgisinternal
        """,
        BOUNDARY_MARKER_POSTGRES_TRIGGER_NAME,
    )
    function_definition = (
        trigger["function_definition"] if trigger is not None else None
    )
    function_sha256 = (
        hashlib.sha256(function_definition.encode("utf-8")).hexdigest()
        if isinstance(function_definition, str)
        else None
    )
    if (
        trigger is None
        or trigger["tgname"] != BOUNDARY_MARKER_POSTGRES_TRIGGER_NAME
        or trigger["tgenabled"] != "O"
        or trigger["tgtype"] != 27
        or trigger["tgisinternal"] is not False
        or trigger["trigger_definition"]
        != BOUNDARY_MARKER_POSTGRES_TRIGGER_DEFINITION
        or trigger["function_schema"] != "public"
        or trigger["function_name"]
        != BOUNDARY_MARKER_POSTGRES_FUNCTION_NAME
        or trigger["prokind"] != "f"
        or trigger["prosecdef"] is not False
        or trigger["proleakproof"] is not False
        or trigger["provolatile"] != "v"
        or trigger["proparallel"] != "u"
        or trigger["proconfig"] != ["search_path=pg_catalog, public"]
        or trigger["pronargs"] != 0
        or trigger["return_type"] != "trigger"
        or function_sha256 != BOUNDARY_MARKER_POSTGRES_FUNCTION_SHA256
    ):
        raise RuntimeError(
            "Boundary marker PostgreSQL state trigger is not exact"
        )


async def _heartbeat_writer_singleton(connection) -> None:
    """Prove the checked-out backend still owns singleton writer authority."""
    held_lock_count = await connection.fetchval(
        """
        SELECT count(*)
        FROM pg_catalog.pg_locks
        WHERE pid = pg_backend_pid()
          AND locktype = 'advisory'
          AND mode = 'ExclusiveLock'
          AND granted
          AND classid = 0
          AND objsubid = 1
          AND objid = ($1::bigint)::oid
        """,
        WRITER_SINGLETON_LOCK_ID,
    )
    if held_lock_count != 1:
        raise RuntimeError(
            "PostgreSQL backend no longer holds singleton writer authority"
        )


async def _acquire_maintenance_inhibitor(connection) -> None:
    """Acquire both shared locks in one canonical order on this backend."""
    for lock_id in MAINTENANCE_INHIBITOR_LOCK_IDS:
        await connection.execute(
            "SELECT pg_advisory_lock_shared($1)",
            lock_id,
        )


async def _heartbeat_maintenance_inhibitor(connection) -> None:
    """Require this backend to still own both distinct shared advisory locks."""
    held_lock_count = await connection.fetchval(
        """
        SELECT count(*)
        FROM pg_catalog.pg_locks
        WHERE pid = pg_backend_pid()
          AND locktype = 'advisory'
          AND mode = 'ShareLock'
          AND granted
          AND classid = 0
          AND objsubid = 1
          AND objid::bigint = ANY($1::bigint[])
        """,
        list(MAINTENANCE_INHIBITOR_LOCK_IDS),
    )
    if held_lock_count != len(MAINTENANCE_INHIBITOR_LOCK_IDS):
        raise RuntimeError(
            "PostgreSQL backend no longer holds both maintenance inhibitor locks"
        )


async def _monitor_maintenance_inhibitor(
    connection,
    writer,
    connection_lost: asyncio.Event,
    *,
    heartbeat_seconds: float = MAINTENANCE_HEARTBEAT_SECONDS,
    require_writer_singleton: bool = False,
) -> None:
    """Fence writes when one inhibitor connection becomes uncertain."""
    while not connection_lost.is_set():
        try:
            await asyncio.wait_for(
                connection_lost.wait(),
                timeout=heartbeat_seconds,
            )
        except TimeoutError:
            try:
                await asyncio.wait_for(
                    _heartbeat_maintenance_inhibitor(connection),
                    timeout=heartbeat_seconds,
                )
                if require_writer_singleton:
                    await asyncio.wait_for(
                        _heartbeat_writer_singleton(connection),
                        timeout=heartbeat_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except BaseException:
                connection_lost.set()
    logger.critical(
        "PostgreSQL maintenance inhibitor was lost; stopping ClickHouse writes"
    )
    await writer.stop(flush_buffer=False)


if not 0 < EVENT_STREAM_ALERT_ENTRIES < EVENT_STREAM_MAX_ENTRIES:
    raise RuntimeError("Event stream alert threshold must be below capacity")
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
RFC3339_UTC_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$"
)
CANONICAL_EVENT_TYPES = frozenset({"track", "identify", "group", "page"})
CANONICAL_CONTEXT_FIELDS = frozenset(
    {
        "library",
        "browser",
        "os",
        "device",
        "screen",
        "viewport",
        "page",
        "locale",
        "timezone",
        "referrer",
    }
)
CANONICAL_EVENT_FIELDS = frozenset(
    {
        "event",
        "type",
        "user_id",
        "anonymous_id",
        "group_id",
        "timestamp",
        "properties",
        "traits",
        "context",
        "message_id",
        "session_id",
        # Ingestion-owned authority/metadata added only after public validation.
        "project_id",
        "server_timestamp",
        "ip",
    }
)
CANONICAL_REQUIRED_EVENT_FIELDS = frozenset(
    {
        "event",
        "type",
        "timestamp",
        "server_timestamp",
        "context",
        "message_id",
    }
)
BOUNDARY_MARKER_KIND = "experiment_analysis_boundary"
BOUNDARY_MARKER_FIELDS = frozenset({"message_kind", "boundary_token"})
BOUNDARY_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BOUNDARY_MARKER_DEDUP_PREFIX = "analysis:boundary:"
BOUNDARY_MARKER_DEDUP_INVALID_REPLY = "BOUNDARY_MARKER_DEDUP_INVALID"
BOUNDARY_MARKER_TRANSIENT_FAILURE_CODES = frozenset(
    {
        "event_stream_capacity",
        "redis_publish_failed",
        "boundary_authority_update_failed",
        "unexpected_publish_failure",
    }
)
BOUNDARY_MARKER_TERMINAL_FAILURE_CODES = frozenset(
    {
        "invalid_redis_marker_id",
        "boundary_authority_update_invalid",
        "invalid_boundary_marker_dedup",
        "invalid_stream_authority",
        "invalid_marker_token",
    }
)
BOUNDARY_MARKER_FAILURE_CODES = (
    BOUNDARY_MARKER_TRANSIENT_FAILURE_CODES
    | BOUNDARY_MARKER_TERMINAL_FAILURE_CODES
)
BOUNDARY_MARKER_LUA = """
local function invalid_dedup(existing_id)
    return {'BOUNDARY_MARKER_DEDUP_INVALID', existing_id or ''}
end

local existing_reply = redis.pcall('GET', KEYS[2])
if type(existing_reply) == 'table' and existing_reply.err then
    return invalid_dedup(nil)
end
local existing = existing_reply
if existing then
    if ARGV[4] ~= '' and existing ~= ARGV[4] then
        return invalid_dedup(existing)
    end
    local entries = redis.pcall(
        'XRANGE',
        KEYS[1],
        existing,
        existing,
        'COUNT',
        1
    )
    if type(entries) == 'table' and entries.err then
        return invalid_dedup(existing)
    end
    if #entries ~= 1 or entries[1][1] ~= existing then
        return invalid_dedup(existing)
    end
    local fields = entries[1][2]
    if #fields ~= 4 then
        return invalid_dedup(existing)
    end
    local observed_kind = nil
    local observed_token = nil
    for index = 1, #fields, 2 do
        if fields[index] == 'message_kind' and observed_kind == nil then
            observed_kind = fields[index + 1]
        elseif fields[index] == 'boundary_token' and observed_token == nil then
            observed_token = fields[index + 1]
        else
            return invalid_dedup(existing)
        end
    end
    if observed_kind ~= ARGV[2] or observed_token ~= ARGV[3] then
        return invalid_dedup(existing)
    end
    return existing
end
if ARGV[4] ~= '' then
    return invalid_dedup(nil)
end
local current_entries = redis.call('XLEN', KEYS[1])
if current_entries >= tonumber(ARGV[1]) then
    return redis.error_reply('EVENT_STREAM_CAPACITY_REACHED')
end
local marker_id = redis.call(
    'XADD',
    KEYS[1],
    '*',
    'message_kind',
    ARGV[2],
    'boundary_token',
    ARGV[3]
)
redis.call('SET', KEYS[2], marker_id)
return marker_id
"""


@dataclass(frozen=True)
class BufferedEvent:
    """A parsed row plus the Redis delivery that must remain pending for it."""

    stream_key: str
    message_id: str
    row: dict | None
    boundary_token: str | None = None


@dataclass
class InsertOutcome:
    """Results of inserting a batch while isolating terminal row failures."""

    durable: list[BufferedEvent]
    retry: list[BufferedEvent]
    transient_error: Exception | None = None


@dataclass(frozen=True)
class InFlightInsert:
    """The one synchronous insert currently owned by the DB executor."""

    batch: tuple[BufferedEvent, ...]
    future: asyncio.Future[None]
    query_id: str


@dataclass(frozen=True)
class PendingBoundaryMarker:
    """One database-authoritative marker publication attempt."""

    project_id: str
    experiment_key: str
    config_version: int
    stream_key: Any
    marker_token: Any
    publish_attempts: int
    observed_stream_id: Any = None


class BoundaryMarkerPublishError(RuntimeError):
    """A safe, classified marker failure suitable for durable retry state."""

    def __init__(
        self,
        code: str,
        *,
        terminal: bool,
        observed_stream_id: str | None = None,
    ):
        if code not in BOUNDARY_MARKER_FAILURE_CODES:
            raise ValueError("boundary marker failure code is not canonical")
        if terminal != (code in BOUNDARY_MARKER_TERMINAL_FAILURE_CODES):
            raise ValueError(
                "boundary marker failure terminal classification is invalid"
            )
        super().__init__(code)
        self.code = code
        self.terminal = terminal
        self.observed_stream_id = observed_stream_id


class ClickHouseWriter:
    """Consumes events from Redis Streams and writes to ClickHouse in batches."""

    def __init__(
        self,
        redis_url: str,
        clickhouse_url: str,
        buffer_size: int = 1000,
        flush_interval: float = 5.0,
        dlq_maxlen: int = DEFAULT_DLQ_MAXLEN,
        pending_claim_idle_ms: int = PENDING_CLAIM_IDLE_MS,
        pending_claim_interval: float = PENDING_CLAIM_INTERVAL_SECONDS,
        stream_discovery_interval: float = STREAM_DISCOVERY_INTERVAL_SECONDS,
        clickhouse_connect_timeout: float = CLICKHOUSE_CONNECT_TIMEOUT_SECONDS,
        clickhouse_send_receive_timeout: float = (
            CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS
        ),
        clickhouse_sync_request_timeout: float = (
            CLICKHOUSE_SYNC_REQUEST_TIMEOUT_SECONDS
        ),
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
        durable_ack_authority_timeout: float = (
            DURABLE_ACK_AUTHORITY_TIMEOUT_SECONDS
        ),
        authority_pool=None,
        boundary_marker_poll_interval: float = (BOUNDARY_MARKER_POLL_INTERVAL_SECONDS),
    ):
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive")
        if dlq_maxlen <= 0:
            raise ValueError("dlq_maxlen must be positive")
        if stream_discovery_interval <= 0:
            raise ValueError("stream_discovery_interval must be positive")
        if boundary_marker_poll_interval <= 0:
            raise ValueError("boundary_marker_poll_interval must be positive")
        for name, value in (
            ("clickhouse_connect_timeout", clickhouse_connect_timeout),
            ("clickhouse_send_receive_timeout", clickhouse_send_receive_timeout),
            ("clickhouse_sync_request_timeout", clickhouse_sync_request_timeout),
            ("shutdown_timeout", shutdown_timeout),
            ("durable_ack_authority_timeout", durable_ack_authority_timeout),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
        configured_clickhouse_url = self._clickhouse_url_with_timeouts(
            clickhouse_url,
            connect_timeout=clickhouse_connect_timeout,
            send_receive_timeout=clickhouse_send_receive_timeout,
            sync_request_timeout=clickhouse_sync_request_timeout,
        )
        self.ch_client = ClickHouseClient.from_url(configured_clickhouse_url)
        # Cancellation and process inspection must never share the native
        # connection that can be blocked inside an INSERT.
        self.ch_control_client = ClickHouseClient.from_url(configured_clickhouse_url)
        self._db_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="clickhouse-writer-db",
        )
        self._control_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="clickhouse-writer-control",
        )
        self._inflight_insert: InFlightInsert | None = None
        self._accepting_inserts = True
        self._stop_task: asyncio.Task[None] | None = None
        self.buffer: list[BufferedEvent] = []
        self._durable_pending_ack: dict[str, list[str]] = {}
        self._boundary_tokens_by_delivery: dict[tuple[str, str], str] = {}
        self._uncertain_redis_finalizations: set[str] = set()
        self._finalized_since_frontier: dict[str, set[str]] = {}
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.dlq_maxlen = dlq_maxlen
        self.pending_claim_idle_ms = pending_claim_idle_ms
        self.pending_claim_interval = pending_claim_interval
        self.stream_discovery_interval = stream_discovery_interval
        self.shutdown_timeout = shutdown_timeout
        self.durable_ack_authority_timeout = durable_ack_authority_timeout
        self.authority_pool = authority_pool
        self.boundary_marker_poll_interval = boundary_marker_poll_interval
        self.running = False
        self._closed = False
        self.last_flush = time.monotonic()
        self._last_pending_claim = 0.0
        self.consumer_name = f"worker-{os.getpid()}"
        self.stats = {
            "consumed": 0,
            "flushed": 0,
            "rejected": 0,
            "dead_lettered": 0,
            "lost_or_deleted_pending": 0,
            "boundary_markers_published": 0,
            "boundary_markers_retried": 0,
            "boundary_markers_quarantined": 0,
            "errors": 0,
        }
        self._flush_retry_count = 0
        self._next_flush_retry_at = 0.0
        self._flush_lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._new_stream_cursor = 0
        self._pending_stream_cursor = 0
        self._known_stream_keys: set[str] = set()
        self._stream_registry_lock = asyncio.Lock()
        self._last_stream_pressure_log: dict[str, float] = {}

    async def start(self, project_ids: list[str] | None = None):
        """Start consuming from all project streams.

        Args:
            project_ids: Explicit list of project IDs to consume. If None,
                         streams are discovered dynamically via SCAN.
        """
        self.running = True
        logger.info("ClickHouseWriter starting, consumer=%s", self.consumer_name)

        # Force one bounded discovery before the consume loop starts. Explicit
        # project configuration is already authoritative and must never trigger
        # a global Redis SCAN.
        initial_stream_keys = self._normalized_stream_keys(
            [self._stream_key_for_project(project_id) for project_id in project_ids]
            if project_ids is not None
            else await self._discover_streams()
        )

        # Reclaim legacy acknowledged history before any group-creation write.
        # Redis rejects XGROUP CREATE while memory is already above maxmemory,
        # but permits XTRIM. Existing groups can therefore recover first.
        await self._reconcile_acknowledged_history(
            initial_stream_keys,
            require_group=False,
        )
        self._replace_stream_registry(initial_stream_keys)

        # Ensure consumer groups exist for known streams.
        await self._ensure_consumer_groups(project_ids)
        stream_keys = await self._get_stream_keys(project_ids)
        await self._reconcile_acknowledged_history(stream_keys)
        await self._log_due_stream_pressure(stream_keys)
        await self._log_redis_memory_pressure()

        # Run consumption, flushing, and bounded observability concurrently.
        try:
            tasks = [
                self._consume_loop(project_ids),
                self._flush_loop(),
                self._monitor_loop(project_ids),
            ]
            if self.authority_pool is not None:
                tasks.append(self._boundary_marker_loop(project_ids))
            if project_ids is None:
                tasks.append(self._stream_discovery_loop())
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Writer task cancelled, starting bounded shutdown")
            await self.stop()
            raise

    async def stop(self, *, flush_buffer: bool = True):
        """Stop writes and prove the native INSERT is gone before returning.

        Normal shutdown gets one bounded final-flush grace period. Maintenance
        inhibitor loss fences new INSERTs immediately. Once the grace period
        expires, the stable query ID is killed synchronously and checked
        against ``system.processes``. A control-plane failure is fail-closed:
        this method keeps retrying, so its caller retains the surviving
        PostgreSQL inhibitor and Redis deliveries remain pending.
        """
        self.running = False
        if not flush_buffer:
            self._accepting_inserts = False
        if self._closed:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_fail_closed(flush_buffer=flush_buffer),
                name="clickhouse-writer-stop",
            )

        deferred_exception: BaseException | None = None
        while not self._stop_task.done():
            try:
                await asyncio.shield(self._stop_task)
            except BaseException as exc:
                # Do not let a second cancellation, KeyboardInterrupt, or
                # SystemExit release PostgreSQL guards before drain proof.
                deferred_exception = deferred_exception or exc

        self._stop_task.result()
        if deferred_exception is not None:
            raise deferred_exception

    async def _stop_fail_closed(self, *, flush_buffer: bool) -> None:
        """Retry cleanup after every pre-proof BaseException."""
        should_flush = flush_buffer
        while not self._closed:
            try:
                await self._stop_once(flush_buffer=should_flush)
            except BaseException as exc:
                self.running = False
                self._accepting_inserts = False
                should_flush = False
                try:
                    logger.critical(
                        "Writer cleanup interrupted before ClickHouse drain proof; "
                        "retaining database inhibitors and retrying: %s",
                        exc,
                    )
                except BaseException:
                    pass

    async def _stop_once(self, *, flush_buffer: bool) -> None:
        async with self._stop_lock:
            if self._closed:
                return
            try:
                if flush_buffer:
                    await asyncio.wait_for(
                        self._flush(),
                        timeout=self.shutdown_timeout,
                    )
            except TimeoutError:
                self.stats["errors"] += 1
                logger.error(
                    "ClickHouse final flush exceeded %.1fs; fencing new INSERTs "
                    "and draining the registered query",
                    self.shutdown_timeout,
                )

            self._accepting_inserts = False
            await self._drain_inflight_insert()
            self._disconnect_clickhouse()
            self._disconnect_clickhouse_control()
            await self.redis_client.aclose()
            self._db_executor.shutdown(wait=False, cancel_futures=True)
            self._control_executor.shutdown(wait=False, cancel_futures=True)
            self._closed = True
            logger.info("ClickHouseWriter stopped. Stats: %s", self.stats)

    @staticmethod
    def _clickhouse_url_with_timeouts(
        clickhouse_url: str,
        *,
        connect_timeout: float,
        send_receive_timeout: float,
        sync_request_timeout: float,
    ) -> str:
        """Return a driver URL with process-owned timeout values."""
        parsed = urlsplit(clickhouse_url)
        timeout_names = {
            "connect_timeout",
            "send_receive_timeout",
            "sync_request_timeout",
        }
        query = [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if name not in timeout_names
        ]
        query.extend(
            [
                ("connect_timeout", str(connect_timeout)),
                ("send_receive_timeout", str(send_receive_timeout)),
                ("sync_request_timeout", str(sync_request_timeout)),
            ]
        )
        return urlunsplit(parsed._replace(query=urlencode(query)))

    def _disconnect_clickhouse(self) -> None:
        """Close the native socket so an in-flight blocking call can unwind."""
        try:
            self.ch_client.disconnect()
        except BaseException as exc:
            logger.warning("ClickHouse disconnect failed during shutdown: %s", exc)

    def _disconnect_clickhouse_control(self) -> None:
        """Close the independent cancellation/inspection connection."""
        try:
            self.ch_control_client.disconnect()
        except BaseException as exc:
            logger.warning(
                "ClickHouse control disconnect failed during shutdown: %s", exc
            )

    async def _discover_streams(self) -> list[str]:
        """Discover all event streams using SCAN with pattern matching.

        Returns a list of stream keys matching the events:raw:* pattern.
        """
        streams: list[str] = []
        cursor = 0
        while True:
            cursor, keys = await self.redis_client.scan(
                cursor=cursor, match=f"{STREAM_PREFIX}*", count=100
            )
            for stream_key in keys:
                try:
                    self._project_id_from_stream(stream_key)
                except ValueError as exc:
                    self.stats["errors"] += 1
                    logger.warning(
                        "Ignoring invalid event stream %r: %s", stream_key, exc
                    )
                    continue
                streams.append(stream_key)
            if cursor == 0:
                break
        return streams

    async def _ensure_consumer_groups(self, project_ids: list[str] | None):
        """Create consumer groups if they don't exist.

        New groups start at the beginning of the stream so events published
        before writer discovery remain available to the group.
        """
        if project_ids is not None:
            stream_keys = [self._stream_key_for_project(pid) for pid in project_ids]
        else:
            stream_keys = sorted(self._known_stream_keys)

        await self._ensure_consumer_groups_for_streams(stream_keys)

    async def _ensure_consumer_groups_for_streams(self, stream_keys: list[str]) -> None:
        """Ensure groups for one already-resolved registry snapshot."""
        for stream_key in stream_keys:
            await self._ensure_consumer_group(stream_key)

    async def _ensure_consumer_group(self, stream_key: str) -> None:
        """Create the required group without an unnecessary DENYOOM write."""
        try:
            groups = await self.redis_client.xinfo_groups(stream_key)
        except redis.ResponseError as exc:
            if "no such key" not in str(exc).lower():
                raise
            groups = []

        existing_group = next(
            (
                group
                for group in groups
                if self._redis_info_value(group, "name") == CONSUMER_GROUP
            ),
            None,
        )
        if existing_group is not None:
            existing_group_frontier = self._redis_info_value(
                existing_group,
                "last-delivered-id",
            )
            existing_group_entries_read = self._redis_info_value(
                existing_group,
                "entries-read",
            )
            logger.debug(
                "Consumer group '%s' already exists on '%s'",
                CONSUMER_GROUP,
                stream_key,
            )
            await self._initialize_pipeline_authority(
                stream_key,
                group_was_created=False,
                observed_group_frontier=existing_group_frontier,
                observed_group_entries_read=existing_group_entries_read,
                safe_genesis=(existing_group_frontier == "0-0"),
            )
            return

        created = False
        try:
            await self.redis_client.xgroup_create(
                name=stream_key,
                groupname=CONSUMER_GROUP,
                id=CONSUMER_GROUP_START_ID,
                mkstream=True,
            )
            logger.info(
                "Created consumer group '%s' on stream '%s'",
                CONSUMER_GROUP,
                stream_key,
            )
            created = True
        except redis.ResponseError as exc:
            # Another writer may create the group after the read above.
            if "BUSYGROUP" not in str(exc):
                raise
        await self._initialize_pipeline_authority(
            stream_key,
            group_was_created=created,
            observed_group_frontier="0-0" if created else None,
            observed_group_entries_read=0 if created else None,
            safe_genesis=created,
        )

    async def _initialize_pipeline_authority(
        self,
        stream_key: str,
        *,
        group_was_created: bool,
        observed_group_frontier: str | None,
        observed_group_entries_read: int | str | None,
        safe_genesis: bool,
    ) -> None:
        """Create completeness authority without blessing legacy ACK history."""
        if self.authority_pool is None:
            return
        project_id = self._project_id_from_stream(stream_key)
        async with self.authority_pool.acquire() as connection:
            existing = await connection.fetchrow(
                """
                SELECT
                    stream_key,
                    contiguous_stream_id,
                    consumer_group_entries_read,
                    status
                FROM event_pipeline_watermarks
                WHERE project_id = $1
                """,
                project_id,
            )
        if existing is not None:
            if self._postgres_value(existing, "stream_key") != stream_key:
                raise RuntimeError("pipeline watermark stream authority is invalid")
            if group_was_created:
                await self._degrade_pipeline_authority(
                    stream_key,
                    "stream_state_unverifiable",
                )
                return
            try:
                observed_parts = self._stream_id_parts(observed_group_frontier)
                persisted_parts = self._stream_id_parts(
                    self._postgres_value(existing, "contiguous_stream_id")
                )
                if isinstance(observed_group_entries_read, bool):
                    raise ValueError
                observed_entries_read = int(observed_group_entries_read)
                if observed_entries_read < 0:
                    raise ValueError
                persisted_entries_read = self._postgres_value(
                    existing,
                    "consumer_group_entries_read",
                )
                if type(persisted_entries_read) is not int:
                    raise ValueError
            except (RuntimeError, TypeError, ValueError):
                await self._degrade_pipeline_authority(
                    stream_key,
                    "stream_state_unverifiable",
                )
                return
            # A healthy authority must restart at exactly the frontier it last
            # committed. A lower group head proves rollback; a higher head is
            # also ambiguous (for example XGROUP SETID, or a crash after Redis
            # finalization but before the PostgreSQL commit). Never bless that
            # cross-store gap implicitly.
            if self._postgres_value(existing, "status") == "healthy" and (
                observed_parts != persisted_parts
                or observed_entries_read != persisted_entries_read
            ):
                await self._degrade_pipeline_authority(
                    stream_key,
                    "stream_state_unverifiable",
                )
            return

        try:
            if isinstance(observed_group_entries_read, bool):
                raise ValueError
            initial_entries_read = int(observed_group_entries_read)
            if initial_entries_read < 0:
                raise ValueError
        except (TypeError, ValueError):
            initial_entries_read = 0
            safe_genesis = False
        if safe_genesis:
            stream_info = await self.redis_client.xinfo_stream(stream_key)
            max_deleted_entry_id = self._redis_info_value(
                stream_info,
                "max-deleted-entry-id",
            )
            if max_deleted_entry_id is None:
                raise RuntimeError(
                    "Redis stream does not expose deleted-history authority"
                )
            safe_genesis = self._stream_id_parts(max_deleted_entry_id) == (0, 0)
            safe_genesis = safe_genesis and initial_entries_read == 0
        status = "healthy" if safe_genesis else "degraded"
        failure_reason = None if safe_genesis else "legacy_state_unverifiable"
        async with self.authority_pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO event_pipeline_watermarks (
                    project_id,
                    stream_key,
                    provenance_start_stream_id,
                    contiguous_stream_id,
                    consumer_group_entries_read,
                    status,
                    failure_reason
                )
                VALUES ($1, $2, '0-0', '0-0', $3, $4, $5)
                ON CONFLICT (project_id) DO NOTHING
                """,
                project_id,
                stream_key,
                initial_entries_read,
                status,
                failure_reason,
            )
            row = await connection.fetchrow(
                """
                SELECT stream_key
                FROM event_pipeline_watermarks
                WHERE project_id = $1
                """,
                project_id,
            )
        if row is None or self._postgres_value(row, "stream_key") != stream_key:
            raise RuntimeError("pipeline watermark stream authority is invalid")

    async def _consume_loop(self, project_ids: list[str] | None):
        """Main consume loop reading from Redis Streams.

        Uses XREADGROUP for reliable delivery with consumer groups.
        First reads any pending (previously delivered but unacknowledged)
        messages, then switches to reading new messages.
        """
        # Phase 1: Claim any pending messages from a previous crash
        await self._process_pending(project_ids)

        # Phase 2: Read new messages continuously
        while self.running:
            try:
                if self._delivery_is_backpressured():
                    await self._flush_after_retry_deadline()
                    continue

                if (
                    time.monotonic() - self._last_pending_claim
                    >= self.pending_claim_interval
                ):
                    await self._process_pending(project_ids)
                    if self._delivery_is_backpressured():
                        continue

                stream_keys = await self._get_stream_keys(project_ids)
                if not stream_keys:
                    # No streams found yet, wait and retry
                    await asyncio.sleep(2.0)
                    continue
                readable_stream_keys = [
                    stream_key
                    for stream_key in stream_keys
                    if not self._delivery_is_backpressured(stream_key)
                ]
                if not readable_stream_keys:
                    # Durable rows for these exact streams are still awaiting
                    # finalization. The flush loop retries them independently;
                    # do not redeliver more work from a blocked tenant.
                    await asyncio.sleep(1.0)
                    continue

                stream_key = self._next_stream_key(
                    readable_stream_keys,
                    pending=False,
                )
                remaining = self._remaining_capacity()
                if remaining <= 0:
                    continue

                # Redis applies COUNT per stream, not across the whole call. Read
                # one stream at a time so buffer_size remains a global bound while
                # rotating fairly across tenants. Keep a complete rotation near
                # one second even when most streams are idle.
                results = await self.redis_client.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=self.consumer_name,
                    streams={stream_key: ">"},
                    count=remaining,
                    block=max(1, 1000 // len(readable_stream_keys)),
                )

                if results:
                    await self._process_messages(results)

            except redis.ConnectionError as e:
                logger.error("Redis connection error: %s, retrying in 5s", e)
                self.stats["errors"] += 1
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Unexpected error in consume loop: %s", e, exc_info=True)
                self.stats["errors"] += 1
                await asyncio.sleep(1.0)

    async def _process_pending(self, project_ids: list[str] | None):
        """Process any pending messages left from a previous crash.

        When a consumer crashes after XREADGROUP but before XACK, messages
        remain owned by its consumer name in the Pending Entries List (PEL).
        XAUTOCLAIM transfers deliveries that have exceeded the idle threshold
        to this consumer so they can be inserted and ACKed.
        """
        logger.info("Checking for pending messages")
        stream_keys = await self._get_stream_keys(project_ids)

        for stream_key in self._rotated_stream_keys(stream_keys, pending=True):
            if self._delivery_is_backpressured(stream_key):
                # A failed PostgreSQL verification/frontier transaction for one
                # tenant must not prevent pending sweeps for every other stream.
                continue
            if self._delivery_is_backpressured():
                if not await self._flush_after_retry_deadline():
                    return

            start_id = "0-0"
            while self.running:
                try:
                    remaining = self._remaining_capacity()
                    if remaining <= 0:
                        return
                    claimed = await self.redis_client.xautoclaim(
                        name=stream_key,
                        groupname=CONSUMER_GROUP,
                        consumername=self.consumer_name,
                        min_idle_time=self.pending_claim_idle_ms,
                        start_id=start_id,
                        count=remaining,
                    )
                    next_start_id, messages, deleted_ids = self._claimed_messages(
                        claimed
                    )
                    if deleted_ids:
                        self.stats["lost_or_deleted_pending"] += len(deleted_ids)
                        await self._degrade_pipeline_authority(
                            stream_key,
                            "lost_pending_entry",
                        )
                        logger.critical(
                            "Redis XAUTOCLAIM reported %d lost or deleted pending "
                            "messages from '%s': %s",
                            len(deleted_ids),
                            stream_key,
                            ", ".join(deleted_ids),
                            extra={
                                "event": "lost_or_deleted_pending",
                                "stream_key": stream_key,
                                "deleted_pending_count": len(deleted_ids),
                            },
                        )
                    if not messages and next_start_id in {"0-0", start_id}:
                        break
                    if messages:
                        buffered = await self._process_messages(
                            [(stream_key, messages)]
                        )
                        if buffered:
                            if not self._flush_retry_is_due():
                                return
                            if not await self._flush():
                                return
                        logger.info(
                            "Processed %d stale pending messages from '%s'",
                            buffered,
                            stream_key,
                        )
                        # One claimed page per stream per sweep prevents a busy
                        # tenant's PEL from starving every later tenant.
                        break
                    if next_start_id == "0-0":
                        break
                    start_id = next_start_id
                except Exception as e:
                    logger.error(
                        "Error processing pending from '%s': %s",
                        stream_key,
                        e,
                    )
                    break
        self._last_pending_claim = time.monotonic()

    async def _monitor_loop(self, project_ids: list[str] | None) -> None:
        """Monitor stream backlog and shared Redis memory off the consume path."""
        while self.running:
            remaining = EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS
            while self.running and remaining > 0:
                interval = min(remaining, 1.0)
                await asyncio.sleep(interval)
                remaining -= interval
            if not self.running:
                return
            try:
                stream_keys = await self._get_stream_keys(project_ids)
                await self._reconcile_acknowledged_history(stream_keys)
                await self._log_due_stream_pressure(stream_keys)
                await self._log_redis_memory_pressure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning("Event stream monitor failed: %s", exc)

    async def _stream_discovery_loop(self) -> None:
        """Refresh the dynamic stream registry away from the consume path."""
        while self.running:
            remaining = self.stream_discovery_interval
            while self.running and remaining > 0:
                interval = min(remaining, 1.0)
                await asyncio.sleep(interval)
                remaining -= interval
            if not self.running:
                return
            try:
                await self._refresh_discovered_stream_registry()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["errors"] += 1
                logger.warning(
                    "Redis event stream discovery failed; retaining %d cached "
                    "streams: %s",
                    len(self._known_stream_keys),
                    exc,
                )

    async def _boundary_marker_loop(
        self,
        project_ids: list[str] | None,
    ) -> None:
        """Materialize deterministic experiment barriers in project streams."""
        while self.running:
            try:
                await self._publish_pending_boundary_markers(project_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["errors"] += 1
                logger.error("Experiment boundary marker publication failed: %s", exc)

            remaining = self.boundary_marker_poll_interval
            while self.running and remaining > 0:
                interval = min(remaining, 1.0)
                await asyncio.sleep(interval)
                remaining -= interval

    async def _publish_pending_boundary_markers(
        self,
        project_ids: list[str] | None,
    ) -> None:
        if self.authority_pool is None:
            return
        async with self.authority_pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH due_markers AS (
                    SELECT
                        project_id,
                        experiment_key,
                        config_version,
                        stream_key,
                        marker_token,
                        marker_publish_attempts,
                        marker_publish_observed_stream_id,
                        marker_publish_next_attempt_at,
                        requested_at,
                        row_number() OVER (
                            PARTITION BY project_id
                            ORDER BY
                                marker_publish_next_attempt_at,
                                requested_at,
                                experiment_key,
                                config_version
                        ) AS project_rank
                    FROM experiment_analysis_boundaries
                    WHERE marker_publish_state = 'pending'
                      AND marker_stream_id IS NULL
                      AND marker_publish_next_attempt_at <= clock_timestamp()
                      AND (
                          $1::text[] IS NULL
                          OR project_id = ANY($1::text[])
                      )
                )
                SELECT
                    project_id,
                    experiment_key,
                    config_version,
                    stream_key,
                    marker_token,
                    marker_publish_attempts,
                    marker_publish_observed_stream_id
                FROM due_markers
                WHERE project_rank = 1
                ORDER BY
                    marker_publish_next_attempt_at,
                    requested_at,
                    project_id,
                    experiment_key,
                    config_version
                LIMIT 100
                """,
                project_ids,
            )

        for row in rows:
            try:
                marker = self._pending_boundary_marker(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["errors"] += 1
                logger.error(
                    "Boundary marker row could not be identified; continuing "
                    "with other tenants: %s",
                    exc,
                )
                continue

            try:
                await self._publish_one_boundary_marker(marker)
            except asyncio.CancelledError:
                raise
            except BoundaryMarkerPublishError as exc:
                self.stats["errors"] += 1
                try:
                    state = await self._record_boundary_marker_failure(marker, exc)
                except asyncio.CancelledError:
                    raise
                except Exception as state_exc:
                    logger.error(
                        "Boundary marker failure state could not be persisted "
                        "for project=%s experiment=%s version=%d; continuing "
                        "with other tenants: %s",
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                        state_exc,
                    )
                    continue
                if state == "published":
                    self.stats["boundary_markers_published"] += 1
                    continue
                if state == "quarantined":
                    self.stats["boundary_markers_quarantined"] += 1
                    logger.error(
                        "Boundary marker terminally quarantined "
                        "project=%s experiment=%s version=%d failure_code=%s",
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                        exc.code,
                    )
                else:
                    self.stats["boundary_markers_retried"] += 1
                    logger.warning(
                        "Boundary marker publication deferred "
                        "project=%s experiment=%s version=%d failure_code=%s",
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                        exc.code,
                    )
                continue
            except Exception as exc:
                self.stats["errors"] += 1
                failure = BoundaryMarkerPublishError(
                    "unexpected_publish_failure",
                    terminal=False,
                )
                try:
                    state = await self._record_boundary_marker_failure(
                        marker,
                        failure,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as state_exc:
                    logger.error(
                        "Unexpected boundary marker failure and retry-state "
                        "persistence both failed for project=%s experiment=%s "
                        "version=%d; continuing with other tenants: %s / %s",
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                        exc,
                        state_exc,
                    )
                    continue
                if state == "published":
                    self.stats["boundary_markers_published"] += 1
                elif state == "quarantined":
                    self.stats["boundary_markers_quarantined"] += 1
                else:
                    self.stats["boundary_markers_retried"] += 1
                logger.exception(
                    "Unexpected boundary marker publication failure isolated "
                    "for project=%s experiment=%s version=%d",
                    marker.project_id,
                    marker.experiment_key,
                    marker.config_version,
                )
                continue

            self.stats["boundary_markers_published"] += 1

    @classmethod
    def _pending_boundary_marker(cls, row) -> PendingBoundaryMarker:
        project_id = cls._postgres_value(row, "project_id")
        experiment_key = cls._postgres_value(row, "experiment_key")
        config_version = cls._postgres_value(row, "config_version")
        publish_attempts = cls._postgres_value(
            row,
            "marker_publish_attempts",
        )
        observed_stream_id = cls._postgres_value(
            row,
            "marker_publish_observed_stream_id",
        )
        if (
            not isinstance(project_id, str)
            or PROJECT_ID_PATTERN.fullmatch(project_id) is None
        ):
            raise RuntimeError("boundary marker project identity is invalid")
        if not isinstance(experiment_key, str) or not experiment_key:
            raise RuntimeError("boundary marker experiment identity is invalid")
        if type(config_version) is not int or config_version <= 0:
            raise RuntimeError("boundary marker config version is invalid")
        if (
            type(publish_attempts) is not int
            or publish_attempts < 0
            or publish_attempts >= BOUNDARY_MARKER_MAX_PUBLISH_ATTEMPTS
        ):
            raise RuntimeError("boundary marker publish attempts are invalid")
        if observed_stream_id is not None:
            try:
                observed_parts = cls._stream_id_parts(observed_stream_id)
            except ValueError as exc:
                raise RuntimeError(
                    "boundary marker observed stream ID is invalid"
                ) from exc
            if observed_parts[0] == 0:
                raise RuntimeError(
                    "boundary marker observed stream ID is invalid"
                )
        return PendingBoundaryMarker(
            project_id=project_id,
            experiment_key=experiment_key,
            config_version=config_version,
            stream_key=cls._postgres_value(row, "stream_key"),
            marker_token=cls._postgres_value(row, "marker_token"),
            publish_attempts=publish_attempts,
            observed_stream_id=observed_stream_id,
        )

    async def _publish_one_boundary_marker(
        self,
        marker: PendingBoundaryMarker,
    ) -> None:
        expected_stream_key = self._stream_key_for_project(marker.project_id)
        if marker.stream_key != expected_stream_key:
            raise BoundaryMarkerPublishError(
                "invalid_stream_authority",
                terminal=True,
            )
        if (
            not isinstance(marker.marker_token, str)
            or BOUNDARY_TOKEN_PATTERN.fullmatch(marker.marker_token) is None
        ):
            raise BoundaryMarkerPublishError(
                "invalid_marker_token",
                terminal=True,
            )

        try:
            marker_stream_id = await self.redis_client.eval(
                BOUNDARY_MARKER_LUA,
                2,
                marker.stream_key,
                f"{BOUNDARY_MARKER_DEDUP_PREFIX}{marker.marker_token}",
                EVENT_STREAM_MAX_ENTRIES,
                BOUNDARY_MARKER_KIND,
                marker.marker_token,
                marker.observed_stream_id or "",
            )
        except asyncio.CancelledError:
            raise
        except redis.ResponseError as exc:
            message = str(exc)
            if "BOUNDARY_MARKER_DEDUP_INVALID" in message:
                raise BoundaryMarkerPublishError(
                    "invalid_boundary_marker_dedup",
                    terminal=True,
                ) from exc
            code = (
                "event_stream_capacity"
                if "EVENT_STREAM_CAPACITY_REACHED" in message
                else "redis_publish_failed"
            )
            raise BoundaryMarkerPublishError(
                code,
                terminal=False,
            ) from exc
        except Exception as exc:
            raise BoundaryMarkerPublishError(
                "redis_publish_failed",
                terminal=False,
            ) from exc

        if isinstance(marker_stream_id, (list, tuple)):
            invalid_observed_stream_id = None
            reply_code = marker_stream_id[0] if marker_stream_id else None
            if isinstance(reply_code, bytes):
                try:
                    reply_code = reply_code.decode("utf-8")
                except UnicodeDecodeError:
                    reply_code = None
            if (
                len(marker_stream_id) == 2
                and reply_code == BOUNDARY_MARKER_DEDUP_INVALID_REPLY
            ):
                candidate = marker_stream_id[1]
                if isinstance(candidate, bytes):
                    try:
                        candidate = candidate.decode("utf-8")
                    except UnicodeDecodeError:
                        candidate = None
                try:
                    candidate_parts = self._stream_id_parts(candidate)
                except ValueError:
                    candidate_parts = None
                if candidate_parts is not None and candidate_parts[0] > 0:
                    invalid_observed_stream_id = candidate
                if marker.observed_stream_id is not None:
                    # The first validated observation is monotone authority.
                    # A later dedup mutation must quarantine, never replace it.
                    invalid_observed_stream_id = marker.observed_stream_id
                raise BoundaryMarkerPublishError(
                    "invalid_boundary_marker_dedup",
                    terminal=True,
                    observed_stream_id=invalid_observed_stream_id,
                )
            raise BoundaryMarkerPublishError(
                "invalid_redis_marker_id",
                terminal=True,
            )

        if isinstance(marker_stream_id, bytes):
            try:
                marker_stream_id = marker_stream_id.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BoundaryMarkerPublishError(
                    "invalid_redis_marker_id",
                    terminal=True,
                ) from exc
        try:
            marker_parts = self._stream_id_parts(marker_stream_id)
        except ValueError as exc:
            raise BoundaryMarkerPublishError(
                "invalid_redis_marker_id",
                terminal=True,
            ) from exc
        if marker_parts[0] == 0:
            raise BoundaryMarkerPublishError(
                "invalid_redis_marker_id",
                terminal=True,
            )
        if (
            marker.observed_stream_id is not None
            and marker.observed_stream_id != marker_stream_id
        ):
            raise BoundaryMarkerPublishError(
                "invalid_boundary_marker_dedup",
                terminal=True,
            )

        try:
            async with self.authority_pool.acquire() as connection:
                async with connection.transaction():
                    stored = await connection.fetchrow(
                        """
                        UPDATE experiment_analysis_boundaries
                        SET marker_stream_id = $6,
                            marked_at = clock_timestamp(),
                            marker_publish_state = 'published',
                            marker_publish_next_attempt_at = NULL,
                            marker_publish_quarantined_at = NULL,
                            marker_publish_observed_stream_id = $6
                        WHERE project_id = $1
                          AND experiment_key = $2
                          AND config_version = $3
                          AND stream_key = $4
                          AND marker_token = $5
                          AND marker_publish_state = 'pending'
                          AND marker_publish_attempts = $7
                          AND marker_stream_id IS NULL
                        RETURNING
                            marker_publish_state,
                            marker_stream_id,
                            marker_publish_observed_stream_id
                        """,
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                        marker.stream_key,
                        marker.marker_token,
                        marker_stream_id,
                        marker.publish_attempts,
                    )
                    if stored is None:
                        stored = await connection.fetchrow(
                            """
                            SELECT
                                marker_publish_state,
                                marker_stream_id,
                                marker_publish_observed_stream_id
                            FROM experiment_analysis_boundaries
                            WHERE project_id = $1
                              AND experiment_key = $2
                              AND config_version = $3
                            FOR SHARE
                            """,
                            marker.project_id,
                            marker.experiment_key,
                            marker.config_version,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise BoundaryMarkerPublishError(
                "boundary_authority_update_failed",
                terminal=False,
                observed_stream_id=marker_stream_id,
            ) from exc

        if stored is None:
            raise BoundaryMarkerPublishError(
                "boundary_authority_update_invalid",
                terminal=True,
                observed_stream_id=marker_stream_id,
            )
        if (
            self._postgres_value(stored, "marker_publish_state") != "published"
            or self._postgres_value(stored, "marker_stream_id")
            != marker_stream_id
            or self._postgres_value(
                stored,
                "marker_publish_observed_stream_id",
            )
            != marker_stream_id
        ):
            raise BoundaryMarkerPublishError(
                "boundary_authority_update_invalid",
                terminal=True,
                observed_stream_id=marker_stream_id,
            )

    @staticmethod
    def _boundary_marker_retry_delay(new_attempt: int) -> int:
        if (
            type(new_attempt) is not int
            or new_attempt <= 0
            or new_attempt >= BOUNDARY_MARKER_MAX_PUBLISH_ATTEMPTS
        ):
            raise ValueError("boundary marker retry attempt is out of range")
        return min(
            BOUNDARY_MARKER_RETRY_BASE_SECONDS * (2 ** (new_attempt - 1)),
            BOUNDARY_MARKER_RETRY_MAX_SECONDS,
        )

    @staticmethod
    async def _update_boundary_marker_failure(
        connection,
        marker: PendingBoundaryMarker,
        failure: BoundaryMarkerPublishError,
        *,
        quarantined: bool,
        retry_delay: int,
        observed_stream_id: str | None,
    ):
        return await connection.fetchrow(
            """
            WITH failure_clock AS (
                SELECT clock_timestamp() AS failed_at
            )
            UPDATE experiment_analysis_boundaries AS boundary
            SET marker_publish_attempts =
                    boundary.marker_publish_attempts + 1,
                marker_publish_state = CASE
                    WHEN $6::boolean
                        OR boundary.marker_publish_attempts + 1 >= $8
                    THEN 'quarantined'
                    ELSE 'pending'
                END,
                marker_publish_next_attempt_at = CASE
                    WHEN $6::boolean
                        OR boundary.marker_publish_attempts + 1 >= $8
                    THEN NULL
                    ELSE failure_clock.failed_at
                        + $7::integer * INTERVAL '1 second'
                END,
                marker_publish_failure_code = $5,
                marker_publish_last_error_at = failure_clock.failed_at,
                marker_publish_observed_stream_id = COALESCE(
                    boundary.marker_publish_observed_stream_id,
                    $9::text
                ),
                marker_publish_quarantined_at = CASE
                    WHEN $6::boolean
                        OR boundary.marker_publish_attempts + 1 >= $8
                    THEN failure_clock.failed_at
                    ELSE NULL
                END
            FROM failure_clock
            WHERE boundary.project_id = $1
              AND boundary.experiment_key = $2
              AND boundary.config_version = $3
              AND boundary.marker_publish_state = 'pending'
              AND boundary.marker_publish_attempts = $4
              AND boundary.marker_stream_id IS NULL
              AND (
                  boundary.marker_publish_observed_stream_id IS NULL
                  OR $9::text IS NULL
                  OR boundary.marker_publish_observed_stream_id = $9
              )
            RETURNING
                boundary.marker_publish_state,
                boundary.marker_publish_attempts,
                boundary.marker_publish_observed_stream_id
            """,
            marker.project_id,
            marker.experiment_key,
            marker.config_version,
            marker.publish_attempts,
            failure.code,
            quarantined,
            retry_delay,
            BOUNDARY_MARKER_MAX_PUBLISH_ATTEMPTS,
            observed_stream_id,
        )

    async def _record_boundary_marker_failure(
        self,
        marker: PendingBoundaryMarker,
        failure: BoundaryMarkerPublishError,
    ) -> str:
        new_attempt = marker.publish_attempts + 1
        observed_stream_id = (
            failure.observed_stream_id
            if failure.observed_stream_id is not None
            else marker.observed_stream_id
        )
        if (
            marker.observed_stream_id is not None
            and observed_stream_id != marker.observed_stream_id
        ):
            raise RuntimeError("boundary marker observed identity changed")
        quarantined = (
            failure.terminal
            or new_attempt >= BOUNDARY_MARKER_MAX_PUBLISH_ATTEMPTS
        )
        retry_delay = (
            0
            if quarantined
            else self._boundary_marker_retry_delay(new_attempt)
        )
        async with self.authority_pool.acquire() as connection:
            async with connection.transaction():
                persisted_observed_stream_id = observed_stream_id
                try:
                    async with connection.transaction():
                        stored = await self._update_boundary_marker_failure(
                            connection,
                            marker,
                            failure,
                            quarantined=quarantined,
                            retry_delay=retry_delay,
                            observed_stream_id=observed_stream_id,
                        )
                except asyncpg.UniqueViolationError as exc:
                    collision_can_be_discarded = (
                        quarantined
                        and failure.code == "invalid_boundary_marker_dedup"
                        and marker.observed_stream_id is None
                        and observed_stream_id is not None
                        and exc.constraint_name
                        == BOUNDARY_MARKER_OBSERVED_IDENTITY_CONSTRAINT
                    )
                    if not collision_can_be_discarded:
                        raise
                    # A poisoned dedup key can point at an entry already owned
                    # by another boundary in this project. The savepoint rolls
                    # back only the failed identity claim; terminal quarantine
                    # then advances without stealing that authority. Genuine
                    # post-XADD observations are never discarded.
                    persisted_observed_stream_id = None
                    async with connection.transaction():
                        stored = await self._update_boundary_marker_failure(
                            connection,
                            marker,
                            failure,
                            quarantined=quarantined,
                            retry_delay=retry_delay,
                            observed_stream_id=None,
                        )
                    logger.error(
                        "Boundary marker poison ID is already owned; "
                        "quarantined without observed authority "
                        "project=%s experiment=%s version=%d",
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                    )
                if stored is None:
                    stored = await connection.fetchrow(
                        """
                        SELECT
                            marker_publish_state,
                            marker_publish_attempts,
                            marker_publish_observed_stream_id
                        FROM experiment_analysis_boundaries
                        WHERE project_id = $1
                          AND experiment_key = $2
                          AND config_version = $3
                        FOR SHARE
                        """,
                        marker.project_id,
                        marker.experiment_key,
                        marker.config_version,
                    )
        if stored is None:
            raise RuntimeError("boundary marker retry authority disappeared")
        state = self._postgres_value(stored, "marker_publish_state")
        attempts = self._postgres_value(stored, "marker_publish_attempts")
        stored_observed_stream_id = self._postgres_value(
            stored,
            "marker_publish_observed_stream_id",
        )
        if state == "published":
            return state
        expected_state = "quarantined" if quarantined else "pending"
        if (
            state != expected_state
            or attempts != new_attempt
            or stored_observed_stream_id != persisted_observed_stream_id
        ):
            raise RuntimeError("boundary marker retry authority changed concurrently")
        return state

    async def _refresh_discovered_stream_registry(self) -> list[str]:
        """Atomically replace the registry after a complete valid refresh.

        Discovery, legacy-history reconciliation, and consumer-group creation
        must all succeed before readers can observe the new snapshot. Any
        failure therefore leaves the last usable snapshot intact.
        """
        async with self._stream_registry_lock:
            stream_keys = self._normalized_stream_keys(await self._discover_streams())
            await self._reconcile_acknowledged_history(
                stream_keys,
                require_group=False,
            )
            await self._ensure_consumer_groups_for_streams(stream_keys)
            self._replace_stream_registry(stream_keys)
            return sorted(self._known_stream_keys)

    @staticmethod
    def _claimed_messages(
        claimed,
    ) -> tuple[str, list[tuple[str, dict]], list[str]]:
        """Normalize Redis 6.2/7 XAUTOCLAIM response variants."""
        if not claimed or len(claimed) < 2:
            return "0-0", [], []
        deleted_ids = (
            []
            if len(claimed) < 3
            else [
                item.decode() if isinstance(item, bytes) else str(item)
                for item in claimed[2] or []
            ]
        )
        return str(claimed[0]), list(claimed[1]), deleted_ids

    def _rotated_stream_keys(
        self, stream_keys: list[str], *, pending: bool
    ) -> list[str]:
        """Return stable tenant order with a different first stream each sweep."""
        ordered = sorted(set(stream_keys))
        if not ordered:
            return []
        cursor_name = "_pending_stream_cursor" if pending else "_new_stream_cursor"
        start = getattr(self, cursor_name) % len(ordered)
        setattr(self, cursor_name, (start + 1) % len(ordered))
        return ordered[start:] + ordered[:start]

    def _next_stream_key(self, stream_keys: list[str], *, pending: bool) -> str:
        return self._rotated_stream_keys(stream_keys, pending=pending)[0]

    def _remaining_capacity(self) -> int:
        return max(self.buffer_size - len(self.buffer), 0)

    async def _get_stream_keys(self, project_ids: list[str] | None) -> list[str]:
        """Resolve the list of stream keys to read from.

        Explicit project IDs are authoritative. Dynamic consumers read the
        last complete background-discovery snapshot, so hot consumption and
        pending sweeps never issue a global Redis SCAN.
        """
        if project_ids is not None:
            return self._normalized_stream_keys(
                [self._stream_key_for_project(pid) for pid in project_ids]
            )
        return sorted(self._known_stream_keys)

    @staticmethod
    def _normalized_stream_keys(stream_keys: list[str]) -> list[str]:
        """Return one stable entry per stream for fair, duplicate-free reads."""
        return sorted(set(stream_keys))

    def _replace_stream_registry(self, stream_keys: list[str]) -> None:
        """Publish one validated registry snapshot and trim per-stream state."""
        self._known_stream_keys = set(stream_keys)
        self._last_stream_pressure_log = {
            stream_key: logged_at
            for stream_key, logged_at in self._last_stream_pressure_log.items()
            if stream_key in self._known_stream_keys
        }

    async def _log_due_stream_pressure(self, stream_keys) -> None:
        """Rate-limit exact outstanding-entry and consumer pressure logs."""
        now = time.monotonic()
        for stream_key in sorted(set(stream_keys)):
            last_logged = self._last_stream_pressure_log.get(stream_key)
            if (
                last_logged is not None
                and now - last_logged < EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS
            ):
                continue

            # Mark the attempt before I/O so an unavailable Redis instance does
            # not turn observability into a tight retry loop.
            self._last_stream_pressure_log[stream_key] = now
            try:
                stream_length, groups = await asyncio.gather(
                    self.redis_client.xlen(stream_key),
                    self.redis_client.xinfo_groups(stream_key),
                )
                group = next(
                    item
                    for item in groups
                    if self._redis_info_value(item, "name") == CONSUMER_GROUP
                )
                pending = self._redis_info_value(group, "pending")
                lag = self._redis_info_value(group, "lag")
            except Exception as exc:
                logger.warning(
                    "Could not read Redis stream pressure for '%s': %s",
                    stream_key,
                    exc,
                )
                continue

            lag_unknown = lag is None
            log_level = (
                logging.WARNING
                if stream_length >= EVENT_STREAM_ALERT_ENTRIES
                else logging.INFO
            )
            log_context = {
                "event": "event_stream_pressure",
                "stream_key": stream_key,
                "outstanding_entries": stream_length,
                "pending": pending,
                "lag": lag,
                "lag_unknown": lag_unknown,
                "alert_entries": EVENT_STREAM_ALERT_ENTRIES,
                "max_entries": EVENT_STREAM_MAX_ENTRIES,
            }
            logger.log(
                log_level,
                "event_stream_pressure stream=%s outstanding_entries=%d "
                "pending=%s lag=%s lag_unknown=%s alert_entries=%d max_entries=%d",
                stream_key,
                stream_length,
                pending,
                lag,
                str(lag_unknown).lower(),
                EVENT_STREAM_ALERT_ENTRIES,
                EVENT_STREAM_MAX_ENTRIES,
                extra=log_context,
            )

    async def _log_redis_memory_pressure(self) -> None:
        """Emit aggregate Redis memory pressure for the shared dependency."""
        try:
            memory = await self.redis_client.info("memory")
            used_memory = int(self._redis_info_value(memory, "used_memory") or 0)
            max_memory = int(self._redis_info_value(memory, "maxmemory") or 0)
        except Exception as exc:
            logger.warning("Could not read Redis memory pressure: %s", exc)
            return

        utilization = used_memory / max_memory if max_memory > 0 else None
        pressured = utilization is not None and utilization >= REDIS_MEMORY_ALERT_RATIO
        log_context = {
            "event": "redis_memory_pressure",
            "used_memory_bytes": used_memory,
            "max_memory_bytes": max_memory,
            "utilization": utilization,
            "alert_ratio": REDIS_MEMORY_ALERT_RATIO,
            "maxmemory_configured": max_memory > 0,
        }
        logger.log(
            logging.WARNING if pressured else logging.INFO,
            "redis_memory_pressure used_memory_bytes=%d max_memory_bytes=%d "
            "utilization=%s alert_ratio=%.2f maxmemory_configured=%s",
            used_memory,
            max_memory,
            f"{utilization:.4f}" if utilization is not None else "unbounded",
            REDIS_MEMORY_ALERT_RATIO,
            str(max_memory > 0).lower(),
            extra=log_context,
        )

    async def _reconcile_acknowledged_history(
        self,
        stream_keys: list[str],
        *,
        require_group: bool = True,
    ) -> None:
        """Remove pre-upgrade history proven durable by the sole group.

        Legacy writers XACKed durable entries but retained them in the stream.
        Entries below the earliest pending ID are therefore acknowledged; when
        no entries are pending, every ID through ``last-delivered-id`` is safe.
        Unread and pending entries are never crossed by the exact MINID trim.
        """
        for stream_key in stream_keys:
            try:
                groups = await self.redis_client.xinfo_groups(stream_key)
            except redis.ResponseError as exc:
                if not require_group and "no such key" in str(exc).lower():
                    continue
                raise
            group = next(
                (
                    item
                    for item in groups
                    if self._redis_info_value(item, "name") == CONSUMER_GROUP
                ),
                None,
            )
            if group is None:
                if not require_group:
                    continue
                raise RuntimeError(
                    f"Required consumer group {CONSUMER_GROUP!r} is missing "
                    f"from {stream_key!r}"
                )

            last_delivered = self._redis_info_value(group, "last-delivered-id")
            if not last_delivered or last_delivered == "0-0":
                continue

            pending = int(self._redis_info_value(group, "pending") or 0)
            if pending > 0:
                summary = await self.redis_client.xpending(
                    stream_key,
                    CONSUMER_GROUP,
                )
                minimum_pending = self._redis_info_value(summary, "min")
                if not minimum_pending:
                    logger.warning(
                        "Skipping acknowledged-history reconciliation for %s: "
                        "pending summary has no minimum ID",
                        stream_key,
                    )
                    continue
                trim_before = str(minimum_pending)
            else:
                trim_before = self._next_stream_id(str(last_delivered))

            removed = await self.redis_client.xtrim(
                stream_key,
                minid=trim_before,
                approximate=False,
            )
            if removed:
                logger.info(
                    "event_stream_history_reconciled stream=%s removed_entries=%d "
                    "trim_before=%s pending=%d",
                    stream_key,
                    removed,
                    trim_before,
                    pending,
                    extra={
                        "event": "event_stream_history_reconciled",
                        "stream_key": stream_key,
                        "removed_entries": removed,
                        "trim_before": trim_before,
                        "pending": pending,
                    },
                )

    @staticmethod
    def _next_stream_id(stream_id: str) -> str:
        """Return the exclusive MINID boundary immediately after one ID."""
        try:
            milliseconds_text, sequence_text = stream_id.split("-", 1)
            milliseconds = int(milliseconds_text)
            sequence = int(sequence_text)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Redis stream ID: {stream_id!r}") from exc
        if milliseconds < 0 or sequence < 0:
            raise ValueError(f"Invalid Redis stream ID: {stream_id!r}")
        if sequence >= 2**64 - 1:
            return f"{milliseconds + 1}-0"
        return f"{milliseconds}-{sequence + 1}"

    @staticmethod
    def _stream_id_parts(stream_id: str) -> tuple[int, int]:
        if not isinstance(stream_id, str):
            raise ValueError(f"Invalid Redis stream ID: {stream_id!r}")
        try:
            milliseconds_text, sequence_text = stream_id.split("-", 1)
            if (
                str(int(milliseconds_text)) != milliseconds_text
                or str(int(sequence_text)) != sequence_text
            ):
                raise ValueError
            milliseconds = int(milliseconds_text)
            sequence = int(sequence_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Redis stream ID: {stream_id!r}") from exc
        if (
            milliseconds < 0
            or sequence < 0
            or milliseconds >= 2**64
            or sequence >= 2**64
        ):
            raise ValueError(f"Invalid Redis stream ID: {stream_id!r}")
        return milliseconds, sequence

    @staticmethod
    def _redis_info_value(info: dict, field: str):
        """Read redis-py XINFO mappings with decoded or byte keys/values."""
        value = info.get(field, info.get(field.encode()))
        if isinstance(value, bytes):
            return value.decode()
        return value

    @staticmethod
    def _postgres_value(row, field: str):
        try:
            return row[field]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"PostgreSQL authority omitted {field}") from exc

    async def _process_messages(
        self, results: list[tuple[str, list[tuple[str, dict]]]]
    ) -> int:
        """Parse messages from XREADGROUP results and add to buffer.

        Each result is a tuple of (stream_key, [(message_id, fields), ...]).
        Redis deliveries remain pending until their parsed rows have been
        inserted durably into ClickHouse.
        """
        buffered_count = 0
        for stream_key, messages in results:
            try:
                project_id = self._project_id_from_stream(stream_key)
            except ValueError as exc:
                self.stats["errors"] += len(messages)
                logger.error("Rejecting invalid event stream %r: %s", stream_key, exc)
                continue
            for message_id, data in messages:
                try:
                    boundary_token = self._parse_boundary_marker(data)
                    parsed = (
                        None
                        if boundary_token is not None
                        else self._parse_event(
                            data,
                            project_id,
                            source_stream=stream_key,
                            source_stream_id=message_id,
                        )
                    )
                except (
                    json.JSONDecodeError,
                    KeyError,
                    OverflowError,
                    TypeError,
                    UnicodeError,
                    ValueError,
                ) as exc:
                    logger.warning(
                        "Rejecting malformed message %s on %s: %s",
                        message_id,
                        stream_key,
                        exc,
                    )
                    self.stats["errors"] += 1
                    await self._dead_letter_delivery(
                        stream_key,
                        message_id,
                        project_id,
                        reason_code="invalid_event_schema",
                        error=exc,
                    )
                    continue

                if self._remaining_capacity() <= 0:
                    if not self._flush_retry_is_due() or not await self._flush():
                        # XREADGROUP/XAUTOCLAIM has already put this and every
                        # later delivery in the PEL. Leaving them unacknowledged
                        # is safe; a later pending sweep will reclaim them.
                        logger.warning(
                            "Global event buffer is full; leaving message %s "
                            "and later deliveries pending",
                            message_id,
                        )
                        return buffered_count

                self.buffer.append(
                    BufferedEvent(
                        stream_key=stream_key,
                        message_id=message_id,
                        row=parsed,
                        boundary_token=boundary_token,
                    )
                )
                self.stats["consumed"] += 1
                buffered_count += 1

        # Flush if buffer is full
        if len(self.buffer) >= self.buffer_size and self._flush_retry_is_due():
            await self._flush()
        return buffered_count

    @staticmethod
    def _parse_boundary_marker(data: dict) -> str | None:
        """Recognize only the writer-owned strict settlement-marker contract."""
        if not isinstance(data, dict):
            raise TypeError("Redis event fields must be an object")
        if "message_kind" not in data:
            return None
        if set(data) != BOUNDARY_MARKER_FIELDS:
            raise ValueError("analysis boundary marker fields are not canonical")
        if data.get("message_kind") != BOUNDARY_MARKER_KIND:
            raise ValueError("unknown Redis stream message kind")
        boundary_token = data.get("boundary_token")
        if (
            not isinstance(boundary_token, str)
            or BOUNDARY_TOKEN_PATTERN.fullmatch(boundary_token) is None
        ):
            raise ValueError("analysis boundary token is invalid")
        return boundary_token

    @staticmethod
    def _project_id_from_stream(stream_key: str) -> str:
        if not isinstance(stream_key, str) or not stream_key.startswith(STREAM_PREFIX):
            raise ValueError("stream key must use events:raw:{project_id}")
        project_id = stream_key.removeprefix(STREAM_PREFIX)
        if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
            raise ValueError("stream project ID is not canonical")
        return project_id

    @classmethod
    def _stream_key_for_project(cls, project_id: str) -> str:
        if (
            not isinstance(project_id, str)
            or PROJECT_ID_PATTERN.fullmatch(project_id) is None
        ):
            raise ValueError(f"invalid project ID: {project_id!r}")
        return f"{STREAM_PREFIX}{project_id}"

    @classmethod
    def _dlq_stream_for_project(cls, project_id: str) -> str:
        cls._stream_key_for_project(project_id)
        return f"{DLQ_STREAM_PREFIX}{project_id}"

    async def _dead_letter_delivery(
        self,
        stream_key: str,
        message_id: str,
        project_id: str,
        *,
        reason_code: str,
        error: Exception,
    ) -> bool:
        """Persist safe reject metadata before making the source ACK-eligible."""
        self.stats["rejected"] += 1
        fields = {
            "source_stream": stream_key,
            "source_message_id": message_id,
            "reason_code": reason_code,
            "error_type": type(error).__name__,
            "rejected_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self.redis_client.xadd(
                self._dlq_stream_for_project(project_id),
                fields,
                maxlen=self.dlq_maxlen,
                approximate=False,
            )
        except Exception as exc:
            # No ACK is queued: the original delivery remains in the PEL and a
            # later XAUTOCLAIM sweep can retry DLQ persistence.
            self.stats["errors"] += 1
            logger.error(
                "Could not persist reject metadata for %s on %s: %s",
                message_id,
                stream_key,
                exc,
            )
            return False

        try:
            await self._degrade_pipeline_authority(
                stream_key,
                "dead_lettered_event",
            )
        except Exception as exc:
            # The DLQ copy is durable but the source must remain pending until
            # PostgreSQL records that completeness can no longer be proven.
            self.stats["errors"] += 1
            logger.error(
                "Could not degrade pipeline authority for rejected %s on %s: %s",
                message_id,
                stream_key,
                exc,
            )
            return False

        self.stats["dead_lettered"] += 1
        self._queue_durable_ack(
            [BufferedEvent(stream_key=stream_key, message_id=message_id, row={})]
        )
        return True

    async def _flush_loop(self):
        """Periodic flush based on time interval.

        Ensures events are written to ClickHouse even when the buffer
        hasn't reached buffer_size, keeping latency bounded.
        """
        while self.running:
            await asyncio.sleep(1.0)
            if not self._flush_retry_is_due():
                if self._durable_pending_ack:
                    async with self._flush_lock:
                        await self._ack_durable_messages()
                continue
            elapsed = time.monotonic() - self.last_flush
            if self._durable_pending_ack or (
                self.buffer
                and (self._flush_retry_count > 0 or elapsed >= self.flush_interval)
            ):
                await self._flush()

    def _delivery_is_backpressured(self, stream_key: str | None = None) -> bool:
        if stream_key is not None:
            return stream_key in self._durable_pending_ack
        return len(self.buffer) >= self.buffer_size

    def _flush_retry_delay(self) -> float:
        exponent = min(max(self._flush_retry_count - 1, 0), 5)
        return min(
            FLUSH_RETRY_BASE_SECONDS * (2**exponent),
            FLUSH_RETRY_MAX_SECONDS,
        )

    def _flush_retry_is_due(self) -> bool:
        return time.monotonic() >= self._next_flush_retry_at

    def _record_flush_failure(self) -> float:
        self._flush_retry_count += 1
        delay = self._flush_retry_delay()
        self._next_flush_retry_at = time.monotonic() + delay
        return delay

    def _reset_flush_retry(self) -> None:
        self._flush_retry_count = 0
        self._next_flush_retry_at = 0.0

    async def _flush_after_retry_deadline(self) -> bool:
        wait_seconds = max(self._next_flush_retry_at - time.monotonic(), 0.0)
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
        return await self._flush()

    def _queue_durable_ack(self, events: list[BufferedEvent]) -> None:
        for event in events:
            message_ids = self._durable_pending_ack.setdefault(event.stream_key, [])
            if event.message_id not in message_ids:
                message_ids.append(event.message_id)
            if event.boundary_token is not None:
                self._boundary_tokens_by_delivery[
                    (event.stream_key, event.message_id)
                ] = event.boundary_token

    async def _verify_boundary_deliveries(
        self,
        stream_key: str,
        message_ids: list[str],
    ) -> None:
        expected = {
            message_id: self._boundary_tokens_by_delivery[(stream_key, message_id)]
            for message_id in message_ids
            if (stream_key, message_id) in self._boundary_tokens_by_delivery
        }
        if not expected:
            return
        if self.authority_pool is None:
            raise RuntimeError("boundary delivery has no PostgreSQL authority")
        project_id = self._project_id_from_stream(stream_key)
        async with self.authority_pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    marker_token,
                    marker_stream_id,
                    marker_publish_state,
                    marker_publish_observed_stream_id
                FROM experiment_analysis_boundaries
                WHERE project_id = $1
                  AND stream_key = $2
                  AND (
                      marker_token = ANY($3::text[])
                      OR marker_publish_observed_stream_id =
                          ANY($4::text[])
                  )
                """,
                project_id,
                stream_key,
                sorted(set(expected.values())),
                list(expected),
            )
        observed = {}
        terminal_quarantine = False
        for row in rows:
            token = self._postgres_value(row, "marker_token")
            state = self._postgres_value(row, "marker_publish_state")
            if state == "published":
                delivery_id = self._postgres_value(row, "marker_stream_id")
                if expected.get(delivery_id) == token:
                    observed[delivery_id] = token
            elif state == "quarantined":
                delivery_id = self._postgres_value(
                    row,
                    "marker_publish_observed_stream_id",
                )
                if delivery_id in expected:
                    # The exact poisoned/orphaned stream ID is terminally
                    # non-authoritative. Its fields need not match the boundary
                    # token because finalization permanently degrades this
                    # project's completeness frontier before ACK.
                    observed[delivery_id] = expected[delivery_id]
                    terminal_quarantine = True
            else:
                continue
        if observed != expected:
            raise RuntimeError("boundary delivery lacks matching PostgreSQL authority")
        if terminal_quarantine:
            await self._degrade_pipeline_authority(
                stream_key,
                "stream_state_unverifiable",
            )
            logger.critical(
                "Finalizing quarantined boundary marker only after permanently "
                "degrading pipeline authority for %s",
                stream_key,
            )

    async def _degrade_pipeline_authority(
        self,
        stream_key: str,
        failure_reason: str,
    ) -> None:
        """Permanently prevent completeness claims after a proven stream gap."""
        if self.authority_pool is None:
            return
        if failure_reason not in {
            "legacy_state_unverifiable",
            "dead_lettered_event",
            "lost_pending_entry",
            "stream_state_unverifiable",
        }:
            raise ValueError("invalid pipeline degradation reason")
        project_id = self._project_id_from_stream(stream_key)
        async with self.authority_pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO event_pipeline_watermarks (
                    project_id,
                    stream_key,
                    provenance_start_stream_id,
                    contiguous_stream_id,
                    consumer_group_entries_read,
                    status,
                    failure_reason
                )
                VALUES ($1, $2, '0-0', '0-0', 0, 'degraded', $3)
                ON CONFLICT (project_id) DO UPDATE
                SET status = 'degraded',
                    failure_reason = CASE
                        WHEN event_pipeline_watermarks.status = 'degraded'
                        THEN event_pipeline_watermarks.failure_reason
                        ELSE EXCLUDED.failure_reason
                    END,
                    updated_at = now()
                WHERE event_pipeline_watermarks.stream_key = EXCLUDED.stream_key
                """,
                project_id,
                stream_key,
                failure_reason,
            )
            row = await connection.fetchrow(
                """
                SELECT stream_key, status
                FROM event_pipeline_watermarks
                WHERE project_id = $1
                """,
                project_id,
            )
        if (
            row is None
            or self._postgres_value(row, "stream_key") != stream_key
            or self._postgres_value(row, "status") != "degraded"
        ):
            raise RuntimeError("pipeline authority could not be degraded")

    async def _advance_pipeline_frontier(
        self,
        stream_key: str,
        finalized_message_ids: list[str],
    ) -> None:
        """Advance only when every Redis delivery through the group head is ACKed."""
        if self.authority_pool is None:
            return
        if not finalized_message_ids:
            raise RuntimeError("pipeline frontier requires finalized deliveries")
        finalized = self._finalized_since_frontier.setdefault(stream_key, set())
        for message_id in finalized_message_ids:
            self._stream_id_parts(message_id)
            finalized.add(message_id)
        try:
            groups = await self.redis_client.xinfo_groups(stream_key)
            group = next(
                item
                for item in groups
                if self._redis_info_value(item, "name") == CONSUMER_GROUP
            )
            pending = int(self._redis_info_value(group, "pending") or 0)
            raw_lag = self._redis_info_value(group, "lag")
            if raw_lag is None:
                raise RuntimeError("Redis consumer-group lag is not provable")
            lag = int(raw_lag)
            if lag < 0:
                raise RuntimeError("Redis consumer-group lag is invalid")
            candidate = self._redis_info_value(group, "last-delivered-id")
            candidate_parts = self._stream_id_parts(candidate)
            raw_entries_read = self._redis_info_value(group, "entries-read")
            if isinstance(raw_entries_read, bool):
                raise RuntimeError("Redis consumer-group entries-read is invalid")
            entries_read = int(raw_entries_read)
            if entries_read < 0:
                raise RuntimeError("Redis consumer-group entries-read is invalid")
        except Exception:
            await self._degrade_pipeline_authority(
                stream_key,
                "stream_state_unverifiable",
            )
            raise
        if pending < 0:
            await self._degrade_pipeline_authority(
                stream_key,
                "stream_state_unverifiable",
            )
            raise RuntimeError("Redis consumer group returned negative pending count")
        if pending > 0:
            return

        project_id = self._project_id_from_stream(stream_key)
        clear_finalized = False
        async with self.authority_pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT
                        stream_key,
                        provenance_start_stream_id,
                        contiguous_stream_id,
                        consumer_group_entries_read,
                        status
                    FROM event_pipeline_watermarks
                    WHERE project_id = $1
                    FOR UPDATE
                    """,
                    project_id,
                )
                if row is None:
                    raise RuntimeError("pipeline watermark is missing")
                if self._postgres_value(row, "stream_key") != stream_key:
                    raise RuntimeError("pipeline watermark stream authority is invalid")
                if self._postgres_value(row, "status") == "degraded":
                    clear_finalized = True
                elif self._postgres_value(row, "status") != "healthy":
                    raise RuntimeError("pipeline watermark status is invalid")
                else:
                    provenance_start = self._stream_id_parts(
                        self._postgres_value(row, "provenance_start_stream_id")
                    )
                    current = self._stream_id_parts(
                        self._postgres_value(row, "contiguous_stream_id")
                    )
                    persisted_entries_read = self._postgres_value(
                        row,
                        "consumer_group_entries_read",
                    )
                    if type(persisted_entries_read) is not int:
                        raise RuntimeError(
                            "pipeline delivery-count authority is invalid"
                        )
                    new_finalized = {
                        message_id
                        for message_id in finalized
                        if self._stream_id_parts(message_id) > current
                    }
                    idempotent_commit = (
                        candidate_parts == current
                        and entries_read == persisted_entries_read
                        and not new_finalized
                    )
                    exact_advance = (
                        bool(new_finalized)
                        and candidate_parts > current
                        and candidate_parts
                        == max(
                            self._stream_id_parts(message_id)
                            for message_id in new_finalized
                        )
                        and entries_read - persisted_entries_read == len(new_finalized)
                    )
                    invalid_authority = (
                        candidate_parts < provenance_start
                        or candidate_parts < current
                        or entries_read < persisted_entries_read
                        or not (idempotent_commit or exact_advance)
                    )
                    if invalid_authority:
                        # entries-read is the group-wide delivery sequence. Its
                        # delta must exactly equal this process's finalized IDs;
                        # XGROUP SETID and concurrent consumers therefore fail
                        # closed even when the head happens to equal our batch.
                        await connection.execute(
                            """
                            UPDATE event_pipeline_watermarks
                            SET status = 'degraded',
                                failure_reason = 'stream_state_unverifiable',
                                updated_at = now()
                            WHERE project_id = $1
                              AND status = 'healthy'
                            """,
                            project_id,
                        )
                    elif exact_advance:
                        await connection.execute(
                            """
                            UPDATE event_pipeline_watermarks
                            SET contiguous_stream_id = $2,
                                consumer_group_entries_read = $3,
                                updated_at = now()
                            WHERE project_id = $1
                              AND status = 'healthy'
                            """,
                            project_id,
                            candidate,
                            entries_read,
                        )
                    clear_finalized = True
        if clear_finalized:
            self._finalized_since_frontier.pop(stream_key, None)

    async def _finalize_durable_stream(
        self,
        stream_key: str,
        message_ids: list[str],
    ) -> None:
        """Finalize one stream without sharing its PostgreSQL wait with peers."""
        await asyncio.wait_for(
            self._verify_boundary_deliveries(stream_key, message_ids),
            timeout=self.durable_ack_authority_timeout,
        )
        try:
            async with self.redis_client.pipeline(
                transaction=True
            ) as transaction:
                transaction.xack(
                    stream_key,
                    CONSUMER_GROUP,
                    *message_ids,
                )
                transaction.xdel(stream_key, *message_ids)
                results = await transaction.execute()
        except Exception:
            # EXEC may have committed even when its reply is lost. A retry may
            # therefore legitimately observe (0, 0).
            self._uncertain_redis_finalizations.add(stream_key)
            raise
        if (
            not isinstance(results, list)
            or len(results) != 2
            or any(type(value) is not int or value < 0 for value in results)
        ):
            raise RuntimeError("Redis ACK/delete returned an invalid result")
        expected = len(message_ids)
        result_pair = tuple(results)
        allowed_results = {(expected, expected)}
        if stream_key in self._uncertain_redis_finalizations:
            allowed_results.add((0, 0))
        if result_pair not in allowed_results:
            await asyncio.wait_for(
                self._degrade_pipeline_authority(
                    stream_key,
                    "stream_state_unverifiable",
                ),
                timeout=self.durable_ack_authority_timeout,
            )
            raise RuntimeError(
                "Redis ACK/delete did not finalize the exact durable batch"
            )
        # Redis has committed, but PostgreSQL may still fail. Keep the
        # ambiguity marker until its frontier transaction succeeds.
        self._uncertain_redis_finalizations.add(stream_key)
        await asyncio.wait_for(
            self._advance_pipeline_frontier(stream_key, message_ids),
            timeout=self.durable_ack_authority_timeout,
        )

    async def _ack_durable_messages(self) -> bool:
        """Finalize every due stream independently and retain only failures."""
        snapshots = [
            (stream_key, list(message_ids))
            for stream_key, message_ids in self._durable_pending_ack.items()
        ]
        if not snapshots:
            return True
        results = await asyncio.gather(
            *(
                self._finalize_durable_stream(stream_key, message_ids)
                for stream_key, message_ids in snapshots
            ),
            return_exceptions=True,
        )
        all_succeeded = True
        for (stream_key, message_ids), result in zip(
            snapshots,
            results,
            strict=True,
        ):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                all_succeeded = False
                self.stats["errors"] += 1
                logger.error(
                    "Durable finalization failed for %d events from %s; "
                    "retaining only this stream for retry: %s",
                    len(message_ids),
                    stream_key,
                    result,
                )
                continue

            self._uncertain_redis_finalizations.discard(stream_key)
            # EXEC is the commit boundary. If the connection fails before its
            # result is known, retain these IDs and safely retry the idempotent
            # XACK/XDEL pair rather than risking an unfinalized delivery.
            finalized_ids = set(message_ids)
            current_ids = self._durable_pending_ack.get(stream_key, [])
            remaining_ids = [
                message_id
                for message_id in current_ids
                if message_id not in finalized_ids
            ]
            if remaining_ids:
                self._durable_pending_ack[stream_key] = remaining_ids
            else:
                self._durable_pending_ack.pop(stream_key, None)
            for message_id in message_ids:
                self._boundary_tokens_by_delivery.pop(
                    (stream_key, message_id),
                    None,
                )

        return all_succeeded and not self._durable_pending_ack

    @staticmethod
    def _is_terminal_insert_error(exc: Exception) -> bool:
        """Recognize only local row-serialization failures as terminal.

        ServerException is deliberately absent: server schema/configuration
        errors and outages must retain the batch instead of dead-lettering it.
        """
        return isinstance(
            exc,
            (TypeMismatchError, TypeError, OverflowError, UnicodeError),
        )

    def _execute_insert(self, batch: list[BufferedEvent], query_id: str) -> None:
        # Re-check inside the single executor thread. A maintenance-loss signal
        # can close the gate after the event loop queues this future but before
        # the native driver begins the query.
        if not self._accepting_inserts:
            raise RuntimeError(
                "ClickHouse INSERTs are fenced while the writer is stopping"
            )
        rows = [event.row for event in batch if event.row is not None]
        if not rows:
            return
        self.ch_client.execute(
            EVENT_INSERT_QUERY,
            external_tables=[
                {
                    "name": EVENT_EXTERNAL_TABLE_NAME,
                    "structure": list(EVENT_INPUT_STRUCTURE),
                    "data": rows,
                }
            ],
            query_id=query_id,
            types_check=True,
        )

    def _execute_control_query(
        self,
        query: str,
        params: dict[str, str],
    ) -> list[tuple]:
        return self.ch_control_client.execute(query, params)

    async def _execute_control_query_async(
        self,
        query: str,
        params: dict[str, str],
    ) -> list[tuple]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._control_executor,
            self._execute_control_query,
            query,
            params,
        )

    async def _drain_inflight_insert(self) -> None:
        while self._inflight_insert is not None:
            try:
                async with self._drain_lock:
                    await self._drain_inflight_insert_locked()
            except BaseException as exc:
                logger.critical(
                    "ClickHouse drain interrupted before proof; retaining database "
                    "inhibitors and retrying: %s",
                    exc,
                )
                try:
                    await asyncio.sleep(0)
                except BaseException:
                    pass

    async def _drain_inflight_insert_locked(self) -> None:
        """Prove the current INSERT completed or is killed and no longer active.

        The local native-driver future must settle before an absence observation
        is authoritative. Otherwise a queued thread could register its query
        immediately after ``system.processes`` was checked. Failed control
        operations are retried indefinitely so callers retain their database
        inhibitors rather than allowing a migration to race an unknown INSERT.
        """
        while (inflight := self._inflight_insert) is not None:
            if inflight.future.done():
                try:
                    inflight.future.result()
                except BaseException:
                    pass
                else:
                    self._inflight_insert = None
                    return

            # Closing the data socket prevents a query that has not registered
            # yet from continuing normally. KILL handles one already executing.
            self._disconnect_clickhouse()
            kill_succeeded = False
            try:
                await self._execute_control_query_async(
                    "KILL QUERY WHERE query_id = %(query_id)s SYNC",
                    {"query_id": inflight.query_id},
                )
                kill_succeeded = True
            except BaseException as exc:
                logger.error(
                    "Could not kill ClickHouse query %s; retaining database "
                    "inhibitor and retrying: %s",
                    inflight.query_id,
                    exc,
                )

            local_call_settled = inflight.future.done()
            if not local_call_settled:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(inflight.future),
                        timeout=self.shutdown_timeout,
                    )
                except TimeoutError:
                    logger.error(
                        "ClickHouse query %s remains active in the native client; "
                        "retaining database inhibitor and retrying",
                        inflight.query_id,
                    )
                except BaseException:
                    local_call_settled = inflight.future.done()
                else:
                    # A successful native call is itself completion proof.
                    self._inflight_insert = None
                    return
            else:
                try:
                    inflight.future.result()
                except BaseException:
                    pass
                else:
                    self._inflight_insert = None
                    return

            if kill_succeeded and local_call_settled:
                try:
                    rows = await self._execute_control_query_async(
                        "SELECT count() FROM system.processes "
                        "WHERE query_id = %(query_id)s",
                        {"query_id": inflight.query_id},
                    )
                    if rows == [(0,)]:
                        self._inflight_insert = None
                        return
                    if len(rows) != 1 or len(rows[0]) != 1:
                        raise RuntimeError(
                            "system.processes returned an invalid query count"
                        )
                    logger.error(
                        "ClickHouse query %s is still registered after KILL QUERY "
                        "SYNC; retaining database inhibitor and retrying",
                        inflight.query_id,
                    )
                except BaseException as exc:
                    logger.error(
                        "Could not prove ClickHouse query %s absent; retaining "
                        "database inhibitor and retrying: %s",
                        inflight.query_id,
                        exc,
                    )

            try:
                await asyncio.sleep(min(MAINTENANCE_HEARTBEAT_SECONDS, 0.1))
            except BaseException as exc:
                logger.warning(
                    "ClickHouse drain retry delay interrupted for query %s; "
                    "continuing with inhibitors held: %s",
                    inflight.query_id,
                    exc,
                )

    async def _execute_insert_async(self, batch: list[BufferedEvent]) -> None:
        """Run the synchronous native driver in one bounded worker thread.

        ``asyncio.shield`` prevents coroutine cancellation from pretending the
        socket operation was cancelled. The in-flight batch is retained so a
        later flush observes the same result instead of submitting a duplicate
        insert while the original call is still running.
        """
        batch_key = tuple(batch)
        inflight = self._inflight_insert
        if inflight is None:
            if not self._accepting_inserts:
                raise RuntimeError(
                    "ClickHouse INSERTs are fenced while the writer is stopping"
                )
            query_id = f"apdl-runtime-writer-{uuid4()}"
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._db_executor,
                self._execute_insert,
                batch,
                query_id,
            )
            inflight = InFlightInsert(
                batch=batch_key,
                future=future,
                query_id=query_id,
            )
            self._inflight_insert = inflight
        elif inflight.batch != batch_key:
            raise RuntimeError(
                "A different ClickHouse batch was submitted while an insert "
                "was still in flight"
            )

        try:
            await asyncio.shield(inflight.future)
        except asyncio.CancelledError:
            # The worker thread is still running. Keep the future so the next
            # flush can observe its real success/failure without double-write.
            raise
        except Exception:
            # A socket error does not prove that the server stopped executing.
            # Resolve the registered query before a retry may get a new ID.
            await self._drain_inflight_insert()
            raise
        else:
            self._inflight_insert = None

    async def _insert_or_isolate(self, batch: list[BufferedEvent]) -> InsertOutcome:
        """Insert valid subsets and DLQ only proven singleton row failures."""
        if all(event.row is None for event in batch):
            return InsertOutcome(durable=batch, retry=[])
        try:
            await self._execute_insert_async(batch)
            return InsertOutcome(durable=batch, retry=[])
        except Exception as exc:
            if not self._is_terminal_insert_error(exc):
                return InsertOutcome(
                    durable=[],
                    retry=batch,
                    transient_error=exc,
                )

            if len(batch) == 1:
                event = batch[0]
                self.stats["errors"] += 1
                project_id = self._project_id_from_stream(event.stream_key)
                await self._dead_letter_delivery(
                    event.stream_key,
                    event.message_id,
                    project_id,
                    reason_code="clickhouse_row_rejected",
                    error=exc,
                )
                return InsertOutcome(durable=[], retry=[])

            midpoint = len(batch) // 2
            left = await self._insert_or_isolate(batch[:midpoint])
            right = await self._insert_or_isolate(batch[midpoint:])
            return InsertOutcome(
                durable=left.durable + right.durable,
                retry=left.retry + right.retry,
                transient_error=left.transient_error or right.transient_error,
            )

    async def _flush(self) -> bool:
        """Batch insert buffered events into ClickHouse.

        Redis deliveries are ACKed and deleted only after ClickHouse or the DLQ
        accepts them.
        Failed inserts remain buffered and apply backpressure to consumption;
        they are never silently dropped. A crash between ClickHouse insertion
        and Redis ACK can replay a row, so this is at-least-once delivery rather
        than exactly-once delivery.
        """
        async with self._flush_lock:
            ack_succeeded = True
            if self._durable_pending_ack:
                ack_succeeded = await self._ack_durable_messages()
            if not self.buffer:
                if ack_succeeded:
                    self._reset_flush_retry()
                return ack_succeeded

            batch = self.buffer.copy()
            outcome = await self._insert_or_isolate(batch)
            tail = self.buffer[len(batch) :]
            self.buffer = outcome.retry + tail

            if outcome.durable:
                self._queue_durable_ack(outcome.durable)
                self.stats["flushed"] += len(outcome.durable)
                self.last_flush = time.monotonic()
                logger.info("Flushed %d events to ClickHouse", len(outcome.durable))

            if outcome.transient_error is not None:
                delay = self._record_flush_failure()
                logger.error(
                    "ClickHouse flush failed (attempt %d, retrying in %.1fs): %s",
                    self._flush_retry_count,
                    delay,
                    outcome.transient_error,
                )
                self.stats["errors"] += 1

            # If an ACK failed at the start of this flush, do not hammer Redis
            # again immediately. Newly durable IDs stay queued for the shared
            # retry deadline. Otherwise ACK every ClickHouse/DLQ-durable record.
            if ack_succeeded and self._durable_pending_ack:
                ack_succeeded = await self._ack_durable_messages()

            if outcome.transient_error is None and ack_succeeded:
                self._reset_flush_retry()
                return True
            return False

    def _parse_event(
        self,
        data: dict,
        project_id: str,
        *,
        source_stream: str = "",
        source_stream_id: str = "",
    ) -> dict:
        """Parse a Redis stream message into a ClickHouse row dict.

        Expected Redis message fields:
            - event_json: str (JSON-encoded event payload)

        Project authority comes exclusively from the validated Redis stream
        key. Optional project assertions in the Redis fields or event JSON must
        match that authority.

        The event JSON must contain the canonical Ingestion contract fields:
            - event: str (canonical ClickHouse event name)
            - type: one of track, identify, group, page
            - user_id: str or null
            - anonymous_id: str or null
            - session_id: str (optional for non-browser events)
            - timestamp: str (ISO 8601)
            - properties: dict or null (null normalizes to an empty object)
            - context: strict nested SDK context
        """
        if not isinstance(data, dict):
            raise TypeError("Redis event fields must be an object")
        raw_event_json = data.get("event_json")
        if not isinstance(raw_event_json, str):
            raise TypeError("event_json must be a JSON string")
        event_json = json.loads(
            raw_event_json,
            parse_constant=self._reject_nonfinite_json,
            object_pairs_hook=self._reject_duplicate_json_keys,
        )
        if not isinstance(event_json, dict):
            raise ValueError("event_json must decode to an object")
        unknown_fields = sorted(set(event_json) - CANONICAL_EVENT_FIELDS)
        if unknown_fields:
            raise ValueError(
                "event_json contains unknown fields: " + ", ".join(unknown_fields)
            )
        missing_fields = sorted(CANONICAL_REQUIRED_EVENT_FIELDS - set(event_json))
        if missing_fields:
            raise ValueError(
                "event_json is missing required fields: " + ", ".join(missing_fields)
            )
        for field in (
            "user_id",
            "anonymous_id",
            "group_id",
            "properties",
            "traits",
            "session_id",
        ):
            if field in event_json and event_json[field] is None:
                raise ValueError(
                    f"optional event field {field!r} must be omitted rather than null"
                )
        for asserted_project in (
            data.get("project_id"),
            event_json.get("project_id"),
        ):
            if asserted_project is not None and asserted_project != project_id:
                raise ValueError("event project ID conflicts with stream authority")
        event_name = self._event_name(event_json)
        event_type = event_json.get("type")
        if event_type not in CANONICAL_EVENT_TYPES:
            raise ValueError("type must be one of: track, identify, group, page")
        expected_event = {
            "identify": "identify",
            "group": "group",
            "page": "page",
        }.get(event_type)
        if expected_event is not None and event_name != expected_event:
            raise ValueError(f"type {event_type!r} requires event {expected_event!r}")
        if not event_json.get("user_id") and not event_json.get("anonymous_id"):
            raise ValueError("event requires user_id or anonymous_id")
        if event_type == "identify" and not event_json.get("user_id"):
            raise ValueError("identify events require user_id")
        if event_type == "group" and not event_json.get("group_id"):
            raise ValueError("group events require group_id")
        for required_string in ("message_id",):
            value = event_json.get(required_string)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{required_string} must be a non-empty string")
        received_at = self._parse_timestamp(
            event_json.get("server_timestamp"),
            "server_timestamp",
        )
        timestamp = self._parse_timestamp(event_json.get("timestamp"), "timestamp")
        self._validate_event_timestamp_window(timestamp, received_at)
        if source_stream:
            if source_stream != self._stream_key_for_project(project_id):
                raise ValueError("source stream conflicts with project authority")
            source_stream_id_ms, source_stream_id_seq = self._stream_id_parts(
                source_stream_id
            )
        elif source_stream_id:
            raise ValueError("source stream ID requires a source stream")
        else:
            source_stream_id_ms, source_stream_id_seq = 0, 0

        context = event_json.get("context")
        if not isinstance(context, dict):
            raise TypeError("context must be an object")
        self._validate_context(context)
        properties = event_json.get("properties")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise TypeError("properties must be an object")
        traits = event_json.get("traits")
        if traits is None:
            traits = {}
        if not isinstance(traits, dict):
            raise TypeError("traits must be an object")

        browser = context.get("browser") or {}
        device = context.get("device") or {}
        if not isinstance(browser, dict):
            raise TypeError("context.browser must be an object")
        if not isinstance(device, dict):
            raise TypeError("context.device must be an object")

        row = {
            "project_id": project_id,
            "message_id": self._optional_string(event_json, "message_id"),
            "event_type": event_type,
            "event_name": event_name,
            "user_id": self._identity_string(event_json, "user_id"),
            "anonymous_id": self._identity_string(event_json, "anonymous_id"),
            "group_id": self._optional_string(event_json, "group_id"),
            "session_id": self._optional_string(event_json, "session_id"),
            "timestamp": timestamp,
            "received_at": received_at,
            "properties": json.dumps(
                properties,
                allow_nan=False,
                separators=(",", ":"),
            ),
            "traits": json.dumps(traits, allow_nan=False, separators=(",", ":")),
            "context": json.dumps(context, allow_nan=False, separators=(",", ":")),
            "ip": self._optional_string(event_json, "ip"),
            "country": "",
            "device_type": self._optional_string(device, "type"),
            "browser": self._optional_string(browser, "name"),
            "source_stream": source_stream,
            "source_stream_id": source_stream_id,
            "source_stream_id_ms": source_stream_id_ms,
            "source_stream_id_seq": source_stream_id_seq,
        }
        self._validate_clickhouse_row(row)
        return row

    @staticmethod
    def _reject_nonfinite_json(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value} is not canonical")

    @staticmethod
    def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key: {key}")
            value[key] = item
        return value

    @classmethod
    def _validate_context(cls, context: dict[str, Any]) -> None:
        cls._reject_unknown_object_fields(
            context,
            CANONICAL_CONTEXT_FIELDS,
            "context",
        )
        for field in ("library", "browser", "os"):
            if field in context:
                cls._validate_nested_object(
                    context[field],
                    {"name", "version"},
                    f"context.{field}",
                )
                cls._require_string(
                    context[field]["name"],
                    f"context.{field}.name",
                    128,
                    non_empty=True,
                )
                cls._require_string(
                    context[field]["version"],
                    f"context.{field}.version",
                    128,
                    non_empty=True,
                )
        if "device" in context:
            cls._validate_nested_object(context["device"], {"type"}, "context.device")
            cls._require_string(
                context["device"]["type"],
                "context.device.type",
                64,
                non_empty=True,
            )
        for field in ("screen", "viewport"):
            if field in context:
                cls._validate_nested_object(
                    context[field],
                    {"width", "height"},
                    f"context.{field}",
                )
                for dimension in ("width", "height"):
                    cls._require_dimension(
                        context[field][dimension],
                        f"context.{field}.{dimension}",
                    )
        if "page" in context:
            cls._validate_nested_object(
                context["page"],
                {"url", "title", "path", "search"},
                "context.page",
            )
            for field, maximum in (
                ("url", 4096),
                ("title", 1024),
                ("path", 2048),
                ("search", 2048),
            ):
                cls._require_string(
                    context["page"][field],
                    f"context.page.{field}",
                    maximum,
                )
        for field, maximum in (
            ("locale", 128),
            ("timezone", 128),
            ("referrer", 4096),
        ):
            if field in context:
                cls._require_string(context[field], f"context.{field}", maximum)

    @staticmethod
    def _require_string(
        value: Any,
        path: str,
        maximum: int,
        *,
        non_empty: bool = False,
    ) -> None:
        if not isinstance(value, str):
            raise TypeError(f"{path} must be a string")
        if non_empty and not value:
            raise ValueError(f"{path} must not be empty")
        if len(value) > maximum:
            raise ValueError(f"{path} exceeds maximum length of {maximum}")

    @staticmethod
    def _require_dimension(value: Any, path: str) -> None:
        if type(value) is not int or not 0 <= value <= 100_000:
            raise ValueError(f"{path} must be an integer from 0 through 100000")

    @classmethod
    def _validate_nested_object(
        cls,
        value: Any,
        fields: set[str],
        path: str,
    ) -> None:
        if not isinstance(value, dict):
            raise TypeError(f"{path} must be an object")
        cls._reject_unknown_object_fields(value, fields, path)
        missing = sorted(fields - set(value))
        if missing:
            raise ValueError(f"{path} is missing fields: {', '.join(missing)}")

    @staticmethod
    def _reject_unknown_object_fields(
        value: dict[str, Any],
        allowed: set[str] | frozenset[str],
        path: str,
    ) -> None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"{path} contains unknown fields: {', '.join(unknown)}")

    @staticmethod
    def _parse_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be an RFC3339 UTC string")
        match = RFC3339_UTC_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(f"{field} must use canonical RFC3339 UTC format")
        year, month, day, hour, minute, second = (
            int(part) for part in match.groups()[:6]
        )
        if year < 1000:
            raise ValueError(f"{field} year must contain four canonical digits")
        microsecond = int((match.group(7) or "").ljust(6, "0"))
        try:
            parsed = datetime(
                year,
                month,
                day,
                hour,
                minute,
                second,
                microsecond,
                tzinfo=timezone.utc,
            )
        except ValueError as exc:
            raise ValueError(f"{field} must be a valid RFC3339 value") from exc
        return parsed

    @staticmethod
    def _validate_event_timestamp_window(
        timestamp: datetime,
        received_at: datetime,
    ) -> None:
        if timestamp < received_at - timedelta(seconds=MAX_EVENT_AGE_SECONDS):
            raise ValueError(
                "timestamp is more than 7 days older than server receipt time"
            )
        if timestamp > received_at + timedelta(
            seconds=MAX_EVENT_FUTURE_SKEW_SECONDS
        ):
            raise ValueError(
                "timestamp is more than 5 minutes ahead of server receipt time"
            )

    @staticmethod
    def _event_name(payload: dict[str, Any]) -> str:
        value = payload.get("event")
        if not isinstance(value, str) or not value:
            raise TypeError("event must be a non-empty string")
        return value

    @staticmethod
    def _identity_string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        return value

    @staticmethod
    def _optional_string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        return value

    @staticmethod
    def _validate_clickhouse_row(row: dict[str, Any]) -> None:
        for field in (
            "project_id",
            "message_id",
            "event_type",
            "event_name",
            "user_id",
            "anonymous_id",
            "group_id",
            "session_id",
            "properties",
            "traits",
            "context",
            "ip",
            "country",
            "device_type",
            "browser",
            "source_stream",
            "source_stream_id",
        ):
            if not isinstance(row[field], str):
                raise TypeError(f"ClickHouse row field {field} must be a string")
        if not isinstance(row["timestamp"], datetime):
            raise TypeError("ClickHouse row field timestamp must be a datetime")
        if not isinstance(row["received_at"], datetime):
            raise TypeError("ClickHouse row field received_at must be a datetime")
        for field in ("source_stream_id_ms", "source_stream_id_seq"):
            if type(row[field]) is not int or row[field] < 0 or row[field] >= 2**64:
                raise TypeError(
                    f"ClickHouse row field {field} must be an unsigned 64-bit integer"
                )


async def main():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    postgres_url = os.environ.get(
        "POSTGRES_URL",
        "postgresql://apdl:apdl_dev@localhost:5432/apdl",
    )
    clickhouse_url = os.environ.get(
        "CLICKHOUSE_NATIVE_URL",
        "clickhouse://apdl:apdl_dev@localhost:9000/apdl",
    )
    buffer_size = int(os.environ.get("BUFFER_SIZE", "1000"))
    flush_interval = float(os.environ.get("FLUSH_INTERVAL", "5.0"))
    dlq_maxlen = int(os.environ.get("DLQ_MAXLEN", str(DEFAULT_DLQ_MAXLEN)))
    pending_claim_idle_ms = int(
        os.environ.get("PENDING_CLAIM_IDLE_MS", str(PENDING_CLAIM_IDLE_MS))
    )
    pending_claim_interval = float(
        os.environ.get(
            "PENDING_CLAIM_INTERVAL_SECONDS",
            str(PENDING_CLAIM_INTERVAL_SECONDS),
        )
    )
    clickhouse_connect_timeout = float(
        os.environ.get(
            "CLICKHOUSE_CONNECT_TIMEOUT_SECONDS",
            str(CLICKHOUSE_CONNECT_TIMEOUT_SECONDS),
        )
    )
    clickhouse_send_receive_timeout = float(
        os.environ.get(
            "CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS",
            str(CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS),
        )
    )
    clickhouse_sync_request_timeout = float(
        os.environ.get(
            "CLICKHOUSE_SYNC_REQUEST_TIMEOUT_SECONDS",
            str(CLICKHOUSE_SYNC_REQUEST_TIMEOUT_SECONDS),
        )
    )
    shutdown_timeout = float(
        os.environ.get(
            "SHUTDOWN_TIMEOUT_SECONDS",
            str(SHUTDOWN_TIMEOUT_SECONDS),
        )
    )

    # Optional: comma-separated list of project IDs to consume
    project_ids_env = os.environ.get("PROJECT_IDS", "")
    project_ids = (
        [pid.strip() for pid in project_ids_env.split(",") if pid.strip()]
        if project_ids_env
        else None
    )

    maintenance_pool = await asyncpg.create_pool(
        postgres_url,
        min_size=2,
        max_size=3,
    )
    maintenance_connections = []
    writer = None
    maintenance_monitors = []
    termination_listeners = []
    connection_lost_events = []
    try:
        # Keep two dedicated backend sessions checked out for the entire writer
        # lifetime. The first exclusively owns the one supported consumer-group
        # writer authority; both redundantly block migrations. Losing either
        # session fences new writes. A total PostgreSQL restart can remove both
        # sessions together, so Compose quiescence must also observe this writer
        # stopped before it permits migration execution.
        for inhibitor_index in range(2):
            maintenance_connection = await maintenance_pool.acquire()
            maintenance_connections.append(maintenance_connection)
            await _acquire_maintenance_inhibitor(maintenance_connection)
            if inhibitor_index == 0:
                # Hold both shared migration guards continuously across the
                # capability check and runtime. The schema cannot race between
                # validation and startup; failure releases this backend and
                # every session-scoped lock in the outer cleanup.
                await _assert_boundary_marker_schema(maintenance_connection)
                await _acquire_writer_singleton(maintenance_connection)

        writer = ClickHouseWriter(
            redis_url=redis_url,
            clickhouse_url=clickhouse_url,
            buffer_size=buffer_size,
            flush_interval=flush_interval,
            dlq_maxlen=dlq_maxlen,
            pending_claim_idle_ms=pending_claim_idle_ms,
            pending_claim_interval=pending_claim_interval,
            clickhouse_connect_timeout=clickhouse_connect_timeout,
            clickhouse_send_receive_timeout=clickhouse_send_receive_timeout,
            clickhouse_sync_request_timeout=clickhouse_sync_request_timeout,
            shutdown_timeout=shutdown_timeout,
            authority_pool=maintenance_pool,
        )

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(writer.stop()))

        for maintenance_connection in maintenance_connections:
            connection_lost = asyncio.Event()
            connection_lost_events.append(connection_lost)

            def mark_connection_lost(
                _connection,
                *,
                lost_event=connection_lost,
            ) -> None:
                loop.call_soon_threadsafe(lost_event.set)

            termination_listeners.append(mark_connection_lost)
            maintenance_connection.add_termination_listener(mark_connection_lost)

        try:
            # Close the acquire-to-monitor gap: no Redis consumption begins until
            # both backends positively prove both lock IDs are still held.
            await asyncio.gather(
                *(
                    asyncio.wait_for(
                        _heartbeat_maintenance_inhibitor(maintenance_connection),
                        timeout=MAINTENANCE_HEARTBEAT_SECONDS,
                    )
                    for maintenance_connection in maintenance_connections
                ),
                asyncio.wait_for(
                    _heartbeat_writer_singleton(maintenance_connections[0]),
                    timeout=MAINTENANCE_HEARTBEAT_SECONDS,
                ),
            )
            for inhibitor_index, (
                maintenance_connection,
                connection_lost,
            ) in enumerate(
                zip(
                    maintenance_connections,
                    connection_lost_events,
                    strict=True,
                ),
                start=1,
            ):
                maintenance_monitors.append(
                    asyncio.create_task(
                        _monitor_maintenance_inhibitor(
                            maintenance_connection,
                            writer,
                            connection_lost,
                            require_writer_singleton=inhibitor_index == 1,
                        ),
                        name=f"maintenance-inhibitor-monitor-{inhibitor_index}",
                    )
                )

            await writer.start(project_ids)
        finally:
            try:
                await writer.stop()
            finally:
                for maintenance_monitor in maintenance_monitors:
                    maintenance_monitor.cancel()
                await asyncio.gather(*maintenance_monitors, return_exceptions=True)
                for maintenance_connection, listener in zip(
                    maintenance_connections,
                    termination_listeners,
                    strict=True,
                ):
                    maintenance_connection.remove_termination_listener(listener)
    finally:
        # Reaching this point means writer.stop proved every native INSERT is
        # complete or synchronously killed and absent from system.processes.
        for maintenance_connection in reversed(maintenance_connections):
            await maintenance_pool.release(maintenance_connection)
        await maintenance_pool.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
