#!/usr/bin/env bash
set -euo pipefail

# Run the sealed evaluation controller against the exact production sandbox
# candidate image. The controller owns corpus/oracle data and the Docker socket;
# each candidate receives only its materialized workspace, public invocation,
# model configuration, and provider credential.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT_DIR"

git_head="$(git rev-parse HEAD)"
revision="${CODEGEN_REVISION:-$git_head}"
if [[ "$revision" == "$git_head" ]] \
  && [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "The worktree is dirty, so Git HEAD is not a complete candidate identity. Commit the evaluated source or set a distinct explicit CODEGEN_REVISION." >&2
  exit 2
fi
model="${CODEGEN_MODEL:-claude-opus-4-8}"
policy_path="${CODEGEN_ROLLOUT_POLICY:-$ROOT_DIR/services/codegen/app/evaluations/rollout_policy_v3.json}"
artifact_dir="${CODEGEN_EVALUATION_ARTIFACT_DIR:-$ROOT_DIR/local-files/codegen-rollouts/$revision}"
controller_image="${CODEGEN_EVALUATION_CONTROLLER_IMAGE:-apdl-codegen-evaluation-controller:$revision}"
candidate_image="${CODEGEN_SANDBOX_IMAGE:-apdl-codegen-sandbox:$revision}"

if [[ -z "$revision" || "$revision" == "development-unversioned" ]]; then
  echo "CODEGEN_REVISION must identify an immutable evaluated candidate; development-unversioned cannot authorize publication." >&2
  exit 2
fi
if [[ ! "$revision" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "CODEGEN_REVISION must also be a Docker-tag-safe identifier (letters, digits, underscore, period, and dash)." >&2
  exit 2
fi
if [[ -z "$model" ]]; then
  echo "CODEGEN_MODEL must not be empty." >&2
  exit 2
fi
if [[ ! -f "$policy_path" ]]; then
  echo "Checked-in rollout policy not found: $policy_path" >&2
  exit 2
fi

mkdir -p "$artifact_dir"
artifact_dir="$(cd -- "$artifact_dir" && pwd -P)"
policy_path="$(cd -- "$(dirname -- "$policy_path")" && pwd -P)/$(basename -- "$policy_path")"

policy_artifact="$artifact_dir/rollout-policy.json"
if [[ "$policy_path" != "$policy_artifact" ]]; then
  install -m 0600 "$policy_path" "$policy_artifact"
fi

# Never leave an older successful bundle or image identity beside a new failed
# attempt. Image IDs are written again only after their labels are verified;
# the bundle is written only after the complete trusted evaluation passes.
rm -f \
  "$artifact_dir/controller-image-id.txt" \
  "$artifact_dir/candidate-image-id.txt" \
  "$artifact_dir/evaluation-run.json" \
  "$artifact_dir/evaluation-report.json" \
  "$artifact_dir/evaluation-segments.json" \
  "$artifact_dir/publication-bundle.json"

socket_path="${CODEGEN_DOCKER_SOCKET:-}"
if [[ -z "$socket_path" ]]; then
  if [[ -S "$HOME/.docker/run/docker.sock" ]]; then
    socket_path="$HOME/.docker/run/docker.sock"
  else
    socket_path=/var/run/docker.sock
  fi
fi
if [[ ! -S "$socket_path" ]]; then
  echo "Docker socket not found: $socket_path (set CODEGEN_DOCKER_SOCKET explicitly)." >&2
  exit 2
fi
socket_path="$(cd -- "$(dirname -- "$socket_path")" && pwd -P)/$(basename -- "$socket_path")"

echo "==> Building evaluation controller: $controller_image"
docker build \
  --label "org.opencontainers.image.revision=$revision" \
  --label "dev.apdl.codegen.revision=$revision" \
  --label "dev.apdl.codegen.role=evaluation-controller" \
  --tag "$controller_image" \
  --file services/codegen/Dockerfile \
  services/codegen

controller_revision="$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.revision" }}' "$controller_image")"
controller_role="$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.role" }}' "$controller_image")"
if [[ "$controller_revision" != "$revision" || "$controller_role" != "evaluation-controller" ]]; then
  echo "Controller image identity mismatch for revision $revision." >&2
  exit 1
fi
controller_image_id="$(docker image inspect --format '{{.Id}}' "$controller_image")"
if [[ ! "$controller_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Controller image did not resolve to an immutable local image ID: $controller_image_id" >&2
  exit 1
fi
printf '%s\n' "$controller_image_id" >"$artifact_dir/controller-image-id.txt"
chmod 0600 "$artifact_dir/controller-image-id.txt"

echo "==> Building production candidate: $candidate_image"
docker build \
  --build-arg "CODEGEN_REVISION=$revision" \
  --tag "$candidate_image" \
  --file services/codegen/Dockerfile.worker \
  services/codegen

candidate_revision="$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.revision" }}' "$candidate_image")"
candidate_role="$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.role" }}' "$candidate_image")"
if [[ "$candidate_revision" != "$revision" || "$candidate_role" != "candidate" ]]; then
  echo "Candidate image identity mismatch for revision $revision." >&2
  exit 1
