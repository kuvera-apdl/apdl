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
    $'12\t12\t1\t12' \
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
    $'2\t1\t1' \
    "$(query "SELECT (SELECT count() FROM events FINAL), (SELECT count() FROM feature_flag_exposures FINAL), (SELECT count() FROM frontend_health_events FINAL)")" \
    "migration rerun changed canonical row counts"

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
