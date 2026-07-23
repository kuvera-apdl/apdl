#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/scripts/fixtures/docker-compose.clickhouse-upgrade.yml"
LEGACY_SCHEMA="$ROOT_DIR/scripts/fixtures/clickhouse_pre_ledger_schema.sql"
PROJECT_NAME="apdl-clickhouse-upgrade-$$"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apdl-clickhouse-upgrade.XXXXXX")"
inhibitor_pid=""
credential_pid=""

compose() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
    if [ -n "$inhibitor_pid" ]; then
        kill "$inhibitor_pid" >/dev/null 2>&1 || true
        wait "$inhibitor_pid" 2>/dev/null || true
    fi
    if [ -n "$credential_pid" ]; then
        kill "$credential_pid" >/dev/null 2>&1 || true
        wait "$credential_pid" 2>/dev/null || true
    fi
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

query() {
    compose exec -T clickhouse clickhouse-client \
        --user apdl \
        --password apdl_dev \
        --database apdl \
        --format TSVRaw \
        --query "$1"
}

assert_equal() {
    local expected="$1"
    local actual="$2"
    local description="$3"
    if [ "$actual" != "$expected" ]; then
        echo "$description: expected '$expected', got '$actual'" >&2
        exit 1
    fi
}

assert_owner_absent() {
    assert_equal \
        '0' \
        "$(query "SELECT count() FROM system.tables WHERE database = 'apdl' AND name = 'apdl_active_maintenance'")" \
        "durable ClickHouse maintenance owner survived a handled run"
}

assert_gate_state() {
    local expected_blocked="$1"
    assert_equal \
        $'1\t'"$expected_blocked" \
        "$(query "SELECT count() = uniqExact(generation), argMax(writes_blocked, generation) FROM apdl_maintenance_gate WHERE authority = 'runtime-writes'")" \
        "ClickHouse runtime gate has a duplicate latest generation or wrong state"
}

run_migrations() {
    COMPOSE_PROJECT_NAME="$PROJECT_NAME" \
    CLICKHOUSE_COMPOSE_FILE="$COMPOSE_FILE" \
    CLICKHOUSE_USER=apdl \
    CLICKHOUSE_PASSWORD=apdl_dev \
    CLICKHOUSE_DB=apdl \
    CLICKHOUSE_MIGRATIONS_DIR="${1:-$ROOT_DIR/pipeline/clickhouse/migrations}" \
    CLICKHOUSE_BACKFILLS_DIR="$ROOT_DIR/pipeline/clickhouse/backfills" \
        "$ROOT_DIR/scripts/init-clickhouse.sh"
}

echo "==> Starting isolated pre-ledger ClickHouse"
compose up -d --wait --wait-timeout 90 clickhouse >/dev/null
compose exec -T clickhouse clickhouse-client \
    --user apdl \
    --password apdl_dev \
    --query "CREATE DATABASE IF NOT EXISTS apdl"
compose exec -T clickhouse clickhouse-client \
    --user apdl \
    --password apdl_dev \
    --database apdl \
    --multiquery < "$LEGACY_SCHEMA"

echo "==> Upgrading seeded legacy schema"
run_migrations

assert_owner_absent
assert_gate_state '0'
assert_equal \
    $'authority\tString\t1\t1\ngeneration\tUInt64\t0\t0\nwrites_blocked\tUInt8\t0\t0' \
    "$(query "SELECT name, type, is_in_primary_key, is_in_sorting_key FROM system.columns WHERE database = 'apdl' AND table = 'apdl_maintenance_gate' ORDER BY position")" \
    "runtime gate columns are not canonical"
assert_equal \
    $'ReplacingMergeTree\tauthority\tauthority' \
    "$(query "SELECT engine, sorting_key, primary_key FROM system.tables WHERE database = 'apdl' AND name = 'apdl_maintenance_gate'")" \
    "runtime gate engine is not canonical"
