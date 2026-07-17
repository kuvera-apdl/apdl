#!/usr/bin/env bash
# Blocking dependency-vulnerability gate for published and supported runtimes.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apdl-dependency-audit.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

command -v npm >/dev/null || {
    echo "Required command not found: npm" >&2
    exit 1
}
command -v uv >/dev/null || {
    echo "Required command not found: uv" >&2
    exit 1
}

for package_dir in sdk/javascript services/admin; do
    echo "==> Auditing $package_dir"
    (cd "$ROOT_DIR/$package_dir" && npm audit --audit-level=high)
done

for lock in \
    services/ingestion/requirements.lock \
    services/config/requirements.lock \
    services/query/requirements.lock \
    services/agents/requirements.lock \
    services/admin-api/requirements.lock \
    pipeline/redis/requirements.lock
do
    echo "==> Auditing $lock"
    uvx pip-audit --strict --require-hashes -r "$ROOT_DIR/$lock"
done

for project in sdk/python services/codegen; do
    slug="${project//\//-}"
    requirements="$TMP_DIR/$slug.txt"
    if [ "$project" = "services/codegen" ]; then
        echo "==> Resolving and auditing $project (offline API only; agent extra excluded)"
    else
        echo "==> Resolving and auditing $project"
    fi
    uv pip compile "$ROOT_DIR/$project/pyproject.toml" \
        --python-version 3.12 \
        --output-file "$requirements" \
        --quiet
    uvx pip-audit --strict -r "$requirements"
done

echo "==> Dependency audits passed"
