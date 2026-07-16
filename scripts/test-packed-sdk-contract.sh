#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apdl-packed-sdk.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

(
  cd "$ROOT_DIR/sdk/javascript"
  npm pack \
    --silent \
    --pack-destination "$TMP_DIR" >/dev/null
)

TARBALL="$(find "$TMP_DIR" -maxdepth 1 -name '*.tgz' -print -quit)"
if [[ -z "$TARBALL" ]]; then
  echo "npm pack did not produce a tarball" >&2
  exit 1
fi

npm install \
  --silent \
  --ignore-scripts \
  --prefix "$TMP_DIR" \
  "$TARBALL"

cp "$ROOT_DIR/services/ingestion/tests/packed_sdk_consumer.mjs" "$TMP_DIR/consumer.mjs"
APDL_CAPTURE_PATH="$TMP_DIR/payload.json" node "$TMP_DIR/consumer.mjs"

cd "$ROOT_DIR/services/ingestion"
.venv/bin/python tests/validate_packed_sdk_payload.py "$TMP_DIR/payload.json"
