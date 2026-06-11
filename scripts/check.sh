#!/usr/bin/env bash
# Run every package's lint and test suite in parallel — a local mirror of the
# GitHub Actions CI matrix. Each package runs in its own background job; output
# is buffered per-job and printed under a header so logs don't interleave.
#
# Usage:
#   scripts/check.sh            # lint + test for all packages
#   scripts/check.sh lint       # lint only
#   scripts/check.sh test       # test only
#   scripts/check.sh --help
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-all}"
case "$MODE" in
  all|lint|test) ;;
  -h|--help)
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
    exit 0 ;;
  *) echo "unknown mode: $MODE (expected: all | lint | test)" >&2; exit 2 ;;
esac

# ── Colors ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $*"; }

# ── Package matrix ──────────────────────────────────────────────────
# Each entry: "name|dir|lint command|test command"
# An empty command field means that step is skipped for the package.
PACKAGES=(
  "js-sdk|sdk/javascript|npm run lint|npm test -- --run"
  "python-sdk|sdk/python|.venv/bin/ruff check apdl/ tests/|.venv/bin/python -m pytest -q"
  "ingestion|services/ingestion|.venv/bin/ruff check app/|.venv/bin/python -m pytest -q"
  "config|services/config|.venv/bin/ruff check app/|.venv/bin/python -m pytest -q"
  "query|services/query|.venv/bin/ruff check app/|.venv/bin/python -m pytest -q"
  "agents|services/agents|.venv/bin/ruff check app/|.venv/bin/python -m pytest -q"
  "etl|pipeline/etl|.venv/bin/ruff check etl/ scripts/ tests/|.venv/bin/python -m pytest -q"
)

LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT

# run_job <slug> <dir> <command> — execute a single step, capturing output.
run_job() {
  local slug="$1" dir="$2" cmd="$3"
  {
    if (cd "$dir" && eval "$cmd"); then
      echo "PASS" > "$LOG_DIR/$slug.status"
    else
      echo "FAIL" > "$LOG_DIR/$slug.status"
    fi
  } > "$LOG_DIR/$slug.log" 2>&1
}

jobs=()
for entry in "${PACKAGES[@]}"; do
  IFS='|' read -r name dir lint_cmd test_cmd <<< "$entry"
  if [[ "$MODE" == "all" || "$MODE" == "lint" ]] && [[ -n "$lint_cmd" ]]; then
    run_job "${name}__lint" "$dir" "$lint_cmd" & jobs+=("${name}__lint")
  fi
  if [[ "$MODE" == "all" || "$MODE" == "test" ]] && [[ -n "$test_cmd" ]]; then
    run_job "${name}__test" "$dir" "$test_cmd" & jobs+=("${name}__test")
  fi
done

info "Running ${#jobs[@]} jobs in parallel (mode: $MODE)…"
wait

# ── Report ──────────────────────────────────────────────────────────
fail=0
for slug in "${jobs[@]}"; do
  status="$(cat "$LOG_DIR/$slug.status" 2>/dev/null || echo FAIL)"
  pretty="${slug/__/ }"
  if [[ "$status" == "PASS" ]]; then
    echo -e "${GREEN}  ✓${NC} ${pretty}"
  else
    fail=1
    echo -e "${RED}  ✗${NC} ${pretty}"
    echo -e "${YELLOW}---- ${pretty} output ----${NC}"
    cat "$LOG_DIR/$slug.log"
    echo -e "${YELLOW}--------------------------${NC}"
  fi
done

if [[ $fail -ne 0 ]]; then
  echo -e "${RED}==> checks failed${NC}"
  exit 1
fi
echo -e "${GREEN}==> all checks passed${NC}"
