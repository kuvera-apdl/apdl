"""
Redis Streams to ClickHouse event writer.
Reads events from Redis Streams and batch-inserts into ClickHouse.

Each project's events live in a separate stream keyed as events:raw:{project_id}.
This writer uses consumer groups for reliable, at-least-once delivery with
automatic retry on ClickHouse flush failures.

Usage:
    REDIS_URL=redis://localhost:6379 \
    CLICKHOUSE_URL=clickhouse://localhost:9000/apdl \
    python clickhouse_writer.py
"""

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis
from clickhouse_driver import Client as ClickHouseClient
from clickhouse_driver.errors import TypeMismatchError

logger = logging.getLogger(__name__)

STREAM_PREFIX = "events:raw:"
DLQ_STREAM_PREFIX = "events:dlq:"
CONSUMER_GROUP = "clickhouse-writer"
DEFAULT_DLQ_MAXLEN = 10_000
FLUSH_RETRY_BASE_SECONDS = 1.0
FLUSH_RETRY_MAX_SECONDS = 30.0
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
PENDING_CLAIM_IDLE_MS = 60_000
PENDING_CLAIM_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class BufferedEvent:
    """A parsed row plus the Redis delivery that must remain pending for it."""

    stream_key: str
    message_id: str
    row: dict


@dataclass
class InsertOutcome:
    """Results of inserting a batch while isolating terminal row failures."""

    durable: list[BufferedEvent]
    retry: list[BufferedEvent]
    transient_error: Exception | None = None


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
    ):
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive")
        if dlq_maxlen <= 0:
            raise ValueError("dlq_maxlen must be positive")
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
        self.ch_client = ClickHouseClient.from_url(clickhouse_url)
        self.buffer: list[BufferedEvent] = []
        self._durable_pending_ack: dict[str, list[str]] = {}
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.dlq_maxlen = dlq_maxlen
        self.pending_claim_idle_ms = pending_claim_idle_ms
        self.pending_claim_interval = pending_claim_interval
        self.running = False
        self.last_flush = time.monotonic()
        self._last_pending_claim = 0.0
        self.consumer_name = f"worker-{os.getpid()}"
        self.stats = {
            "consumed": 0,
            "flushed": 0,
            "rejected": 0,
            "dead_lettered": 0,
            "errors": 0,
        }
        self._flush_retry_count = 0
        self._next_flush_retry_at = 0.0
        self._flush_lock = asyncio.Lock()
        self._new_stream_cursor = 0
        self._pending_stream_cursor = 0

    async def start(self, project_ids: list[str] | None = None):
        """Start consuming from all project streams.

        Args:
            project_ids: Explicit list of project IDs to consume. If None,
                         streams are discovered dynamically via SCAN.
        """
        self.running = True
        logger.info("ClickHouseWriter starting, consumer=%s", self.consumer_name)

        # Ensure consumer groups exist for known streams
        await self._ensure_consumer_groups(project_ids)

        # Run consumer and periodic flusher concurrently
        try:
            await asyncio.gather(
                self._consume_loop(project_ids),
                self._flush_loop(),
            )
        except asyncio.CancelledError:
            logger.info("Tasks cancelled, performing final flush")
            await self._flush()

    async def stop(self):
        """Graceful shutdown."""
        self.running = False
        await self._flush()
        await self.redis_client.aclose()
        logger.info("ClickHouseWriter stopped. Stats: %s", self.stats)

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
            streams.extend(keys)
            if cursor == 0:
                break
        return streams

    async def _ensure_consumer_groups(self, project_ids: list[str] | None):
        """Create consumer groups if they don't exist.

        Groups initially start at the latest message. Backlog consumption is
        changed separately by APDL-AUD-070.
        """
        if project_ids:
            stream_keys = [f"{STREAM_PREFIX}{pid}" for pid in project_ids]
        else:
            stream_keys = await self._discover_streams()

        for stream_key in stream_keys:
            try:
                await self.redis_client.xgroup_create(
                    name=stream_key,
                    groupname=CONSUMER_GROUP,
                    id="$",
                    mkstream=True,
                )
                logger.info(
                    "Created consumer group '%s' on stream '%s'",
                    CONSUMER_GROUP,
                    stream_key,
                )
            except redis.ResponseError as e:
                # BUSYGROUP means the group already exists -- that is fine
                if "BUSYGROUP" in str(e):
                    logger.debug(
                        "Consumer group '%s' already exists on '%s'",
                        CONSUMER_GROUP,
                        stream_key,
                    )
                else:
                    raise

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

                stream_key = self._next_stream_key(stream_keys, pending=False)
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
                    block=max(1, 1000 // len(stream_keys)),
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
                    next_start_id, messages = self._claimed_messages(claimed)
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

    @staticmethod
    def _claimed_messages(claimed) -> tuple[str, list[tuple[str, dict]]]:
        """Normalize Redis 6.2/7 XAUTOCLAIM response variants."""
        if not claimed or len(claimed) < 2:
            return "0-0", []
        return str(claimed[0]), list(claimed[1])

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

        If project_ids are specified, use them directly. Otherwise, discover
        streams dynamically. Also ensures consumer groups exist for any
        newly discovered streams.
        """
        if project_ids:
            stream_keys = [f"{STREAM_PREFIX}{pid}" for pid in project_ids]
        else:
            stream_keys = await self._discover_streams()

        # Ensure consumer groups exist for any new streams
        for stream_key in stream_keys:
            try:
                await self.redis_client.xgroup_create(
                    name=stream_key,
                    groupname=CONSUMER_GROUP,
                    id="$",
                    mkstream=True,
                )
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise

        return stream_keys

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
            project_id = stream_key.removeprefix(STREAM_PREFIX)
            for message_id, data in messages:
                try:
                    parsed = self._parse_event(data, project_id)
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
                    )
                )
                self.stats["consumed"] += 1
                buffered_count += 1

        # Flush if buffer is full
        if len(self.buffer) >= self.buffer_size and self._flush_retry_is_due():
            await self._flush()
        return buffered_count

    @staticmethod
    def _dlq_stream_for_project(project_id: str) -> str:
        if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
            raise ValueError("DLQ project ID is not canonical")
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
            # later pending sweep can retry DLQ persistence.
            self.stats["errors"] += 1
            logger.error(
                "Could not persist reject metadata for %s on %s: %s",
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
                continue
            elapsed = time.monotonic() - self.last_flush
            if self._durable_pending_ack or (
                self.buffer
                and (self._flush_retry_count > 0 or elapsed >= self.flush_interval)
            ):
                await self._flush()

    def _delivery_is_backpressured(self) -> bool:
        return bool(self._durable_pending_ack) or len(self.buffer) >= self.buffer_size

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

    async def _ack_durable_messages(self) -> bool:
        """ACK rows already inserted into ClickHouse, grouped by Redis stream."""
        for stream_key, message_ids in list(self._durable_pending_ack.items()):
            try:
                await self.redis_client.xack(
                    stream_key,
                    CONSUMER_GROUP,
                    *message_ids,
                )
            except Exception as exc:
                delay = self._record_flush_failure()
                self.stats["errors"] += 1
                logger.error(
                    "Redis ACK failed for %d durable events from %s "
                    "(retrying in %.1fs): %s",
                    len(message_ids),
                    stream_key,
                    delay,
                    exc,
                )
                return False
            del self._durable_pending_ack[stream_key]

        return True

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

    def _execute_insert(self, batch: list[BufferedEvent]) -> None:
        self.ch_client.execute(
            "INSERT INTO events ("
            "project_id, event_name, user_id, anonymous_id, "
            "session_id, timestamp, properties, country, "
            "device_type, browser"
            ") VALUES",
            [event.row for event in batch],
            types_check=True,
        )

    async def _insert_or_isolate(self, batch: list[BufferedEvent]) -> InsertOutcome:
        """Insert valid subsets and DLQ only proven singleton row failures."""
        try:
            self._execute_insert(batch)
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
                project_id = event.stream_key.removeprefix(STREAM_PREFIX)
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

        Redis deliveries are ACKed only after ClickHouse accepts their rows.
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

    def _parse_event(self, data: dict, project_id: str) -> dict:
        """Parse a Redis stream message into a ClickHouse row dict.

        Expected Redis message fields:
            - event_json: str (JSON-encoded event payload)

        ``project_id`` is the stream-derived fallback. Stream-only project
        authority is enforced separately by APDL-AUD-104.

        The event JSON should contain the ingestion contract fields:
            - event or type: str (canonical ClickHouse event name)
            - user_id/userId: str
            - anonymous_id/anonymousId: str
            - session_id: str
            - timestamp: str (ISO 8601)
            - properties: dict or null (null normalizes to an empty object)
            - country: str (optional)
            - context: dict with device_type, browser, or null (optional)
        """
        if not isinstance(data, dict):
            raise TypeError("Redis event fields must be an object")
        raw_event_json = data.get("event_json")
        if not isinstance(raw_event_json, str):
            raise TypeError("event_json must be a JSON string")
        event_json = json.loads(
            raw_event_json,
            parse_constant=self._reject_nonfinite_json,
        )
        if not isinstance(event_json, dict):
            raise ValueError("event_json must decode to an object")
        selected_project = (
            data.get("project_id") or event_json.get("project_id") or project_id
        )
        if not isinstance(selected_project, str):
            raise TypeError("project_id must be a string")
        event_name = self._event_name(event_json)
        raw_timestamp = event_json.get("timestamp")
        if raw_timestamp not in (None, ""):
            if not isinstance(raw_timestamp, str):
                raise TypeError("timestamp must be an ISO 8601 string")
            timestamp = datetime.fromisoformat(raw_timestamp)
        else:
            timestamp = datetime.now(timezone.utc)

        context = event_json.get("context")
        if context is None:
            context = {}
        if not isinstance(context, dict):
            raise TypeError("context must be an object")
        properties = event_json.get("properties")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise TypeError("properties must be an object")

        row = {
            "project_id": selected_project,
            "event_name": event_name,
            "user_id": self._identity_string(event_json, "user_id", "userId"),
            "anonymous_id": self._identity_string(
                event_json,
                "anonymous_id",
                "anonymousId",
            ),
            "session_id": self._optional_string(event_json, "session_id"),
            "timestamp": timestamp,
            "properties": json.dumps(
                properties,
                allow_nan=False,
                separators=(",", ":"),
            ),
            "country": self._optional_string(event_json, "country"),
            "device_type": self._optional_string(context, "device_type"),
            "browser": self._optional_string(context, "browser"),
        }
        self._validate_clickhouse_row(row)
        return row

    @staticmethod
    def _reject_nonfinite_json(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value} is not canonical")

    @staticmethod
    def _event_name(payload: dict[str, Any]) -> str:
        for field in ("event", "type"):
            value = payload.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                raise TypeError(f"{field} must be a string")
            return value
        raise TypeError("event or type must be a non-empty string")

    @staticmethod
    def _identity_string(payload: dict[str, Any], canonical: str, legacy: str) -> str:
        canonical_value = payload.get(canonical)
        legacy_value = payload.get(legacy)
        for field, value in (
            (canonical, canonical_value),
            (legacy, legacy_value),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field} must be a string")
        if canonical_value and legacy_value and canonical_value != legacy_value:
            raise ValueError(f"{canonical} and {legacy} conflict")
        return canonical_value or legacy_value or ""

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
            "event_name",
            "user_id",
            "anonymous_id",
            "session_id",
            "properties",
            "country",
            "device_type",
            "browser",
        ):
            if not isinstance(row[field], str):
                raise TypeError(f"ClickHouse row field {field} must be a string")
        if not isinstance(row["timestamp"], datetime):
            raise TypeError("ClickHouse row field timestamp must be a datetime")


async def main():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    clickhouse_url = os.environ.get(
        "CLICKHOUSE_URL", "clickhouse://localhost:9000/apdl"
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

    # Optional: comma-separated list of project IDs to consume
    project_ids_env = os.environ.get("PROJECT_IDS", "")
    project_ids = (
        [pid.strip() for pid in project_ids_env.split(",") if pid.strip()]
        if project_ids_env
        else None
    )

    writer = ClickHouseWriter(
        redis_url=redis_url,
        clickhouse_url=clickhouse_url,
        buffer_size=buffer_size,
        flush_interval=flush_interval,
        dlq_maxlen=dlq_maxlen,
        pending_claim_idle_ms=pending_claim_idle_ms,
        pending_claim_interval=pending_claim_interval,
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(writer.stop()))

    await writer.start(project_ids)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
