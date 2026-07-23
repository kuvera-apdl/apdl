#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/scripts/fixtures/docker-compose.boundary-marker.yml"
PROJECT_NAME="apdl-boundary-marker-$$"
PYTHON_BIN="${BOUNDARY_MARKER_TEST_PYTHON:-$ROOT_DIR/pipeline/redis/.venv/bin/python}"

compose() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[ -x "$PYTHON_BIN" ] || {
    echo "Boundary marker test Python is unavailable: $PYTHON_BIN" >&2
    echo "Run 'make deps' or set BOUNDARY_MARKER_TEST_PYTHON." >&2
    exit 1
}

echo "==> Starting isolated Redis and PostgreSQL"
compose up -d --wait --wait-timeout 90 redis postgres >/dev/null

for migration in \
    038_experiment_data_completeness.sql \
    041_boundary_marker_retry_quarantine.sql
do
    compose exec -T \
        -e PGPASSWORD=apdl_dev \
        postgres \
        psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
        < "$ROOT_DIR/pipeline/postgres/migrations/$migration" \
        >/dev/null
done

migration_checksum="$(
    "$PYTHON_BIN" -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$ROOT_DIR/pipeline/postgres/migrations/041_boundary_marker_retry_quarantine.sql"
)"
compose exec -T \
    -e PGPASSWORD=apdl_dev \
    postgres \
    psql -X -v ON_ERROR_STOP=1 -U apdl -d apdl \
    -c "CREATE TABLE public.apdl_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum CHAR(64) NOT NULL
        );
        INSERT INTO public.apdl_schema_migrations (version, name, checksum)
        VALUES (
            41,
            '041_boundary_marker_retry_quarantine.sql',
            '$migration_checksum'
        );" \
    >/dev/null

redis_port="$(compose port redis 6379 | tail -n 1)"
redis_port="${redis_port##*:}"
postgres_port="$(compose port postgres 5432 | tail -n 1)"
postgres_port="${postgres_port##*:}"

"$PYTHON_BIN" "$ROOT_DIR/scripts/test_boundary_marker_fairness.py" \
    --redis-url "redis://127.0.0.1:$redis_port/0" \
    --postgres-url \
        "postgresql://apdl:apdl_dev@127.0.0.1:$postgres_port/apdl"