assert_equal \
    $'16\t16\t1\t16' \
    "$(query "SELECT count(), uniqExact(version), min(version), max(version) FROM apdl_schema_migrations FINAL")" \
    "migration ledger is not the exact contiguous release sequence"
assert_equal \
    $'ReplacingMergeTree\tproject_id, message_id' \
    "$(query "SELECT engine, sorting_key FROM system.tables WHERE database = 'apdl' AND name = 'events'")" \
    "events storage did not converge"
assert_equal \
    $'1\t11111111-1111-1111-1111-111111111111\ttrack' \
    "$(query "SELECT count(), any(message_id), any(event_type) FROM events FINAL WHERE event_id = toUUID('11111111-1111-1111-1111-111111111111')")" \
    "legacy event identity or data was not preserved"
assert_equal \
    $'String\t42' \
    "$(query "SELECT (SELECT type FROM system.columns WHERE database = 'apdl' AND table = 'sessions' AND name = 'project_id'), (SELECT any(project_id) FROM sessions WHERE session_id = 'legacy-session')")" \
    "legacy sessions project identity did not converge"
assert_equal \
    $'1\t1' \
    "$(query "SELECT (SELECT count() FROM feature_flag_exposures FINAL), (SELECT count() FROM frontend_health_events FINAL)")" \
    "derived projection backfill did not converge"
assert_equal \
    $'project_id\ttoDate(received_at)\tproject_id\ttoDate(received_at)' \
    "$(query "SELECT (SELECT partition_key FROM system.tables WHERE database = 'apdl' AND name = 'events'), (SELECT default_expression FROM system.columns WHERE database = 'apdl' AND table = 'events' AND name = 'event_date'), (SELECT partition_key FROM system.tables WHERE database = 'apdl' AND name = 'experiment_event_deliveries'), (SELECT default_expression FROM system.columns WHERE database = 'apdl' AND table = 'experiment_event_deliveries' AND name = 'event_date')")" \
    "event retention storage is not server-receipt authoritative"
assert_equal \
    '6' \
    "$(query "SELECT count() FROM system.tables WHERE database = 'apdl' AND name IN ('events', 'experiment_event_deliveries', 'feature_flag_exposures', 'frontend_health_events', 'sessions', 'identity_alias_assertions') AND partition_key = 'project_id' AND positionCaseInsensitive(create_table_query, 'TTL') > 0")" \
    "not every personal analytics table has project partitioning and TTL"
assert_equal \
    '4' \
    "$(query "SELECT count() FROM system.columns WHERE database = 'apdl' AND name = 'received_at' AND table IN ('feature_flag_exposures', 'frontend_health_events', 'sessions', 'identity_alias_assertions')")" \
    "a derived personal analytics table lacks receipt authority"
assert_equal \
    '0' \
    "$(query "SELECT count() FROM system.tables WHERE database = 'apdl' AND name IN ('identity_alias_resolution_state', 'identity_alias_resolution_state_mv')")" \
    "irreversible identity resolution state survived retention migration"
query "INSERT INTO events (
    project_id, message_id, event_type, event_name, user_id, anonymous_id,
    group_id, session_id, timestamp, received_at, properties, traits, context,
    ip, source_stream, source_stream_id, source_stream_id_ms,
    source_stream_id_seq
) VALUES (
    'demo', 'received-at-retention-probe', 'track', 'retention_probe', '',
    'retention-anon', '', 'retention-session', '2099-12-31 23:59:59.000',
    '2026-07-02 03:04:05.000', '{}', '{}', '{}', '',
    'events:raw:demo', '999-0', 999, 0
)" >/dev/null
assert_equal \
    $'2026-07-02\t2099-12-31\t2026-07-02\t2026-07-02\t2099-12-31\t2026-07-02' \
    "$(query "SELECT (SELECT toString(event_date) FROM events FINAL WHERE message_id = 'received-at-retention-probe'), (SELECT toString(toDate(timestamp)) FROM events FINAL WHERE message_id = 'received-at-retention-probe'), (SELECT toString(toDate(received_at)) FROM events FINAL WHERE message_id = 'received-at-retention-probe'), (SELECT toString(event_date) FROM experiment_event_deliveries FINAL WHERE message_id = 'received-at-retention-probe'), (SELECT toString(toDate(timestamp)) FROM experiment_event_deliveries FINAL WHERE message_id = 'received-at-retention-probe'), (SELECT toString(toDate(received_at)) FROM experiment_event_deliveries FINAL WHERE message_id = 'received-at-retention-probe')")" \
    "client event time still controls a retained event date"

