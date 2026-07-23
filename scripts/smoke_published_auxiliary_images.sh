#!/usr/bin/env bash
# Launch the two official images that are not long-lived services in base Compose.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INDEX_PATH="${1:?usage: smoke_published_auxiliary_images.sh INDEX_PATH}"
EXPECTED_PLATFORM="${APDL_SMOKE_PLATFORM:?set APDL_SMOKE_PLATFORM}"
EXPECTED_REVISION="${APDL_EXPECTED_SOURCE_REVISION:?set APDL_EXPECTED_SOURCE_REVISION}"

case "$EXPECTED_PLATFORM" in
    linux/amd64) expected_architecture=amd64 ;;
    linux/arm64) expected_architecture=arm64 ;;
    *)
        echo "Unsupported smoke platform: $EXPECTED_PLATFORM" >&2
        exit 2
        ;;
esac

[ -f "$INDEX_PATH" ] || {
    echo "Published image index not found: $INDEX_PATH" >&2
    exit 1
}

image_reference() {
    python3 - "$INDEX_PATH" "$1" <<'PY'
import json
import re
import sys

path, requested_name = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    index = json.load(handle)
if set(index) != {"schema_version", "version", "tag", "images"}:
    raise SystemExit("published image index has an invalid schema")
if index["schema_version"] != 1 or not isinstance(index["images"], list):
    raise SystemExit("published image index is not a prepared index")
matches = [
    image for image in index["images"]
    if isinstance(image, dict) and image.get("name") == requested_name
]
if len(matches) != 1:
    raise SystemExit(f"published image reference count differs: {requested_name}")
reference = matches[0].get("reference")
if not isinstance(reference, str) or re.fullmatch(
    r"[^@\s]+@sha256:[0-9a-f]{64}",
    reference,
) is None:
    raise SystemExit(f"published image reference is invalid: {requested_name}")
print(reference)
PY
}

assert_native_image() {
    local image_name=$1 reference=$2 actual_architecture
    docker pull "$reference"
    actual_architecture="$(
        docker image inspect --format '{{.Architecture}}' "$reference"
    )"
    if [ "$actual_architecture" != "$expected_architecture" ]; then
        echo "$image_name resolved to $actual_architecture, expected $expected_architecture" >&2
        return 1
    fi
}

worker_reference="$(image_reference "codegen-worker")"
egress_reference="$(image_reference "codegen-egress")"
assert_native_image "codegen-worker" "$worker_reference"
assert_native_image "codegen-egress" "$egress_reference"

worker_revision="$(
    docker image inspect \
        --format '{{ index .Config.Labels "dev.apdl.codegen.revision" }}' \
        "$worker_reference"
)"
if [ "$worker_revision" != "$EXPECTED_REVISION" ]; then
    echo "Codegen worker revision label differs from the release commit" >&2
    exit 1
fi

echo "==> Launching the exact published Codegen worker entrypoint"
set +e
worker_output="$(
    printf '{}\n' |
        docker run --rm -i \
            --pull never \
            --network none \
            --read-only \
            --cap-drop ALL \
            --security-opt no-new-privileges \
            --pids-limit 64 \
            --memory 768m \
            --cpus 1 \
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
            --tmpfs /workspace:rw,nosuid,nodev,noexec,size=64m,mode=700,uid=1000,gid=1000 \
            "$worker_reference"
)"
worker_status=$?
set -e
if [ "$worker_status" -ne 1 ]; then
    echo "Codegen worker must reject the deliberately invalid probe request" >&2
    exit 1
fi
python3 - "$worker_output" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
if set(payload) != {"success", "error"}:
    raise SystemExit("Codegen worker rejection has an invalid schema")
if payload["success"] is not False:
    raise SystemExit("Codegen worker accepted an invalid probe request")
if not isinstance(payload["error"], str) or not payload["error"].startswith(
    "invalid sandbox input:"
):
    raise SystemExit("Codegen worker did not execute its request parser")
PY

policy_sha256="$(python3 "$ROOT_DIR/scripts/codegen-egress-policy-digest.py")"
egress_policy_label="$(
    docker image inspect \
        --format '{{ index .Config.Labels "dev.apdl.codegen.egress.policy-sha256" }}' \
        "$egress_reference"
)"
if [ "$egress_policy_label" != "$policy_sha256" ]; then
    echo "Codegen egress image policy label differs from shipped policy sources" >&2
    exit 1
fi

resource_suffix="$$-$(date -u +%s)"
egress_container="apdl-release-egress-$resource_suffix"
egress_volume="apdl-release-egress-$resource_suffix"
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    docker rm -f "$egress_container" >/dev/null 2>&1 || true
    docker volume rm "$egress_volume" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker volume create "$egress_volume" >/dev/null
echo "==> Launching the exact published Codegen egress entrypoint"
docker run -d \
    --name "$egress_container" \
    --pull never \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 128 \
    --memory 256m \
    --cpus 0.5 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
    --tmpfs /var/log/squid:rw,nosuid,nodev,noexec,size=16m,uid=13,gid=13 \
    --tmpfs /var/spool/squid:rw,nosuid,nodev,noexec,size=16m,uid=13,gid=13 \
    --mount "type=volume,src=$egress_volume,dst=/run/apdl-codegen-egress" \
    "$egress_reference" >/dev/null

egress_health=
for _ in $(seq 1 40); do
    egress_health="$(
        docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
            "$egress_container"
    )"
    if [ "$egress_health" = "healthy" ]; then
        break
    fi
    if [ "$egress_health" = "unhealthy" ]; then
        docker logs "$egress_container" >&2
        exit 1
    fi
    sleep 0.5
done
if [ "$egress_health" != "healthy" ]; then
    docker logs "$egress_container" >&2
    echo "Codegen egress image did not become healthy" >&2
    exit 1
fi
docker exec "$egress_container" /usr/local/bin/codegen-egress-healthcheck
echo "==> Published Codegen worker and egress probes passed on $EXPECTED_PLATFORM"
