"""
Flink sessionization job (Phase 3+).

Reads enriched events from the events.enriched Kafka topic, groups them
into sessions using a 30-minute inactivity gap, computes session-level
metrics, and writes completed sessions to ClickHouse.

Session definition:
  - A session is a sequence of events from the same (project_id, user_or_anon_id)
    where no two consecutive events are more than 30 minutes apart.
  - When the gap exceeds 30 minutes, the current session is closed and
    a new session begins.

Requires:
  - PyFlink 1.18+
  - Kafka brokers at KAFKA_BROKERS
  - ClickHouse JDBC connector jar
"""
import json
import logging
import os
from datetime import datetime

from pyflink.common import Row, Types, WatermarkStrategy, Duration
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import (
    KafkaOffsetsInitializer,
    KafkaSource,
)
from pyflink.datastream.functions import (
    KeyedProcessFunction,
    RuntimeContext,
)
from pyflink.datastream.state import ValueStateDescriptor, ListStateDescriptor
from pyflink.datastream.window import EventTimeSessionWindows, Time
from pyflink.datastream.functions import ProcessWindowFunction
from pyflink.common.time import Time as CommonTime

logger = logging.getLogger(__name__)

KAFKA_BROKERS = os.environ.get("KAFKA_BROKERS", "localhost:9092")
CLICKHOUSE_URL = os.environ.get(
    "CLICKHOUSE_JDBC_URL",
    "jdbc:clickhouse://localhost:8123/apdl",
)
INPUT_TOPIC = "events.enriched"
CONSUMER_GROUP = "flink-sessionization"
SESSION_GAP_MS = 30 * 60 * 1000  # 30 minutes


class EventTimestampAssigner(TimestampAssigner):
    """Extracts event timestamps for Flink's event-time processing.

    Parses the ISO 8601 timestamp from each event's JSON payload and
    converts it to epoch milliseconds for watermark generation.
    """

    def extract_timestamp(self, value: str, record_timestamp: int) -> int:
        try:
            event = json.loads(value)
            ts_str = event.get("timestamp", "")
            if ts_str:
                dt = datetime.fromisoformat(ts_str)
                return int(dt.timestamp() * 1000)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        # Fall back to the Kafka record timestamp
        return record_timestamp


class SessionWindowFunction(ProcessWindowFunction):
    """Processes a session window of events and emits a session summary row.

    Called once per session window when it fires (after the 30-minute
    inactivity gap). Receives all events in the session, computes
    aggregate metrics, and outputs a Row for ClickHouse insertion.
    """

    def process(
        self,
        key: str,
        context: ProcessWindowFunction.Context,
        elements: list[str],
    ):
        """Aggregate events within a session window.

        Args:
            key: The composite key "project_id:user_or_anon_id".
            context: Window context with timing information.
            elements: All event JSON strings in this session window.
        """
        if not elements:
            return

        events = []
        for elem in elements:
            try:
                events.append(json.loads(elem))
            except json.JSONDecodeError:
                continue

        if not events:
            return

        # Sort events by timestamp within the session
        events.sort(key=lambda e: e.get("timestamp", ""))

        # Parse the composite key
        key_parts = key.split(":", 1)
        project_id = key_parts[0] if len(key_parts) > 0 else ""
        user_or_anon = key_parts[1] if len(key_parts) > 1 else ""

        first_event = events[0]
        last_event = events[-1]

        # Determine user_id and anonymous_id
        user_id = ""
        anonymous_id = ""
        for evt in events:
            if evt.get("user_id"):
                user_id = evt["user_id"]
            if evt.get("anonymous_id"):
                anonymous_id = evt["anonymous_id"]

        # Compute session metrics
        session_id = first_event.get("session_id", "")
        start_time = first_event.get("timestamp", "")
        end_time = last_event.get("timestamp", "")
        event_count = len(events)

        # Count distinct page views
        pages = []
        for evt in events:
            page_url = evt.get("properties", {})
            if isinstance(page_url, str):
                try:
                    page_url = json.loads(page_url)
                except (json.JSONDecodeError, TypeError):
                    page_url = {}
            url = page_url.get("page_url", "")
            if url:
                pages.append(url)
        page_count = len(set(pages))

        entry_page = pages[0] if pages else ""
        exit_page = pages[-1] if pages else ""

        # Compute duration in milliseconds
        duration_ms = 0
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)
            duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        except (ValueError, TypeError):
            pass

        country = first_event.get("country", "")
        device_type = first_event.get("context", {}).get("device_type", "")

        # Yield the session row as a JSON string for downstream JDBC sink
        session = {
            "project_id": project_id,
            "session_id": session_id,
            "user_id": user_id,
            "anonymous_id": anonymous_id,
            "start_time": start_time,
            "end_time": end_time,
            "duration_ms": duration_ms,
            "event_count": event_count,
            "page_count": page_count,
            "entry_page": entry_page,
            "exit_page": exit_page,
            "country": country,
            "device_type": device_type,
        }

        yield json.dumps(session)