echo "==> Verifying TTL removes every personal base and derived row"
query "INSERT INTO events (
    project_id, message_id, event_type, event_name, user_id, anonymous_id,
    group_id, session_id, timestamp, received_at, properties, traits, context,
    ip, source_stream, source_stream_id, source_stream_id_ms,
    source_stream_id_seq
) VALUES
(
    'ttlprobe', 'ttl-feature', 'track', '\$feature_flag_exposure', 'ttl-user',
    'ttl-anon', '', 'ttl-session', now64(3),
    toDateTime64('2000-01-01 00:00:00', 3),
    '{\"flag_key\":\"ttl\",\"variant\":\"on\",\"reason\":\"fallthrough\"}',
    '{}', '{}', '', 'events:raw:ttlprobe', '1-0', 1, 0
),
(
    'ttlprobe', 'ttl-frontend', 'track', '\$frontend_error', 'ttl-user',
    'ttl-anon', '', 'ttl-session', now64(3),
    toDateTime64('2000-01-01 00:00:00', 3),
    '{\"page\":\"/ttl\",\"error_type\":\"probe\"}', '{}', '{}', '',
    'events:raw:ttlprobe', '2-0', 2, 0
),
(
    'ttlprobe', 'ttl-identify', 'identify', '\$identify', 'ttl-user',
    'ttl-anon', '', 'ttl-session', now64(3),
    toDateTime64('2000-01-01 00:00:00', 3), '{}', '{}', '{}', '',
    'events:raw:ttlprobe', '3-0', 3, 0
)" >/dev/null
query "INSERT INTO sessions (
    project_id, session_id, user_id, anonymous_id, start_time, end_time,
    duration_ms, event_count, page_count, entry_page, exit_page, country,
    device_type, received_at
) VALUES (
    'ttlprobe', 'ttl-session', 'ttl-user', 'ttl-anon', now64(3), now64(3),
    0, 3, 1, '/ttl', '/ttl', '', 'desktop',
    toDateTime64('2000-01-01 00:00:00', 3)
)" >/dev/null
for table in \
    feature_flag_exposures \
    frontend_health_events \
    sessions \
    experiment_event_deliveries \
    events \
    identity_alias_assertions; do
    query "ALTER TABLE \`$table\` MATERIALIZE TTL SETTINGS mutations_sync = 2" \
        >/dev/null
done
assert_equal \
    $'0\t0\t0\t0\t0\t0' \
    "$(query "SELECT (SELECT count() FROM events FINAL WHERE project_id = 'ttlprobe'), (SELECT count() FROM feature_flag_exposures FINAL WHERE project_id = 'ttlprobe'), (SELECT count() FROM frontend_health_events FINAL WHERE project_id = 'ttlprobe'), (SELECT count() FROM sessions WHERE project_id = 'ttlprobe'), (SELECT count() FROM experiment_event_deliveries FINAL WHERE project_id = 'ttlprobe'), (SELECT count() FROM identity_alias_assertions FINAL WHERE project_id = 'ttlprobe')")" \
    "TTL left personal source or derived rows behind"
assert_equal \
    '0' \
    "$(query "SELECT count() FROM system.tables WHERE database = 'apdl' AND name IN ('events_v2', 'events_dlq_v2', 'decisions_v2', 'feeds_v2', 'flag_evaluations_v', 'experiment_exposures_v', 'agent_actions_v', 'personalizations_v')")" \
    "disconnected durable prototype objects survived the cutover"

