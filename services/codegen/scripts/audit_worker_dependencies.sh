#!/usr/bin/env bash
# Reproducible, fail-closed vulnerability gate for the published worker image.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

command -v uv >/dev/null || {
    echo "Required command not found: uv" >&2
    exit 1
}
command -v uvx >/dev/null || {
    echo "Required command not found: uvx" >&2
    exit 1
}

PYTHON_312="$(uv python find 3.12)"
exec "$PYTHON_312" \
    "$ROOT_DIR/services/codegen/scripts/audit_worker_dependencies.py"
