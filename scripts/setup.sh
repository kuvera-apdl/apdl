#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_VERSION="3.12"

# ── Colors ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}==>${NC} $*"; }
ok()    { echo -e "${GREEN}  ✓${NC} $*"; }
warn()  { echo -e "${YELLOW}  ⚠${NC} $*"; }
fail()  { echo -e "${RED}  ✗${NC} $*"; exit 1; }

# ── Preflight checks ───────────────────────────────────────────────
info "Checking prerequisites"

command -v uv   >/dev/null 2>&1 || fail "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v node >/dev/null 2>&1 || fail "node not found. Install Node.js >= 20"
command -v docker >/dev/null 2>&1 || fail "docker not found. Install Docker Desktop"

ok "uv $(uv --version 2>&1 | head -1)"
ok "node $(node --version)"
ok "docker $(docker --version | cut -d' ' -f3 | tr -d ',')"

# ── .env file ───────────────────────────────────────────────────────
if [ ! -f "$ROOT_DIR/.env" ]; then
    if [ -f "$ROOT_DIR/.env.example" ]; then
        cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
        ok "Created .env from .env.example"
        warn "Review .env and add any API keys before running services"
    fi
else
    ok ".env already exists"
fi

# ── Python services ────────────────────────────────────────────────
setup_python_service() {
    local name="$1"
    local dir="$2"

    info "Setting up $name"

    if [ ! -d "$dir" ]; then
        warn "Directory $dir not found, skipping"
        return
    fi

    cd "$dir"

    # Create venv with uv
    if [ ! -d ".venv" ]; then
        uv venv --python "$PYTHON_VERSION" .venv
        ok "Created virtualenv"
    else
        ok "Virtualenv already exists"
    fi

    # Install dependencies
    if [ -f "pyproject.toml" ]; then
        uv pip install -e ".[dev]" --python .venv/bin/python
        ok "Installed dependencies from pyproject.toml"
    elif [ -f "requirements.txt" ]; then
        uv pip install -r requirements.txt --python .venv/bin/python
        ok "Installed dependencies from requirements.txt"
    fi

    cd "$ROOT_DIR"
}

setup_python_service "Query Service"    "$ROOT_DIR/services/query"
setup_python_service "Agents Service"   "$ROOT_DIR/services/agents"
setup_python_service "Pipeline Writer"  "$ROOT_DIR/pipeline/redis"

# ── SDK (JavaScript) ───────────────────────────────────────────────
info "Setting up JavaScript SDK"
cd "$ROOT_DIR/sdk/javascript"
npm install --silent
ok "Installed npm dependencies"
cd "$ROOT_DIR"

# ── Docker infrastructure ──────────────────────────────────────────
info "Starting infrastructure (Redis, ClickHouse, PostgreSQL)"
docker compose -f "$ROOT_DIR/infra/docker/docker-compose.deps.yml" up -d

# Wait for health checks
echo -n "  Waiting for services to be healthy"
for i in $(seq 1 30); do
    healthy=$(docker compose -f "$ROOT_DIR/infra/docker/docker-compose.deps.yml" ps --format json 2>/dev/null | grep -c '"healthy"' || true)
    total=$(docker compose -f "$ROOT_DIR/infra/docker/docker-compose.deps.yml" ps --format json 2>/dev/null | grep -c '"running"\|"healthy"' || true)
    if [ "$healthy" -ge 3 ] 2>/dev/null; then
        break
    fi
    echo -n "."
    sleep 2
done
echo ""
ok "Infrastructure is running"

# ── ClickHouse migrations ──────────────────────────────────────────
CLICKHOUSE_COMPOSE_FILE="$ROOT_DIR/infra/docker/docker-compose.deps.yml" "$ROOT_DIR/scripts/init-clickhouse.sh"
ok "ClickHouse schema initialized"

# ── Summary ─────────────────────────────────────────────────────────
echo ""
info "Setup complete! Available commands:"
echo ""
echo "  make run-query       Start Query Service    (port 8082)"
echo "  make run-agents      Start Agents Service   (port 8083)"
echo "  make run-pipeline    Start Pipeline Writer"
echo "  make dev-all         Start everything via Docker"
echo ""
echo "  make test            Run all tests"
echo "  make lint            Run all linters"
echo ""
echo "  make dev-down        Stop infrastructure"
echo ""