echo "==> Verifying exact-once rerun"
rerun_output="$(run_migrations)"
if [[ "$rerun_output" != *"ClickHouse schema is already current"* ]]; then
    echo "migration rerun did not report an exact current ledger" >&2
    exit 1
fi
assert_equal \
    $'3\t1\t1\t1' \
    "$(query "SELECT (SELECT count() FROM events FINAL), (SELECT count() FROM feature_flag_exposures FINAL), (SELECT count() FROM frontend_health_events FINAL), (SELECT count() FROM experiment_event_deliveries FINAL)")" \
    "migration rerun changed canonical row counts"

echo "==> Verifying auditable project and user deletion"
compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    < "$ROOT_DIR/pipeline/postgres/migrations/040_analytics_data_deletion_audit.sql" \
    >/dev/null
compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    >/dev/null <<'SQL'
CREATE TABLE public.apdl_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO public.apdl_schema_migrations (version, name, checksum)
VALUES (
    40,
    '040_analytics_data_deletion_audit.sql',
    repeat('0', 64)
);
SQL
query "INSERT INTO events (
    project_id, message_id, event_type, event_name, user_id, anonymous_id,
    group_id, session_id, timestamp, received_at, properties, traits, context,
    ip, source_stream, source_stream_id, source_stream_id_ms,
    source_stream_id_seq
) VALUES
(
    'eraseuser', 'target-identify', 'identify', '\$identify', 'target-user',
    'target-anon', '', 'target-session', now64(3), now64(3), '{}', '{}', '{}',
    '', 'events:raw:eraseuser', '1001-0', 1001, 0
),
(
    'eraseuser', 'target-feature', 'track', '\$feature_flag_exposure',
    'target-user', 'target-anon', '', 'target-session', now64(3), now64(3),
    '{\"flag_key\":\"erase\",\"variant\":\"on\",\"reason\":\"fallthrough\"}',
    '{}', '{}', '', 'events:raw:eraseuser', '1002-0', 1002, 0
),
(
    'eraseuser', 'target-frontend', 'track', '\$frontend_error', 'target-user',
    'target-anon', '', 'target-session', now64(3), now64(3),
    '{\"page\":\"/erase\",\"error_type\":\"probe\"}', '{}', '{}', '',
    'events:raw:eraseuser', '1003-0', 1003, 0
),
(
    'eraseuser', 'control-identify', 'identify', '\$identify', 'control-user',
    'control-anon', '', 'control-session', now64(3), now64(3), '{}', '{}', '{}',
    '', 'events:raw:eraseuser', '2001-0', 2001, 0
),
(
    'eraseuser', 'control-feature', 'track', '\$feature_flag_exposure',
    'control-user', 'control-anon', '', 'control-session', now64(3), now64(3),
    '{\"flag_key\":\"control\",\"variant\":\"on\",\"reason\":\"fallthrough\"}',
    '{}', '{}', '', 'events:raw:eraseuser', '2002-0', 2002, 0
),
(
    'eraseuser', 'control-frontend', 'track', '\$frontend_error', 'control-user',
    'control-anon', '', 'control-session', now64(3), now64(3),
    '{\"page\":\"/control\",\"error_type\":\"probe\"}', '{}', '{}', '',
    'events:raw:eraseuser', '2003-0', 2003, 0
),
(
    'eraseproject', 'project-identify', 'identify', '\$identify', 'project-user',
    'project-anon', '', 'project-session', now64(3), now64(3), '{}', '{}', '{}',
    '', 'events:raw:eraseproject', '3001-0', 3001, 0
),
(
    'eraseproject', 'project-feature', 'track', '\$feature_flag_exposure',
    'project-user', 'project-anon', '', 'project-session', now64(3), now64(3),
    '{\"flag_key\":\"project\",\"variant\":\"on\",\"reason\":\"fallthrough\"}',
    '{}', '{}', '', 'events:raw:eraseproject', '3002-0', 3002, 0
),
(
    'eraseproject', 'project-frontend', 'track', '\$frontend_error',
    'project-user', 'project-anon', '', 'project-session', now64(3), now64(3),
    '{\"page\":\"/project\",\"error_type\":\"probe\"}', '{}', '{}', '',
    'events:raw:eraseproject', '3003-0', 3003, 0
)" >/dev/null
query "INSERT INTO sessions (
    project_id, session_id, user_id, anonymous_id, start_time, end_time,
    duration_ms, event_count, page_count, entry_page, exit_page, country,
    device_type, received_at
) VALUES
(
    'eraseuser', 'target-session', 'target-user', 'target-anon', now64(3),
    now64(3), 0, 3, 1, '/', '/', '', 'desktop', now64(3)
),
(
    'eraseuser', 'control-session', 'control-user', 'control-anon', now64(3),
    now64(3), 0, 3, 1, '/', '/', '', 'desktop', now64(3)
),
(
    'eraseproject', 'project-session', 'project-user', 'project-anon', now64(3),
    now64(3), 0, 3, 1, '/', '/', '', 'desktop', now64(3)
)" >/dev/null
assert_equal \
    $'3\t1\t1\t1\t3\t1' \
    "$(query "SELECT (SELECT count() FROM events FINAL WHERE project_id = 'eraseuser' AND user_id = 'target-user'), (SELECT count() FROM feature_flag_exposures FINAL WHERE project_id = 'eraseuser' AND user_id = 'target-user'), (SELECT count() FROM frontend_health_events FINAL WHERE project_id = 'eraseuser' AND user_id = 'target-user'), (SELECT count() FROM sessions WHERE project_id = 'eraseuser' AND user_id = 'target-user'), (SELECT count() FROM experiment_event_deliveries FINAL WHERE project_id = 'eraseuser' AND user_id = 'target-user'), (SELECT count() FROM identity_alias_assertions FINAL WHERE project_id = 'eraseuser' AND user_id = 'target-user')")" \
    "user deletion fixture did not populate every target table"

