#!/usr/bin/env bash
# APDL master dev script — one entry point for local setup, running, and testing.
#
#   scripts/dev.sh setup      Full local setup (venvs, npm, infra, migrations, .env)
#   scripts/dev.sh up         Start infra deps (Redis, ClickHouse, PostgreSQL) + migrate
#   scripts/dev.sh up-full    Start the full stack in Docker (detached) + migrate
#   scripts/dev.sh status     Container status + service health endpoints
#   scripts/dev.sh smoke      End-to-end smoke test against the running stack
#   scripts/dev.sh test       All tests           (make test)
#   scripts/dev.sh lint       All linters         (make lint)
#   scripts/dev.sh check      Parallel lint+test  (make check)
#   scripts/dev.sh logs [svc] Tail Docker logs
#   scripts/dev.sh down       Stop all containers
#   scripts/dev.sh reset      Stop containers and DELETE volumes (asks first)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_VERSION="3.12"
DEPS_COMPOSE="$ROOT_DIR/infra/docker/docker-compose.deps.yml"
FULL_COMPOSE="$ROOT_DIR/infra/docker/docker-compose.yml"
SMOKE_API_KEY="${APDL_SMOKE_API_KEY:-proj_demo_0123456789abcdef}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
die()  { echo -e "${RED}  ✗${NC} $*"; exit 1; }

# ── helpers ──────────────────────────────────────────────────────────

require() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found. $2"
}

# wait_healthy <compose-file> <min-healthy>
wait_healthy() {
    local compose="$1" want="$2" healthy=0
    echo -n "  Waiting for containers to be healthy"
    for _ in $(seq 1 60); do
        healthy=$(docker compose -f "$compose" ps --format json 2>/dev/null | grep -c '"healthy"' || true)
        if [ "$healthy" -ge "$want" ] 2>/dev/null; then
            echo ""
            ok "$healthy containers healthy"
            return 0
        fi
        echo -n "."
        sleep 2
    done
    echo ""
    warn "Timed out waiting for health checks ($healthy/$want healthy) — continuing anyway"
}

# http_code <method> <url> [json-body]
http_code() {
    local method="$1" url="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -s -o /tmp/apdl-smoke-body -w '%{http_code}' -X "$method" "$url" \
            -H "x-api-key: $SMOKE_API_KEY" -H 'Content-Type: application/json' \
            -d "$body" --max-time 10 || echo "000"
    else
        curl -s -o /tmp/apdl-smoke-body -w '%{http_code}' -X "$method" "$url" \
            -H "x-api-key: $SMOKE_API_KEY" --max-time 10 || echo "000"
    fi
}

# ── setup ────────────────────────────────────────────────────────────

setup_python_package() {
    local name="$1" dir="$2"
    info "Setting up $name"
    [ -d "$dir" ] || { warn "Directory $dir not found, skipping"; return; }
    cd "$dir"
    if [ ! -d ".venv" ]; then
        uv venv --python "$PYTHON_VERSION" .venv
        ok "Created virtualenv"
    else
        ok "Virtualenv already exists"
    fi
    if [ -f "pyproject.toml" ]; then
        uv pip install -e ".[dev]" --python .venv/bin/python --quiet
        ok "Installed dependencies from pyproject.toml"
    elif [ -f "requirements.txt" ]; then
        uv pip install -r requirements.txt --python .venv/bin/python --quiet
        ok "Installed dependencies from requirements.txt"
    fi
    cd "$ROOT_DIR"
}

cmd_setup() {
    info "Checking prerequisites"
    require uv     "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    require node   "Install Node.js >= 20"
    require docker "Install Docker Desktop"
    require curl   "Install curl"
    ok "uv $(uv --version 2>&1 | head -1)"
    ok "node $(node --version)"
    ok "docker $(docker --version | cut -d' ' -f3 | tr -d ',')"

    if [ ! -f "$ROOT_DIR/.env" ] && [ -f "$ROOT_DIR/.env.example" ]; then
        cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
        ok "Created .env from .env.example"
        warn "Review .env and add LLM API keys before running the agents service"
    else
        ok ".env already exists"
    fi

    setup_python_package "Ingestion Service" "$ROOT_DIR/services/ingestion"
    setup_python_package "Config Service"    "$ROOT_DIR/services/config"
    setup_python_package "Query Service"     "$ROOT_DIR/services/query"
    setup_python_package "Agents Service"    "$ROOT_DIR/services/agents"
    setup_python_package "Pipeline Writer"   "$ROOT_DIR/pipeline/redis"
    setup_python_package "ETL Framework"     "$ROOT_DIR/pipeline/etl"
    setup_python_package "Python SDK"        "$ROOT_DIR/sdk/python"

    info "Setting up JavaScript SDK"
    (cd "$ROOT_DIR/sdk/javascript" && npm install --silent)
    ok "Installed npm dependencies"

    cmd_up

    echo ""
    info "Setup complete! Common next steps:"
    echo ""
    echo "  scripts/dev.sh up-full     Run the whole stack in Docker"
    echo "  scripts/dev.sh smoke       End-to-end smoke test"
    echo "  scripts/dev.sh check       Lint + test every package in parallel"
    echo ""
    echo "  make run-ingestion / run-config / run-query / run-agents / run-pipeline"
    echo "                             Run one service locally with hot-reload"
    echo ""
}