fi
candidate_image_id="$(docker image inspect --format '{{.Id}}' "$candidate_image")"
if [[ ! "$candidate_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Candidate image did not resolve to an immutable local image ID: $candidate_image_id" >&2
  exit 1
fi
printf '%s\n' "$candidate_image_id" >"$artifact_dir/candidate-image-id.txt"
chmod 0600 "$artifact_dir/candidate-image-id.txt"

# Fail before spending model tokens if the image lacks the real entry point or
# accidentally carries evaluator-only answer material.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint sh "$candidate_image_id" -ec '
  command -v apdl-codegen-evaluate-candidate >/dev/null
  root="$(python -c "from pathlib import Path; import app.evaluations; print(Path(app.evaluations.__file__).parent)")"
  test ! -e "$root/oracles_v1.json"
  test ! -e "$root/corpus_v2.json"
  test ! -e "$root/fixtures"
'

temp_parent="${TMPDIR:-/tmp}"
evaluation_root="$(mktemp -d "$temp_parent/apdl-codegen-eval.XXXXXX")"
trap 'rm -rf -- "$evaluation_root"' EXIT
mkdir -p "$evaluation_root/home" "$evaluation_root/tmp"
chmod 0700 "$evaluation_root" "$evaluation_root/home" "$evaluation_root/tmp"

socket_gid=""
if socket_gid="$(stat -c '%g' "$socket_path" 2>/dev/null)"; then
  :
elif socket_gid="$(stat -f '%g' "$socket_path" 2>/dev/null)"; then
  :
else
  echo "Could not determine Docker socket group: $socket_path" >&2
  exit 1
fi

docker_args=(
  run --rm --read-only --network none
  --cap-drop ALL
  --security-opt no-new-privileges
  --user "$(id -u):$(id -g)"
  --group-add "$socket_gid"
  --mount "type=bind,src=$evaluation_root,dst=$evaluation_root"
  --mount "type=bind,src=$artifact_dir,dst=/artifacts"
  --mount "type=bind,src=$socket_path,dst=/var/run/docker.sock"
  --env "HOME=$evaluation_root/home"
  --env "TMPDIR=$evaluation_root/tmp"
  --env "DOCKER_HOST=unix:///var/run/docker.sock"
  --env "CODEGEN_MODEL=$model"
  --env "CODEGEN_REVISION=$revision"
)

# Explicit allowlist: never forward GitHub App material, installation tokens,
# PostgreSQL/Redis URLs, APDL credentials, SSH agents, or the caller's full .env.
forward_env=(
  CODEGEN_HELPER_MODEL
  CODEGEN_AIDER_BIN
  CODEGEN_BRIEF
  CODEGEN_REVIEW
  CODEGEN_EDIT_RETRIES
  CODEGEN_REQUIRE_VERIFY
  CODEGEN_CACHE_PROMPTS
  CODEGEN_CONVENTIONS
  CODEGEN_SDK_REFERENCE
  CODEGEN_CONTRACTS
  CODEGEN_CONTRACT_INSTALL_TIMEOUT
  CODEGEN_TIMEOUT
  CODEGEN_JOB_BUDGET
  CODEGEN_GIT_TIMEOUT
  CODEGEN_LLM_TIMEOUT
  CODEGEN_EVALUATION_NETWORK
  CODEGEN_EVALUATION_MEMORY
  CODEGEN_EVALUATION_CPUS
  CODEGEN_EVALUATION_PIDS
  OPENAI_API_KEY
  OPENAI_API_BASE
  OPENAI_BASE_URL
  ANTHROPIC_API_KEY
  ANTHROPIC_BASE_URL
  GOOGLE_API_KEY
  GEMINI_API_KEY
  VERTEXAI_PROJECT
  VERTEXAI_LOCATION
  OPENROUTER_API_KEY
  MISTRAL_API_KEY
  GROQ_API_KEY
  DEEPSEEK_API_KEY
  COHERE_API_KEY
  TOGETHERAI_API_KEY
  FIREWORKS_API_KEY
  XAI_API_KEY
  OLLAMA_API_BASE
  AZURE_API_KEY
  AZURE_API_BASE
  AZURE_API_VERSION
)
for name in "${forward_env[@]}"; do
  if [[ -n "${!name:-}" ]]; then
    docker_args+=(--env "$name")
  fi
done

echo "==> Evaluating $candidate_image_id as revision $revision with $model"
docker "${docker_args[@]}" "$controller_image_id" \
  python -m app.evaluations.cli \
  --controller-image-id "$controller_image_id" \
  --docker-image "$candidate_image_id" \
  --model "$model" \
  --codegen-revision "$revision" \
  --rollout-policy /artifacts/rollout-policy.json \
  --run-output /artifacts/evaluation-run.json \
  --report-output /artifacts/evaluation-report.json \
  --segmented-output /artifacts/evaluation-segments.json \
  --bundle-output /artifacts/publication-bundle.json

for artifact in \
  evaluation-run.json \
  evaluation-report.json \
  evaluation-segments.json \
  publication-bundle.json; do
  if [[ ! -s "$artifact_dir/$artifact" ]]; then
    echo "Evaluation did not create $artifact_dir/$artifact" >&2
    exit 1
  fi
done

chmod 0600 "$artifact_dir"/*.json
echo "==> Evaluation artifacts: $artifact_dir"
echo "    Mount publication-bundle.json read-only before selecting reviewed_pr."