user_delete_output="$(
    COMPOSE_PROJECT_NAME="$PROJECT_NAME" \
    CLICKHOUSE_COMPOSE_FILE="$COMPOSE_FILE" \
    "$ROOT_DIR/scripts/delete-analytics-data.sh" user \
        --request-id 11111111-1111-4111-8111-111111111111 \
        --project-id eraseuser \
        --user-id target-user \
        --actor privacy@example.test \
        --reason "verified user erasure request"
)"
if [[ "$user_delete_output" != *'"status":"completed"'* ]]; then
    echo "user deletion did not report completion: $user_delete_output" >&2
    exit 1
fi
assert_equal \
    $'0\t0\t0\t0\t0\t0\t0' \
    "$(query "SELECT (SELECT count() FROM events FINAL WHERE project_id = 'eraseuser' AND (user_id = 'target-user' OR anonymous_id = 'target-anon')), (SELECT count() FROM feature_flag_exposures FINAL WHERE project_id = 'eraseuser' AND (user_id = 'target-user' OR anonymous_id = 'target-anon')), (SELECT count() FROM frontend_health_events FINAL WHERE project_id = 'eraseuser' AND (user_id = 'target-user' OR anonymous_id = 'target-anon')), (SELECT count() FROM sessions WHERE project_id = 'eraseuser' AND (user_id = 'target-user' OR anonymous_id = 'target-anon')), (SELECT count() FROM experiment_event_deliveries FINAL WHERE project_id = 'eraseuser' AND (user_id = 'target-user' OR anonymous_id = 'target-anon')), (SELECT count() FROM identity_alias_assertions FINAL WHERE project_id = 'eraseuser' AND (user_id = 'target-user' OR anonymous_id = 'target-anon')), (SELECT count() FROM resolved_identity_aliases WHERE project_id = 'eraseuser' AND anonymous_id = 'target-anon')")" \
    "user deletion left personal source, derived, or identity rows behind"
