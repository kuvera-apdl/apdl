#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
UV="${UV:-uv}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apdl-packed-python-sdk.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ $# -gt 1 ]]; then
  echo "usage: $0 [empty-output-directory]" >&2
  exit 2
fi

if [[ $# -eq 1 ]]; then
  DIST_DIR="$1"
  mkdir -p "$DIST_DIR"
  if find "$DIST_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "output directory must be empty: $DIST_DIR" >&2
    exit 2
  fi
  DIST_DIR="$(cd "$DIST_DIR" && pwd)"
else
  DIST_DIR="$TMP_DIR/dist"
  mkdir -p "$DIST_DIR"
fi

"$UV" build \
  --python "$PYTHON" \
  --out-dir "$DIST_DIR" \
  --no-create-gitignore \
  "$ROOT_DIR/sdk/python"
"$PYTHON" "$ROOT_DIR/scripts/verify_python_artifacts.py" "$DIST_DIR"

VERSION="$(cd "$ROOT_DIR" && "$PYTHON" -c 'import json, pathlib; print(json.loads(pathlib.Path("release-manifest.json").read_text())["version"])')"
WHEEL="$DIST_DIR/apdl_sdk-${VERSION}-py3-none-any.whl"
SDIST="$DIST_DIR/apdl_sdk-${VERSION}.tar.gz"

verify_install() {
  local artifact="$1"
  local environment="$2"

  "$UV" venv --python "$PYTHON" "$environment" >/dev/null
  "$UV" pip install --python "$environment/bin/python" "$artifact" >/dev/null
  (
    cd "$TMP_DIR"
    APDL_EXPECTED_VERSION="$VERSION" PYTHONPATH= "$environment/bin/python" - <<'PY'
import os
from importlib.metadata import metadata, version

from apdl import APDLConfig, SDK_VERSION, __version__

expected = os.environ["APDL_EXPECTED_VERSION"]
assert version("apdl-sdk") == expected
assert __version__ == SDK_VERSION == expected
assert metadata("apdl-sdk").get_all("License-File") == ["LICENSE"]

config = APDLConfig(
    api_key="proj_contract_0123456789abcdef",
    endpoint="https://contract.invalid",
    enable_flags=False,
)
assert config.endpoint == "https://contract.invalid"
PY
  )
}

verify_install "$WHEEL" "$TMP_DIR/wheel-env"
verify_install "$SDIST" "$TMP_DIR/sdist-env"

echo "packed Python SDK contract passed"
