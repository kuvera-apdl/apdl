#!/usr/bin/env bash
# Auto-format every package: ruff (format + import/lint autofix) for Python,
# the SDK's own formatter for JavaScript. Run before committing to keep diffs
# clean and the CI lint jobs green.
#
# Usage:
#   scripts/fmt.sh           # format in place
#   scripts/fmt.sh --check   # report formatting diffs without writing (CI-style)
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $*"; }

# "dir|ruff paths"
PY_PACKAGES=(
  "sdk/python|apdl/ tests/"
  "services/ingestion|app/ tests/"
  "services/config|app/ tests/"
  "services/query|app/ tests/"
  "services/agents|app/ tests/"
  "pipeline/etl|etl/ scripts/ tests/"
)

fail=0
for entry in "${PY_PACKAGES[@]}"; do
  IFS='|' read -r dir paths <<< "$entry"
  ruff="$ROOT_DIR/$dir/.venv/bin/ruff"
  [[ -x "$ruff" ]] || ruff="ruff"  # fall back to a ruff on PATH
  info "ruff $dir"
  if [[ $CHECK -eq 1 ]]; then
    (cd "$dir" && "$ruff" format --check $paths && "$ruff" check $paths) || fail=1
  else
    (cd "$dir" && "$ruff" format $paths && "$ruff" check --fix $paths) || fail=1
  fi
done

# JavaScript SDK — uses tsc for lint; format via npm if a `format` script exists.
if grep -q '"format"' sdk/javascript/package.json 2>/dev/null; then
  info "format sdk/javascript"
  if [[ $CHECK -eq 1 ]]; then
    (cd sdk/javascript && npm run format -- --check) || fail=1
  else
    (cd sdk/javascript && npm run format) || fail=1
  fi
fi

if [[ $fail -ne 0 ]]; then
  echo -e "${RED}==> formatting issues found${NC}"
  exit 1
fi
echo -e "${GREEN}==> formatting clean${NC}"