# ── stack lifecycle ──────────────────────────────────────────────────

cmd_up() {
    # If the full stack is already running, reuse its compose file so we
    # don't recreate Redis/ClickHouse underneath the application services.
    local compose="$DEPS_COMPOSE"
    if [ -n "$(docker compose -f "$FULL_COMPOSE" ps -q ingestion 2>/dev/null)" ]; then
        compose="$FULL_COMPOSE"
        info "Full stack detected — starting infrastructure via the full compose file"
    else
        info "Starting infrastructure (Redis, ClickHouse, PostgreSQL)"
    fi
    docker compose -f "$compose" up -d redis clickhouse postgres
    wait_healthy "$compose" 3
    CLICKHOUSE_COMPOSE_FILE="$compose" "$ROOT_DIR/scripts/init-clickhouse.sh"
    ok "ClickHouse schema initialized"
}

cmd_up_full() {
    info "Starting full stack in Docker (detached)"
    docker compose -f "$FULL_COMPOSE" up -d --build redis clickhouse postgres
    wait_healthy "$FULL_COMPOSE" 3
    CLICKHOUSE_COMPOSE_FILE="$FULL_COMPOSE" "$ROOT_DIR/scripts/init-clickhouse.sh"
    ok "ClickHouse schema initialized"
    docker compose -f "$FULL_COMPOSE" up -d --build ingestion config query agents clickhouse-writer
    ok "Application services starting"
    sleep 3
    cmd_status
}

cmd_down() {
    docker compose -f "$FULL_COMPOSE" down
    docker compose -f "$DEPS_COMPOSE" down
    ok "All containers stopped"
}

cmd_reset() {
    if [ "${1:-}" != "--yes" ]; then
        echo -e "${YELLOW}This deletes all local data volumes (events, flags, agent memory).${NC}"
        read -r -p "Type 'yes' to continue: " answer
        [ "$answer" = "yes" ] || die "Aborted"
    fi
    docker compose -f "$FULL_COMPOSE" down -v
    docker compose -f "$DEPS_COMPOSE" down -v
    ok "Containers stopped and volumes removed"
}

cmd_logs() {
    docker compose -f "$FULL_COMPOSE" logs -f --tail=100 "$@"
}

# ── status & smoke ───────────────────────────────────────────────────

check_health() {
    local name="$1" url="$2" optional="${3:-}"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || echo "000")
    if [ "$code" = "200" ]; then
        ok "$name ($url)"
        return 0
    elif [ -n "$optional" ]; then
        warn "$name not responding ($url) — optional, skipping"
        return 1
    else
        echo -e "${RED}  ✗${NC} $name not healthy ($url → $code)"
        return 1
    fi
}

cmd_status() {
    info "Containers"
    docker compose -f "$FULL_COMPOSE" ps 2>/dev/null || true
    docker compose -f "$DEPS_COMPOSE" ps 2>/dev/null || true
    echo ""
    info "Service health"
    local failures=0
    check_health "Ingestion" "http://localhost:8080/health" || failures=$((failures+1))
    check_health "Config"    "http://localhost:8081/health" || failures=$((failures+1))
    check_health "Query"     "http://localhost:8082/health" || failures=$((failures+1))
    check_health "Agents"    "http://localhost:8083/health" optional || true
    [ "$failures" -eq 0 ] || warn "$failures service(s) unhealthy — are they running? (scripts/dev.sh up-full)"
}