assert_equal \
    $'3\t1\t1\t1\t3\t1' \
    "$(query "SELECT (SELECT count() FROM events FINAL WHERE project_id = 'eraseuser' AND user_id = 'control-user'), (SELECT count() FROM feature_flag_exposures FINAL WHERE project_id = 'eraseuser' AND user_id = 'control-user'), (SELECT count() FROM frontend_health_events FINAL WHERE project_id = 'eraseuser' AND user_id = 'control-user'), (SELECT count() FROM sessions WHERE project_id = 'eraseuser' AND user_id = 'control-user'), (SELECT count() FROM experiment_event_deliveries FINAL WHERE project_id = 'eraseuser' AND user_id = 'control-user'), (SELECT count() FROM identity_alias_assertions FINAL WHERE project_id = 'eraseuser' AND user_id = 'control-user')")" \
    "user deletion crossed the requested identity boundary"
user_retry_output="$(
    COMPOSE_PROJECT_NAME="$PROJECT_NAME" \
    CLICKHOUSE_COMPOSE_FILE="$COMPOSE_FILE" \
    "$ROOT_DIR/scripts/delete-analytics-data.sh" user \
        --request-id 11111111-1111-4111-8111-111111111111 \
        --project-id eraseuser \
        --user-id target-user \
        --actor privacy@example.test \
        --reason "verified user erasure request"
)"
if [[ "$user_retry_output" != *'"status":"already_completed"'* ]]; then
    echo "completed user deletion was not idempotent: $user_retry_output" >&2
    exit 1
fi

project_delete_output="$(
    COMPOSE_PROJECT_NAME="$PROJECT_NAME" \
    CLICKHOUSE_COMPOSE_FILE="$COMPOSE_FILE" \
    "$ROOT_DIR/scripts/delete-analytics-data.sh" project \
        --request-id 22222222-2222-4222-8222-222222222222 \
        --project-id eraseproject \
        --actor privacy@example.test \
        --reason "approved project erasure request"
)"
if [[ "$project_delete_output" != *'"status":"completed"'* ]]; then
    echo "project deletion did not report completion: $project_delete_output" >&2
    exit 1
fi
assert_equal \
    $'0\t0\t0\t0\t0\t0' \
    "$(query "SELECT (SELECT count() FROM events FINAL WHERE project_id = 'eraseproject'), (SELECT count() FROM feature_flag_exposures FINAL WHERE project_id = 'eraseproject'), (SELECT count() FROM frontend_health_events FINAL WHERE project_id = 'eraseproject'), (SELECT count() FROM sessions WHERE project_id = 'eraseproject'), (SELECT count() FROM experiment_event_deliveries FINAL WHERE project_id = 'eraseproject'), (SELECT count() FROM identity_alias_assertions FINAL WHERE project_id = 'eraseproject')")" \
    "project deletion left personal source or derived rows behind"

assert_equal \
    '4|2|2|0' \
    "$(compose exec -T postgres psql -X -A -t -U apdl -d apdl -c "SELECT count(*), count(*) FILTER (WHERE event_type = 'requested'), count(*) FILTER (WHERE event_type = 'completed'), count(*) FILTER (WHERE row_to_json(analytics_data_deletion_audit)::text LIKE '%target-user%') FROM analytics_data_deletion_audit")" \
    "deletion audit is incomplete or retained a raw user target"
if compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    -c "UPDATE analytics_data_deletion_audit SET actor = 'rewritten' WHERE request_id = '11111111-1111-4111-8111-111111111111'" \
    >"$WORK_DIR/audit-update.log" 2>&1; then
    echo "deletion audit accepted an update" >&2
    exit 1
fi
if ! grep -q "analytics data deletion audit records are immutable" \
    "$WORK_DIR/audit-update.log"; then
    echo "deletion audit update did not fail through the immutable trigger" >&2
    sed -n '1,120p' "$WORK_DIR/audit-update.log" >&2
    exit 1
fi

echo "==> Verifying a remote runtime inhibitor blocks migration"
compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    -c "SELECT pg_advisory_lock_shared(4158044083); SELECT pg_advisory_lock_shared(4158044084); SELECT pg_sleep(5);" \
    >/dev/null 2>&1 &
