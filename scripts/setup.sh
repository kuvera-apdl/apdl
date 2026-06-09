#!/usr/bin/env bash
# Thin wrapper kept for compatibility (`make setup`) — the full implementation
# lives in scripts/dev.sh, which also covers running, status, and smoke tests.
exec "$(cd "$(dirname "$0")" && pwd)/dev.sh" setup "$@"
