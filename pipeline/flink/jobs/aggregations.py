"""
Flink real-time aggregation job (Phase 3+).

Reads enriched events from the events.enriched Kafka topic and computes
real-time metrics using tumbling windows:
  - Event counts per project/event_name in 1-minute windows
  - Unique user counts per project/event_name in 1-minute windows
  - Revenue sums per project/event_name in 1-minute windows

Aggregated results are written to ClickHouse aggregation tables via JDBC.

Requires:
  - PyFlink 1.18+
  - Kafka brokers at KAFKA_BROKERS
  - ClickHouse JDBC connector jar
"""
import json
import logging
import os
from datetime import datetime

from pyflink.common import Duration, Row, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import StreamExecutionEnvironment, OutputTag
from pyflink.datastream.connectors.kafka import (
    KafkaOffsetsInitializer,
    KafkaSource,
)
from pyflink.datastream.functions import (
    AggregateFunction,
    ProcessWindowFunction,
    RuntimeContext,
)
from pyflink.datastream.window import TumblingEventTimeWindows, Time

logger = logging.getLogger(__name__)

KAFKA_BROKERS = os.environ.get("KAFKA_BROKERS", "localhost:9092")
CLICKHOUSE_URL = os.environ.get(
    "CLICKHOUSE_JDBC_URL",
    "jdbc:clickhouse://localhost:8123/apdl",
)
INPUT_TOPIC = "events.enriched"
CONSUMER_GROUP = "flink-aggregations"


class EventTimestampAssigner(TimestampAssigner):
    """Extracts event timestamps for Flink event-time processing."""

    def extract_timestamp(self, value: str, record_timestamp: int) -> int:
        try:
            event = json.loads(value)
            ts_str = event.get("timestamp", "")
            if ts_str:
                dt = datetime.fromisoformat(ts_str)
                return int(dt.timestamp() * 1000)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return record_timestamp


class EventCountAccumulator:
    """Mutable accumulator for the EventCountAggregateFunction.

    Tracks event count, distinct user set, and revenue sum within
    an aggregation window.
    """

    __slots__ = ("event_count", "user_ids", "revenue_sum")

    def __init__(self):
        self.event_count: int = 0
        self.user_ids: set[str] = set()
        self.revenue_sum: float = 0.0


class EventCountAggregateFunction(AggregateFunction):
    """Incrementally aggregates event metrics within a tumbling window.

    Uses an EventCountAccumulator to track counts, unique users, and
    revenue without holding all events in memory.
    """

    def create_accumulator(self) -> EventCountAccumulator:
        return EventCountAccumulator()

    def add(self, value: str, accumulator: EventCountAccumulator) -> EventCountAccumulator:
        """Add a single event to the accumulator.

        Args:
            value: JSON-encoded enriched event string.
            accumulator: Current aggregation state.
        """
        try:
            event = json.loads(value)
        except json.JSONDecodeError:
            return accumulator

        accumulator.event_count += 1

        user_id = event.get("user_id", "")
        if user_id:
            accumulator.user_ids.add(user_id)

        # Extract revenue from properties
        properties = event.get("properties", {})
        if isinstance(properties, str):
            try:
                properties = json.loads(properties)
            except (json.JSONDecodeError, TypeError):
                properties = {}
        revenue = properties.get("revenue")
        if revenue is not None:
            try:
                accumulator.revenue_sum += float(revenue)
            except (ValueError, TypeError):
                pass

        return accumulator

    def get_result(self, accumulator: EventCountAccumulator) -> dict:
        """Return the final aggregation result."""
        return {
            "event_count": accumulator.event_count,
            "unique_users": len(accumulator.user_ids),
            "revenue_sum": accumulator.revenue_sum,
        }

    def merge(
        self,
        a: EventCountAccumulator,
        b: EventCountAccumulator,
    ) -> EventCountAccumulator:
        """Merge two accumulators (used for session window merging)."""
        a.event_count += b.event_count
        a.user_ids.update(b.user_ids)
        a.revenue_sum += b.revenue_sum
        return a


