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
import signal
import time
from datetime import datetime, timezone

import redis.asyncio as redis
from clickhouse_driver import Client as ClickHouseClient

logger = logging.getLogger(__name__)

STREAM_PREFIX = "events:raw:"
CONSUMER_GROUP = "clickhouse-writer"
# Maximum number of times we retry a failed flush before dropping the batch
# and logging an error. Prevents unbounded buffer growth if ClickHouse is down
# for an extended period.
MAX_FLUSH_RETRIES = 5


class ClickHouseWriter:
    """Consumes events from Redis Streams and writes to ClickHouse in batches."""

    def __init__(
        self,
        redis_url: str,
        clickhouse_url: str,
        buffer_size: int = 1000,
        flush_interval: float = 5.0,
    ):
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
        self.ch_client = ClickHouseClient.from_url(clickhouse_url)
        self.buffer: list[dict] = []
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.running = False
        self.last_flush = time.monotonic()
        self.consumer_name = f"worker-{os.getpid()}"
        self.stats = {"consumed": 0, "flushed": 0, "errors": 0, "dropped": 0}
        self._flush_retry_count = 0

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

        For each stream, we create the consumer group starting from the
        latest message (ID='$') so we don't replay historical data on
        first deployment. For replay scenarios, use '0' instead.
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
                stream_keys = await self._get_stream_keys(project_ids)
                if not stream_keys:
                    # No streams found yet, wait and retry
                    await asyncio.sleep(2.0)
                    continue

                # Build the streams dict: {stream_key: ">"} means read new messages
                streams_arg = {key: ">" for key in stream_keys}

                # XREADGROUP blocks for up to 1 second waiting for new messages
                results = await self.redis_client.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=self.consumer_name,
                    streams=streams_arg,
                    count=self.buffer_size,
                    block=1000,
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
        remain in the Pending Entries List (PEL). We read those first to
        ensure at-least-once delivery.
        """
        logger.info("Checking for pending messages")
        stream_keys = await self._get_stream_keys(project_ids)

        for stream_key in stream_keys:
            while self.running:
                try:
                    # Read pending messages (ID "0" means start of PEL)
                    results = await self.redis_client.xreadgroup(
                        groupname=CONSUMER_GROUP,
                        consumername=self.consumer_name,
                        streams={stream_key: "0"},
                        count=self.buffer_size,
                    )

                    if not results:
                        break

                    # Check if we actually got messages (empty list means PEL is clear)
                    has_messages = False
                    for _, messages in results:
                        if messages:
                            has_messages = True
                            break

                    if not has_messages:
                        break

                    await self._process_messages(results)
                    logger.info(
                        "Processed pending messages from '%s'", stream_key
                    )
                except Exception as e:
                    logger.error(
                        "Error processing pending from '%s': %s",
                        stream_key,
                        e,
                    )
                    break

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
    ):
        """Parse messages from XREADGROUP results and add to buffer.

        Each result is a tuple of (stream_key, [(message_id, fields), ...]).
        After parsing and buffering, we ACK each message immediately. The
        buffer is flushed to ClickHouse when it reaches buffer_size.
        """
        for stream_key, messages in results:
            # Extract project_id from stream key: events:raw:{project_id}
            project_id_str = stream_key.removeprefix(STREAM_PREFIX)
            message_ids = []

            for message_id, data in messages:
                try:
                    # Inject project_id from the stream key if not in the message
                    if "project_id" not in data:
                        data["project_id"] = project_id_str
                    parsed = self._parse_event(data)
                    self.buffer.append(parsed)
                    self.stats["consumed"] += 1
                    message_ids.append(message_id)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(
                        "Skipping malformed message %s from %s: %s",
                        message_id,
                        stream_key,
                        e,
                    )
                    # ACK bad messages so they don't block the PEL
                    message_ids.append(message_id)

            # ACK all processed messages in a single pipeline call
            if message_ids:
                await self.redis_client.xack(
                    stream_key, CONSUMER_GROUP, *message_ids
                )

        # Flush if buffer is full
        if len(self.buffer) >= self.buffer_size:
            await self._flush()

    async def _flush_loop(self):
        """Periodic flush based on time interval.

        Ensures events are written to ClickHouse even when the buffer
        hasn't reached buffer_size, keeping latency bounded.
        """
        while self.running:
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - self.last_flush
            if elapsed >= self.flush_interval and self.buffer:
                await self._flush()

    async def _flush(self):
        """Batch insert buffered events into ClickHouse.

        On failure, events are put back into the buffer for retry on the
        next flush cycle. After MAX_FLUSH_RETRIES consecutive failures,
        the batch is dropped to prevent unbounded memory growth.
        """
        if not self.buffer:
            return

        batch = self.buffer.copy()
        self.buffer.clear()

        try:
            self.ch_client.execute(
                "INSERT INTO events ("
                "project_id, event_name, user_id, anonymous_id, "
                "session_id, timestamp, properties, country, "
                "device_type, browser"
                ") VALUES",
                batch,
                types_check=True,
            )
            self.stats["flushed"] += len(batch)
            self.last_flush = time.monotonic()
            self._flush_retry_count = 0
            logger.info("Flushed %d events to ClickHouse", len(batch))
        except Exception as e:
            self._flush_retry_count += 1
            logger.error(
                "ClickHouse flush failed (attempt %d/%d): %s",
                self._flush_retry_count,
                MAX_FLUSH_RETRIES,
                e,
            )
            self.stats["errors"] += 1

            if self._flush_retry_count >= MAX_FLUSH_RETRIES:
                logger.error(
                    "Dropping %d events after %d consecutive flush failures",
                    len(batch),
                    MAX_FLUSH_RETRIES,
                )
                self.stats["dropped"] += len(batch)
                self._flush_retry_count = 0
            else:
                # Put events back in buffer for retry
                self.buffer = batch + self.buffer

    def _parse_event(self, data: dict) -> dict:
        """Parse a Redis stream message into a ClickHouse row dict.

        Expected Redis message fields:
            - project_id: str (the project identifier)
            - event_json: str (JSON-encoded event payload)

        The event JSON should contain:
            - event: str (event name)
            - user_id: str
            - anonymous_id: str
            - session_id: str
            - timestamp: str (ISO 8601)
            - properties: dict
            - country: str (optional)
            - context: dict with device_type, browser (optional)
        """
        event_json = json.loads(data.get("event_json", "{}"))
        raw_timestamp = event_json.get("timestamp")
        if raw_timestamp:
            timestamp = datetime.fromisoformat(raw_timestamp)
        else:
            timestamp = datetime.now(timezone.utc)

        context = event_json.get("context", {})
        project_id = data.get("project_id") or event_json.get("project_id", "")

        return {
            "project_id": str(project_id),
            "event_name": event_json.get("event", ""),
            "user_id": event_json.get("user_id", ""),
            "anonymous_id": event_json.get("anonymous_id", ""),
            "session_id": event_json.get("session_id", ""),
            "timestamp": timestamp,
            "properties": json.dumps(event_json.get("properties", {})),
            "country": event_json.get("country", ""),
            "device_type": context.get("device_type", ""),
            "browser": context.get("browser", ""),
        }


async def main():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    clickhouse_url = os.environ.get(
        "CLICKHOUSE_URL", "clickhouse://localhost:9000/apdl"
    )
    buffer_size = int(os.environ.get("BUFFER_SIZE", "1000"))
    flush_interval = float(os.environ.get("FLUSH_INTERVAL", "5.0"))

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