def extract_session_key(event_json: str) -> str:
    """Extract the session grouping key from an event.

    Groups by project_id and the best available user identifier
    (user_id preferred, falling back to anonymous_id). This ensures
    sessions are computed per-user within a project.

    Returns:
        Composite key string "project_id:user_or_anon_id".
    """
    try:
        event = json.loads(event_json)
    except json.JSONDecodeError:
        return ":unknown"

    project_id = event.get("project_id", "")
    user_id = event.get("user_id", "")
    anonymous_id = event.get("anonymous_id", "")
    identifier = user_id if user_id else anonymous_id
    if not identifier:
        identifier = "unknown"
    return f"{project_id}:{identifier}"


def build_pipeline():
    """Construct and execute the Flink sessionization pipeline.

    Pipeline topology:
        KafkaSource(events.enriched)
        -> key_by(project_id:user_id)
        -> EventTimeSessionWindows(30 min gap)
        -> SessionWindowFunction (aggregates events into sessions)
        -> JDBC Sink (ClickHouse sessions table)
    """
    env = StreamExecutionEnvironment.get_execution_environment()

    # Configure checkpointing
    env.enable_checkpointing(60_000)
    env.set_parallelism(int(os.environ.get("FLINK_PARALLELISM", "4")))

    # Allowed lateness: events arriving up to 5 minutes late are still
    # included in their session window.
    allowed_lateness_ms = 5 * 60 * 1000

    # --- Kafka Source ---
    kafka_source = (
        KafkaSource.builder()
        .set_bootstrap_servers(KAFKA_BROKERS)
        .set_topics(INPUT_TOPIC)
        .set_group_id(CONSUMER_GROUP)
        .set_starting_offsets(KafkaOffsetsInitializer.committed_offsets())
        .set_value_only_deserializer(SimpleStringSchema())
        .build()
    )

    # Watermark strategy: bounded out-of-orderness with 30-second tolerance.
    # Events may arrive slightly out of order from Kafka partitions, so we
    # allow a 30-second skew before advancing the watermark.
    watermark_strategy = (
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(30))
        .with_timestamp_assigner(EventTimestampAssigner())
    )

    # --- Pipeline ---
    events_stream = env.from_source(
        kafka_source,
        watermark_strategy,
        "events.enriched",
    )

    # Key by composite user identifier, apply session windows
    session_stream = (
        events_stream
        .key_by(extract_session_key)
        .window(EventTimeSessionWindows.with_gap(CommonTime.minutes(30)))
        .allowed_lateness(allowed_lateness_ms)
        .process(SessionWindowFunction())
        .name("sessionize")
    )

    # --- ClickHouse JDBC Sink ---
    # For production, use Flink's JDBC sink connector with the ClickHouse
    # JDBC driver. Here we use the Table API's JDBC connector for simplicity.
    from pyflink.datastream.connectors.jdbc import (
        JdbcSink,
        JdbcConnectionOptions,
        JdbcExecutionOptions,
    )

    jdbc_sink = JdbcSink.sink(
        "INSERT INTO sessions ("
        "project_id, session_id, user_id, anonymous_id, "
        "start_time, end_time, duration_ms, event_count, page_count, "
        "entry_page, exit_page, country, device_type"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        type_info=Types.STRING(),
        jdbc_connection_options=(
            JdbcConnectionOptions.JdbcConnectionOptionsBuilder()
            .with_url(CLICKHOUSE_URL)
            .with_driver_name("com.clickhouse.jdbc.ClickHouseDriver")
            .build()
        ),
        jdbc_execution_options=(
            JdbcExecutionOptions.builder()
            .with_batch_interval_ms(5_000)
            .with_batch_size(500)
            .with_max_retries(3)
            .build()
        ),
    )

    session_stream.add_sink(jdbc_sink).name("clickhouse-sessions")

    env.execute("sessionization")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_pipeline()