class AggregationWindowFunction(ProcessWindowFunction):
    """Emits aggregated metrics with window timing metadata.

    Called after the EventCountAggregateFunction produces a result for
    each window. Attaches the window start/end times and the grouping
    key (project_id, event_name) to the output.
    """

    def process(
        self,
        key: str,
        context: ProcessWindowFunction.Context,
        elements,
    ):
        """Emit one aggregated row per window per key.

        Args:
            key: Composite key "project_id:event_name".
            context: Window context with start/end times.
            elements: Iterable of aggregation results (exactly one from
                      the preceding AggregateFunction).
        """
        key_parts = key.split(":", 1)
        project_id = key_parts[0] if key_parts else ""
        event_name = key_parts[1] if len(key_parts) > 1 else ""

        window = context.window()
        window_start = datetime.utcfromtimestamp(
            window.start / 1000
        ).isoformat()
        window_end = datetime.utcfromtimestamp(
            window.end / 1000
        ).isoformat()

        for agg_result in elements:
            output = {
                "project_id": project_id,
                "event_name": event_name,
                "window_start": window_start,
                "window_end": window_end,
                "event_count": agg_result["event_count"],
                "unique_users": agg_result["unique_users"],
                "revenue_sum": agg_result["revenue_sum"],
            }
            yield json.dumps(output)


def extract_aggregation_key(event_json: str) -> str:
    """Extract the aggregation grouping key from an event.

    Groups by (project_id, event_name) so that we get per-event-type
    counts within each project.

    Returns:
        Composite key string "project_id:event_name".
    """
    try:
        event = json.loads(event_json)
    except json.JSONDecodeError:
        return ":unknown"

    project_id = event.get("project_id", "")
    event_name = event.get("event", "")
    return f"{project_id}:{event_name}"


def build_pipeline():
    """Construct and execute the Flink aggregation pipeline.

    Pipeline topology:
        KafkaSource(events.enriched)
        -> key_by(project_id:event_name)
        -> TumblingEventTimeWindows(1 minute)
        -> aggregate(EventCountAggregateFunction, AggregationWindowFunction)
        -> JDBC Sink (ClickHouse event_counts_realtime table)
    """
    env = StreamExecutionEnvironment.get_execution_environment()

    # Configure checkpointing for fault tolerance
    env.enable_checkpointing(60_000)
    env.set_parallelism(int(os.environ.get("FLINK_PARALLELISM", "4")))

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

    # Watermark strategy: allow 30 seconds of out-of-orderness
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

    # 1-minute tumbling window aggregation
    aggregated_stream = (
        events_stream
        .key_by(extract_aggregation_key)
        .window(TumblingEventTimeWindows.of(Time.minutes(1)))
        .aggregate(
            EventCountAggregateFunction(),
            AggregationWindowFunction(),
        )
        .name("aggregate-event-counts")
    )

    # --- ClickHouse JDBC Sink ---
    from pyflink.datastream.connectors.jdbc import (
        JdbcSink,
        JdbcConnectionOptions,
        JdbcExecutionOptions,
    )

    jdbc_sink = JdbcSink.sink(
        "INSERT INTO event_counts_realtime ("
        "project_id, event_name, window_start, window_end, "
        "event_count, unique_users"
        ") VALUES (?, ?, ?, ?, ?, ?)",
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
            .with_batch_size(200)
            .with_max_retries(3)
            .build()
        ),
    )

    aggregated_stream.add_sink(jdbc_sink).name("clickhouse-event-counts")

    # --- Also output hourly rollups ---
    # 1-hour tumbling window for the hourly aggregation table
    hourly_stream = (
        events_stream
        .key_by(extract_aggregation_key)
        .window(TumblingEventTimeWindows.of(Time.hours(1)))
        .aggregate(
            EventCountAggregateFunction(),
            AggregationWindowFunction(),
        )
        .name("aggregate-hourly-counts")
    )

    hourly_jdbc_sink = JdbcSink.sink(
        "INSERT INTO event_counts_hourly ("
        "project_id, event_name, event_hour, "
        "event_count, unique_users"
        ") VALUES (?, ?, ?, ?, ?)",
        type_info=Types.STRING(),
        jdbc_connection_options=(
            JdbcConnectionOptions.JdbcConnectionOptionsBuilder()
            .with_url(CLICKHOUSE_URL)
            .with_driver_name("com.clickhouse.jdbc.ClickHouseDriver")
            .build()
        ),
        jdbc_execution_options=(
            JdbcExecutionOptions.builder()
            .with_batch_interval_ms(10_000)
            .with_batch_size(100)
            .with_max_retries(3)
            .build()
        ),
    )

    hourly_stream.add_sink(hourly_jdbc_sink).name("clickhouse-hourly-counts")

    env.execute("realtime-aggregations")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_pipeline()
