#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/scripts/fixtures/docker-compose.clickhouse-upgrade.yml"
LEGACY_SCHEMA="$ROOT_DIR/scripts/fixtures/clickhouse_pre_ledger_schema.sql"
PROJECT_NAME="apdl-clickhouse-upgrade-$$"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apdl-clickhouse-upgrade.XXXXXX")"

compose() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
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

assert_equal \
    $'11\t11\t1\t11' \
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

echo "==> ClickHouse pre-ledger upgrade smoke passed"
