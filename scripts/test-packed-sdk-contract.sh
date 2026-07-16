#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE="${NODE:-node}"
NPM="${NPM:-npm}"
PYTHON="${PYTHON:-}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apdl-packed-sdk.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ $# -gt 1 ]]; then
  echo "usage: $0 [empty-output-directory]" >&2
  exit 2
fi

if [[ -z "$PYTHON" ]]; then
  if [[ -x "$ROOT_DIR/services/ingestion/.venv/bin/python" ]]; then
    PYTHON="$ROOT_DIR/services/ingestion/.venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

if [[ $# -eq 1 ]]; then
  ARTIFACT_DIR="$1"
  mkdir -p "$ARTIFACT_DIR"
  if find "$ARTIFACT_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "output directory must be empty: $ARTIFACT_DIR" >&2
    exit 2
  fi
  ARTIFACT_DIR="$(cd "$ARTIFACT_DIR" && pwd)"
else
  ARTIFACT_DIR="$TMP_DIR/artifacts"
  mkdir -p "$ARTIFACT_DIR"
fi

(
  cd "$ROOT_DIR/sdk/javascript"
  "$NPM" pack \
    --silent \
    --pack-destination "$ARTIFACT_DIR" >/dev/null
  "$NPM" run lint:package
)

VERSION="$(cd "$ROOT_DIR/sdk/javascript" && "$NODE" -p 'require("./package.json").version')"
TARBALL="$ARTIFACT_DIR/apdl-oss-sdk-${VERSION}.tgz"
if [[ ! -f "$TARBALL" ]]; then
  echo "npm pack did not produce $TARBALL" >&2
  exit 1
fi
if [[ "$(find "$ARTIFACT_DIR" -maxdepth 1 -type f -name '*.tgz' | wc -l | tr -d ' ')" != "1" ]]; then
  echo "npm pack produced an unexpected artifact set" >&2
  exit 1
fi

"$NPM" install \
  --silent \
  --ignore-scripts \
  --prefix "$TMP_DIR/consumer" \
  "$TARBALL" \
  react@19.2.0 \
  @types/react@19.2.17 \
  typescript@5.9.3

PACKAGE_DIR="$TMP_DIR/consumer/node_modules/@apdl-oss/sdk"
test -f "$PACKAGE_DIR/LICENSE"
test -f "$PACKAGE_DIR/README.md"
test "$(head -n 1 "$PACKAGE_DIR/dist/react.esm.js")" = "'use client';"
test "$(head -n 1 "$PACKAGE_DIR/dist/react.cjs")" = "'use client';"

(
  cd "$TMP_DIR/consumer"
  "$NODE" - <<'NODE'
const assert = require('node:assert/strict');
const sdk = require('@apdl-oss/sdk');
const react = require('@apdl-oss/sdk/react');

assert.equal(typeof sdk.init, 'function');
assert.equal(typeof sdk.APDL.init, 'function');
assert.equal(typeof react.APDLProvider, 'function');
assert.equal(typeof react.useAPDL, 'function');
NODE
)

cp \
  "$ROOT_DIR/services/ingestion/tests/packed_sdk_consumer_nodenext.mts" \
  "$ROOT_DIR/services/ingestion/tests/packed_sdk_consumer_nodenext.cts" \
  "$ROOT_DIR/services/ingestion/tests/tsconfig.packed-sdk-nodenext.json" \
  "$TMP_DIR/consumer/"
"$TMP_DIR/consumer/node_modules/.bin/tsc" \
  --project "$TMP_DIR/consumer/tsconfig.packed-sdk-nodenext.json"

cp "$ROOT_DIR/services/ingestion/tests/packed_sdk_consumer.mjs" "$TMP_DIR/consumer/consumer.mjs"
APDL_CAPTURE_PATH="$TMP_DIR/payload.json" "$NODE" "$TMP_DIR/consumer/consumer.mjs"

cd "$ROOT_DIR/services/ingestion"
"$PYTHON" tests/validate_packed_sdk_payload.py "$TMP_DIR/payload.json"

echo "packed JavaScript SDK contract passed"