inhibitor_pid="$!"
for _ in $(seq 1 30); do
    if [ "$(compose exec -T postgres psql -X -A -t -U apdl -d apdl \
        -c "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND mode = 'ShareLock' AND granted")" != "0" ]; then
        break
    fi
    sleep 0.1
done
if APDL_MAINTENANCE_DRAIN_TIMEOUT_SECONDS=1 run_migrations \
    >"$WORK_DIR/maintenance-fence.log" 2>&1; then
    echo "migration ran while a remote runtime held the shared inhibitor" >&2
    exit 1
fi
if ! grep -q "exclusive APDL maintenance inhibitor" \
    "$WORK_DIR/maintenance-fence.log"; then
    echo "blocked migration did not report the maintenance inhibitor" >&2
    sed -n '1,160p' "$WORK_DIR/maintenance-fence.log" >&2
    exit 1
fi
wait "$inhibitor_pid"
inhibitor_pid=""

echo "==> Verifying development credential provisioning joins the shared fence"
compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl <<'SQL'
CREATE TABLE auth_credentials (
    credential_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    credential_kind TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    roles TEXT[] NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
SQL
compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    -c "SELECT pg_advisory_lock(4158044084); SELECT pg_sleep(3);" \
    >/dev/null 2>&1 &
inhibitor_pid="$!"
for _ in $(seq 1 30); do
    if [ "$(compose exec -T postgres psql -X -A -t -U apdl -d apdl \
        -c "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND mode = 'ExclusiveLock' AND granted")" != "0" ]; then
        break
    fi
    sleep 0.1
done
compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    -v credential_id=maintenance-probe \
    -v project_id=demo \
    -v credential_kind=confidential \
    -v key_prefix=proj_demo_ \
    -v key_hash=maintenance-probe-hash \
    -v roles='{events:write}' \
    -v maintenance_inhibitor_lock_id=4158044083 \
    -v maintenance_guard_lock_id=4158044084 \
    < "$ROOT_DIR/scripts/provision-dev-credential.sql" >/dev/null &
credential_pid="$!"
for _ in $(seq 1 30); do
    if [ "$(compose exec -T postgres psql -X -A -t -U apdl -d apdl \
        -c "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND mode = 'ShareLock' AND NOT granted")" != "0" ]; then
        break
    fi
    sleep 0.1
done
assert_equal \
    '0' \
    "$(compose exec -T postgres psql -X -A -t -U apdl -d apdl \
        -c "SELECT count(*) FROM auth_credentials WHERE credential_id = 'maintenance-probe'")" \
    "development credential mutation bypassed the exclusive maintenance fence"
wait "$inhibitor_pid"
inhibitor_pid=""
wait "$credential_pid"
credential_pid=""
assert_equal \
    '1' \
    "$(compose exec -T postgres psql -X -A -t -U apdl -d apdl \
        -c "SELECT count(*) FROM auth_credentials WHERE credential_id = 'maintenance-probe'")" \
    "development credential mutation did not resume after maintenance"

echo "==> Verifying checksum drift fails closed"
cp -R "$ROOT_DIR/pipeline/clickhouse/migrations" "$WORK_DIR/migrations"
printf '\n-- checksum drift injected by upgrade test\n' \
    >> "$WORK_DIR/migrations/001_events.sql"
if run_migrations "$WORK_DIR/migrations" >"$WORK_DIR/drift.log" 2>&1; then
    echo "modified applied migration was accepted" >&2
    exit 1
fi
if ! grep -q "checksum drift" "$WORK_DIR/drift.log"; then
    echo "modified applied migration did not fail with checksum drift" >&2
    sed -n '1,160p' "$WORK_DIR/drift.log" >&2
    exit 1
fi
assert_owner_absent
assert_gate_state '1'

echo "==> Verifying a clean rerun reopens the failed-closed runtime gate"
run_migrations >/dev/null
assert_owner_absent
assert_gate_state '0'

echo "==> ClickHouse pre-ledger upgrade smoke passed"