cmd_smoke() {
    info "Smoke test against http://localhost:{8080,8081,8082} (api key: $SMOKE_API_KEY)"
    local failures=0 code flag_key
    flag_key="smoke-test-$$"

    check_health "Ingestion" "http://localhost:8080/health" || die "Start the stack first: scripts/dev.sh up-full"
    check_health "Config"    "http://localhost:8081/health" || die "Config service is not running"
    check_health "Query"     "http://localhost:8082/health" || die "Query service is not running"
    check_health "Agents"    "http://localhost:8083/health" optional || true

    info "Ingesting an event batch"
    code=$(http_code POST "http://localhost:8080/v1/events" \
        '{"events":[{"event":"smoke_test","user_id":"u_smoke","properties":{"source":"dev.sh"}}]}')
    if [ "$code" = "202" ]; then ok "POST /v1/events → 202"; else
        echo -e "${RED}  ✗${NC} POST /v1/events → $code ($(cat /tmp/apdl-smoke-body))"; failures=$((failures+1)); fi

    info "Creating flag '$flag_key'"
    code=$(http_code POST "http://localhost:8081/v1/admin/flags" \
        "{\"key\":\"$flag_key\",\"name\":\"Smoke test flag\",\"state\":\"active\",\"enabled\":true,
          \"owners\":[\"dev@localhost\"],
          \"fallthrough\":{\"value\":true,\"rollout\":{\"percentage\":100,\"bucket_by\":\"user_id\"}}}")
    if [ "$code" = "201" ]; then ok "POST /v1/admin/flags → 201"; else
        echo -e "${RED}  ✗${NC} POST /v1/admin/flags → $code ($(cat /tmp/apdl-smoke-body))"; failures=$((failures+1)); fi

    info "Fetching SDK flag config"
    code=$(http_code GET "http://localhost:8081/v1/flags")
    if [ "$code" = "200" ] && grep -q "$flag_key" /tmp/apdl-smoke-body; then
        ok "GET /v1/flags → 200, contains '$flag_key'"
    else
        echo -e "${RED}  ✗${NC} GET /v1/flags → $code (flag missing from payload?)"; failures=$((failures+1))
    fi

    info "Waiting for the event to land in ClickHouse (writer flushes every 5s)"
    local count_body landed=0 attempt
    count_body="{\"project_id\":\"demo\",\"start_date\":\"$(date -u +%Y-%m-%d)\",\"end_date\":\"$(date -u +%Y-%m-%d)\",
                 \"selectors\":[{\"event_name\":\"smoke_test\"}]}"
    for attempt in $(seq 1 12); do
        code=$(http_code POST "http://localhost:8082/v1/query/events/count" "$count_body")
        if [ "$code" != "200" ]; then
            echo -e "${RED}  ✗${NC} POST /v1/query/events/count → $code ($(cat /tmp/apdl-smoke-body))"
            failures=$((failures+1)); break
        fi
        total=$(grep -o '"total_events":[0-9]*' /tmp/apdl-smoke-body | cut -d: -f2)
        if [ "${total:-0}" -ge 1 ]; then
            ok "POST /v1/query/events/count → 200, total_events=$total"
            landed=1; break
        fi
        # On a brand-new stream the writer's consumer group is created at '$'
        # after the first event, skipping it — re-send once halfway through.
        if [ "$attempt" = "5" ]; then
            http_code POST "http://localhost:8080/v1/events" \
                '{"events":[{"event":"smoke_test","user_id":"u_smoke","properties":{"source":"dev.sh","retry":true}}]}' >/dev/null
        fi
        sleep 2
    done
    if [ "$code" = "200" ] && [ "$landed" != "1" ]; then
        echo -e "${RED}  ✗${NC} Event never appeared in ClickHouse — is the clickhouse-writer running?"
        failures=$((failures+1))
    fi

    info "Cleaning up flag '$flag_key'"
    code=$(http_code DELETE "http://localhost:8081/v1/admin/flags/$flag_key")
    if [ "$code" = "200" ]; then ok "DELETE /v1/admin/flags/$flag_key → 200 (archived)"; else
        warn "Cleanup returned $code — archive '$flag_key' manually if needed"; fi

    echo ""
    if [ "$failures" -eq 0 ]; then
        ok "Smoke test passed — events ingest, flags serve, queries answer"
    else
        die "Smoke test failed ($failures step(s))"
    fi
}

# ── dispatch ─────────────────────────────────────────────────────────

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-help}" in
    setup)   cmd_setup ;;
    up)      cmd_up ;;
    up-full) cmd_up_full ;;
    down)    cmd_down ;;
    reset)   shift; cmd_reset "$@" ;;
    status)  cmd_status ;;
    smoke)   cmd_smoke ;;
    test)    make -C "$ROOT_DIR" test ;;
    lint)    make -C "$ROOT_DIR" lint ;;
    check)   make -C "$ROOT_DIR" check ;;
    fmt)     make -C "$ROOT_DIR" fmt ;;
    logs)    shift; cmd_logs "$@" ;;
    help|-h|--help) usage ;;
    *)       usage; die "Unknown command: $1" ;;
esac
