.PHONY: all setup deps build test clean lint check audit-dependencies fmt fmt-check dev dev-core dev-all dev-down smoke-fresh smoke-experiment-fresh test-clickhouse-upgrade test-query-clickhouse install-hooks lint-staged migrate-clickhouse migrate-postgres test-script-contracts test-sdk-python lint-sdk-python setup-sdk release-sdk verify-release test-packed-sdk-contract test-packed-python-sdk status smoke run-admin build-admin test-admin lint-admin clean-admin run-admin-api test-admin-api lint-admin-api create-admin-user test-writer lint-writer build-codegen-controller build-codegen-sandbox build-codegen-egress-proxy build-codegen-runtime evaluate-codegen codegen-development-prepare codegen-reviewed-config codegen-reviewed-up grant-codegen-repository revoke-codegen-repository

# ─── Top-Level ───────────────────────────────────────────────

all: build

CLICKHOUSE_COMPOSE_FILE ?= infra/docker/docker-compose.deps.yml
POSTGRES_COMPOSE_FILE ?= infra/docker/docker-compose.deps.yml

# Full-stack Compose command. `-f infra/docker/docker-compose.yml` makes Compose
# use that folder as its project dir (and its default `.env` lookup), so the
# repo-root `.env` is otherwise ignored — load it explicitly when it exists.
COMPOSE_FILE ?= infra/docker/docker-compose.yml
COMPOSE := docker compose $(if $(wildcard .env),--env-file .env,) -f $(COMPOSE_FILE)
SERVICE_ENV_FILE := $(if $(wildcard .env),--env-file ../../.env,)

# One immutable identity binds the evaluation controller, production candidate,
# evidence bundle, and reviewed-PR deployment. Environment values may override
# these defaults, but the evaluation script rejects development-unversioned.
CODEGEN_REVISION ?= $(shell git rev-parse HEAD 2>/dev/null)
CODEGEN_MODEL ?= claude-opus-4-8
CODEGEN_EVALUATION_CONTROLLER_IMAGE ?= apdl-codegen-evaluation-controller:$(CODEGEN_REVISION)
CODEGEN_SANDBOX_IMAGE ?= apdl-codegen-sandbox:$(CODEGEN_REVISION)
CODEGEN_EVALUATION_ARTIFACT_DIR ?= $(CURDIR)/local-files/codegen-rollouts/$(CODEGEN_REVISION)
CODEGEN_ROLLOUT_POLICY ?= $(CURDIR)/services/codegen/app/evaluations/rollout_policy_v4.json
CODEGEN_ROLLOUT_BUNDLE_PATH ?= $(CODEGEN_EVALUATION_ARTIFACT_DIR)/publication-bundle.json
CODEGEN_EVALUATED_CONTROLLER_IMAGE ?= $(shell test -s "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/controller-image-id.txt" && cat "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/controller-image-id.txt")
CODEGEN_EVALUATED_SANDBOX_IMAGE ?= $(shell test -s "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/candidate-image-id.txt" && cat "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/candidate-image-id.txt")
CODEGEN_SHIPPED_EGRESS_POLICY_SHA256 := $(shell python3 scripts/codegen-egress-policy-digest.py 2>/dev/null)
CODEGEN_EGRESS_POLICY_SHA256 ?= $(CODEGEN_SHIPPED_EGRESS_POLICY_SHA256)
CODEGEN_EGRESS_PROXY_IMAGE ?= apdl-codegen-egress-proxy:$(CODEGEN_EGRESS_POLICY_SHA256)
CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE ?= $(shell test -s "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/egress-proxy-image-id.txt" && cat "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/egress-proxy-image-id.txt")
CODEGEN_EGRESS_COMPOSE_FILE ?= infra/docker/docker-compose.codegen-egress.yml
CODEGEN_ROLLOUT_COMPOSE_FILE ?= infra/docker/docker-compose.codegen-rollout.yml
CODEGEN_EVALUATION_SOCKET_VOLUME ?=
CODEGEN_EGRESS_SOCKET_VOLUME ?= apdl-codegen-reviewed-egress-$(CODEGEN_EGRESS_POLICY_SHA256)
CODEGEN_DOCKER_SOCKET ?= $(if $(wildcard $(HOME)/.docker/run/docker.sock),$(HOME)/.docker/run/docker.sock,/var/run/docker.sock)
CODEGEN_DOCKER_UID ?= $(shell id -u)
CODEGEN_DOCKER_GID ?= $(shell id -g)
CODEGEN_DOCKER_SOCKET_GID ?= $(shell stat -c '%g' "$(CODEGEN_DOCKER_SOCKET)" 2>/dev/null || stat -f '%g' "$(CODEGEN_DOCKER_SOCKET)" 2>/dev/null || echo 0)

# Explicit Codegen development-publication tooling is separate from the normal
# core and dev-all paths. The supported stacks never mount the Docker socket or
# enable development_pr; reviewed publication uses its own evaluated overlay.
CODEGEN_DEVELOPMENT_REVISION := local-development
CODEGEN_DEVELOPMENT_SANDBOX_IMAGE := apdl-codegen-sandbox:$(CODEGEN_DEVELOPMENT_REVISION)
CODEGEN_DEVELOPMENT_SANDBOX_NETWORK := apdl-codegen-development
CODEGEN_DEVELOPMENT_COMPOSE_FILE := infra/docker/docker-compose.codegen-development.yml
CODEGEN_DEVELOPMENT_DOCKER_ENDPOINT := $(or $(strip $(DOCKER_HOST)),$(shell docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null))
CODEGEN_DEVELOPMENT_CONTEXT_SOCKET := $(patsubst unix://%,%,$(filter unix://%,$(CODEGEN_DEVELOPMENT_DOCKER_ENDPOINT)))
CODEGEN_DEVELOPMENT_DOCKER_SOCKET ?= $(if $(CODEGEN_DEVELOPMENT_DOCKER_ENDPOINT),$(CODEGEN_DEVELOPMENT_CONTEXT_SOCKET),$(if $(wildcard $(HOME)/.docker/run/docker.sock),$(HOME)/.docker/run/docker.sock,/var/run/docker.sock))
CODEGEN_DEVELOPMENT_DOCKER_UID := $(shell id -u)
CODEGEN_DEVELOPMENT_DOCKER_GID := $(shell id -g)
CODEGEN_DEVELOPMENT_DOCKER_SOCKET_GID = $(shell stat -c '%g' "$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" 2>/dev/null || stat -f '%g' "$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" 2>/dev/null)

setup:
	@bash scripts/setup.sh

install-hooks:
	@git config core.hooksPath .githooks
	@echo "==> Git hooks path set to .githooks"
	@echo "    pre-commit: ruff + merge-marker guard on staged files"
	@echo "    pre-push:   pytest for services whose .py files changed in the push range"
	@echo "    Bypass once with: git commit --no-verify  /  git push --no-verify"

lint-staged:
	@.githooks/pre-commit

deps:
	@echo "==> Installing SDK dependencies"
	cd sdk/javascript && npm install
	@echo "==> Installing Admin Console dependencies"
	cd services/admin && npm install
	@echo "==> Setting up Admin API"
	cd services/admin-api && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Ingestion service"
	cd services/ingestion && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Config service"
	cd services/config && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Query service"
	cd services/query && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Agents service"
	cd services/agents && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Codegen service"
	cd services/codegen && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Pipeline"
	cd pipeline/redis && uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt --python .venv/bin/python
	@echo "==> Setting up Python SDK"
	cd sdk/python && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python

build: build-sdk build-admin

test: test-script-contracts test-sdk test-sdk-python test-ingestion test-config test-query test-agents test-codegen test-writer test-admin-api test-admin

lint: lint-sdk lint-sdk-python lint-ingestion lint-config lint-query lint-agents lint-codegen lint-writer lint-admin-api lint-admin

clean: clean-sdk clean-admin

# Parallel local CI mirror: lint + test every package at once.
check:
	@bash scripts/check.sh

audit-dependencies:
	@bash scripts/audit_dependencies.sh

test-script-contracts:
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

# Auto-format all packages (ruff format + autofix; JS formatter if present).
fmt:
	@bash scripts/fmt.sh

fmt-check:
	@bash scripts/fmt.sh --check

# ─── SDK (JavaScript) ─────────────────────────────────────────────────────

build-sdk:
	cd sdk/javascript && npm run build

setup-sdk:
	cd sdk/javascript && npm run setup

test-sdk:
	cd sdk/javascript && npm test

clean-sdk:
	rm -rf sdk/javascript/dist sdk/javascript/node_modules

lint-sdk:
	cd sdk/javascript && npm run lint

release-sdk:
	cd sdk/javascript && npm run release:check

verify-release:
	./scripts/verify_release.py

test-packed-python-sdk:
	./scripts/test-packed-python-sdk.sh

# ─── Admin Console (TypeScript) ──────────────────────────────

run-admin:
	cd services/admin && npm run dev

build-admin:
	cd services/admin && npm run build

test-admin:
	cd services/admin && npm test

lint-admin:
	cd services/admin && npm run lint

clean-admin:
	rm -rf services/admin/dist services/admin/node_modules

# ─── Admin API (Python) ─────────────────────────────────────

run-admin-api:
	cd services/admin-api && APDL_ADMIN_COOKIE_SECURE=false .venv/bin/python -m uvicorn app.main:app --reload --port 8085 --no-proxy-headers $(SERVICE_ENV_FILE)

test-admin-api:
	cd services/admin-api && .venv/bin/python -m pytest -v

lint-admin-api:
	cd services/admin-api && .venv/bin/ruff check app/ scripts/ tests/

create-admin-user:
	cd services/admin-api && .venv/bin/python scripts/create_admin_user.py $(ARGS)

# ─── SDK (Python) ────────────────────────────────────────────

test-sdk-python:
	cd sdk/python && .venv/bin/python -m pytest -q --cov=apdl --cov-report=term-missing --cov-fail-under=88

lint-sdk-python:
	cd sdk/python && .venv/bin/ruff check apdl/ tests/

# ─── Ingestion Service (Python) ─────────────────────────────

test-ingestion:
	cd services/ingestion && .venv/bin/python -m pytest -v

test-packed-sdk-contract:
	./scripts/test-packed-sdk-contract.sh

lint-ingestion:
	cd services/ingestion && .venv/bin/ruff check app/

run-ingestion:
	cd services/ingestion && .venv/bin/python -m uvicorn app.main:app --reload --port 8080 --no-proxy-headers $(SERVICE_ENV_FILE)

# ─── Config Service (Python) ────────────────────────────────

test-config:
	cd services/config && .venv/bin/python -m pytest -v

lint-config:
	cd services/config && .venv/bin/ruff check app/

run-config:
	cd services/config && .venv/bin/python -m uvicorn app.main:app --reload --port 8081 $(SERVICE_ENV_FILE)

# ─── Query Service (Python) ──────────────────────────────────

test-query:
	cd services/query && .venv/bin/python -m pytest -v

test-query-clickhouse:
	@bash scripts/test_query_selectors_clickhouse.sh

lint-query:
	cd services/query && .venv/bin/ruff check app/

run-query:
	cd services/query && .venv/bin/python -m uvicorn app.main:app --reload --port 8082 $(SERVICE_ENV_FILE)

# ─── Agents Service (Python) ─────────────────────────────────

test-agents:
	cd services/agents && .venv/bin/python -m pytest -v

lint-agents:
	cd services/agents && .venv/bin/ruff check app/

run-agents:
	cd services/agents && .venv/bin/python -m uvicorn app.main:app --reload --port 8083 $(SERVICE_ENV_FILE)

# ─── Codegen Service (Python) ────────────────────────────────

test-codegen:
	cd services/codegen && .venv/bin/python -m pytest -v

lint-codegen:
	cd services/codegen && .venv/bin/ruff check app/

run-codegen:
	cd services/codegen && .venv/bin/python -m uvicorn app.main:app --reload --port 8084 $(SERVICE_ENV_FILE)

build-codegen-controller:
	docker build \
		--label "org.opencontainers.image.revision=$(CODEGEN_REVISION)" \
		--label "dev.apdl.codegen.revision=$(CODEGEN_REVISION)" \
		--label "dev.apdl.codegen.role=evaluation-controller" \
		-f services/codegen/Dockerfile \
		-t "$(CODEGEN_EVALUATION_CONTROLLER_IMAGE)" \
		services/codegen

build-codegen-sandbox:
	docker build \
		--build-arg "CODEGEN_REVISION=$(CODEGEN_REVISION)" \
		-f services/codegen/Dockerfile.worker \
		-t "$(CODEGEN_SANDBOX_IMAGE)" \
		services/codegen

build-codegen-egress-proxy:
	docker build \
		--build-arg "CODEGEN_EGRESS_POLICY_SHA256=$(CODEGEN_EGRESS_POLICY_SHA256)" \
		-f infra/docker/codegen-egress/Dockerfile \
		-t "$(CODEGEN_EGRESS_PROXY_IMAGE)" \
		infra/docker/codegen-egress

build-codegen-runtime: build-codegen-controller build-codegen-sandbox build-codegen-egress-proxy

codegen-development-prepare:
	@test -n "$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" || (echo "The active Docker context is not a local unix socket; codegen-development-prepare requires local Docker." >&2; exit 1)
	@case "$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" in /*) ;; *) echo "Docker unix socket path must be absolute: $(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" >&2; exit 1;; esac
	@test -S "$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" || (echo "Docker unix socket not found: $(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" >&2; exit 1)
	@test -n "$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET_GID)" || (echo "Could not determine Docker socket group: $(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" >&2; exit 1)
	@if docker network inspect "$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)" >/dev/null 2>&1; then \
		scope="$$(docker network inspect --format '{{ index .Labels "dev.apdl.codegen.scope" }}' "$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)")"; \
		driver="$$(docker network inspect --format '{{.Driver}}' "$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)")"; \
		internal="$$(docker network inspect --format '{{.Internal}}' "$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)")"; \
		test "$$scope" = "local-development" || (echo "Docker network $(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK) exists but is not APDL's development sandbox network." >&2; exit 1); \
		test "$$driver" = "bridge" -a "$$internal" = "false" || (echo "Docker network $(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK) must be a non-internal bridge for local GitHub/model access." >&2; exit 1); \
	else \
		docker network create --driver bridge --label dev.apdl.codegen.scope=local-development "$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)" >/dev/null; \
	fi
	@echo "==> Codegen sandbox network: $(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK) (development-only; not egress-filtered)"
	@$(MAKE) --no-print-directory build-codegen-sandbox \
		CODEGEN_REVISION="$(CODEGEN_DEVELOPMENT_REVISION)" \
		CODEGEN_SANDBOX_IMAGE="$(CODEGEN_DEVELOPMENT_SANDBOX_IMAGE)"
	CODEGEN_DEVELOPMENT_DOCKER_SOCKET="$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)" \
	CODEGEN_DEVELOPMENT_DOCKER_UID="$(CODEGEN_DEVELOPMENT_DOCKER_UID)" \
	CODEGEN_DEVELOPMENT_DOCKER_GID="$(CODEGEN_DEVELOPMENT_DOCKER_GID)" \
	CODEGEN_DEVELOPMENT_DOCKER_SOCKET_GID="$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET_GID)" \
	CODEGEN_DEVELOPMENT_SANDBOX_IMAGE="$(CODEGEN_DEVELOPMENT_SANDBOX_IMAGE)" \
	CODEGEN_DEVELOPMENT_SANDBOX_NETWORK="$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)" \
	$(COMPOSE) -f $(CODEGEN_DEVELOPMENT_COMPOSE_FILE) config --quiet

evaluate-codegen:
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_EVALUATION_CONTROLLER_IMAGE="$(CODEGEN_EVALUATION_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_SANDBOX_IMAGE)" \
	CODEGEN_EVALUATION_ARTIFACT_DIR="$(CODEGEN_EVALUATION_ARTIFACT_DIR)" \
	CODEGEN_ROLLOUT_POLICY="$(CODEGEN_ROLLOUT_POLICY)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	CODEGEN_EGRESS_POLICY_SHA256="$(CODEGEN_EGRESS_POLICY_SHA256)" \
	CODEGEN_EGRESS_PROXY_IMAGE="$(CODEGEN_EGRESS_PROXY_IMAGE)" \
	CODEGEN_EVALUATION_SOCKET_VOLUME="$(CODEGEN_EVALUATION_SOCKET_VOLUME)" \
	./scripts/evaluate-codegen.sh

codegen-reviewed-config:
	@test "$(CODEGEN_EGRESS_POLICY_SHA256)" = "$(CODEGEN_SHIPPED_EGRESS_POLICY_SHA256)" || (echo "CODEGEN_EGRESS_POLICY_SHA256 does not match the checked-in egress policy sources" >&2; exit 1)
	@test -s "$(CODEGEN_ROLLOUT_BUNDLE_PATH)" || (echo "Missing rollout bundle: $(CODEGEN_ROLLOUT_BUNDLE_PATH)" >&2; exit 1)
	@test -n "$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" || (echo "Missing evaluated controller identity: $(CODEGEN_EVALUATION_ARTIFACT_DIR)/controller-image-id.txt" >&2; exit 1)
	@docker image inspect "$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" >/dev/null || (echo "Missing evaluated controller image: $(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" >&2; exit 1)
	@test "$$(docker image inspect --format '{{.Id}}' "$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)")" = "$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" || (echo "Controller reference is not its immutable local image ID" >&2; exit 1)
	@test "$$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.revision" }}' "$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)")" = "$(CODEGEN_REVISION)" || (echo "Evaluated controller image does not match CODEGEN_REVISION=$(CODEGEN_REVISION)" >&2; exit 1)
	@test "$$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.role" }}' "$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)")" = "evaluation-controller" || (echo "Evaluated controller image has the wrong role" >&2; exit 1)
	@test -n "$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" || (echo "Missing evaluated candidate identity: $(CODEGEN_EVALUATION_ARTIFACT_DIR)/candidate-image-id.txt" >&2; exit 1)
	@docker image inspect "$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" >/dev/null || (echo "Missing evaluated candidate image: $(CODEGEN_EVALUATED_SANDBOX_IMAGE)" >&2; exit 1)
	@test "$$(docker image inspect --format '{{.Id}}' "$(CODEGEN_EVALUATED_SANDBOX_IMAGE)")" = "$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" || (echo "Candidate reference is not its immutable local image ID" >&2; exit 1)
	@test "$$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.revision" }}' "$(CODEGEN_EVALUATED_SANDBOX_IMAGE)")" = "$(CODEGEN_REVISION)" || (echo "Evaluated candidate image does not match CODEGEN_REVISION=$(CODEGEN_REVISION)" >&2; exit 1)
	@test "$$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.role" }}' "$(CODEGEN_EVALUATED_SANDBOX_IMAGE)")" = "candidate" || (echo "Evaluated candidate image has the wrong role" >&2; exit 1)
	@test -n "$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" || (echo "Missing evaluated egress proxy identity: $(CODEGEN_EVALUATION_ARTIFACT_DIR)/egress-proxy-image-id.txt" >&2; exit 1)
	@docker image inspect "$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" >/dev/null || (echo "Missing evaluated egress proxy image: $(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" >&2; exit 1)
	@test "$$(docker image inspect --format '{{.Id}}' "$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)")" = "$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" || (echo "Egress proxy reference is not its immutable local image ID" >&2; exit 1)
	@test "$$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.egress.role" }}' "$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)")" = "proxy" || (echo "Evaluated egress proxy image has the wrong role" >&2; exit 1)
	@test "$$(docker image inspect --format '{{ index .Config.Labels "dev.apdl.codegen.egress.policy-sha256" }}' "$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)")" = "$(CODEGEN_EGRESS_POLICY_SHA256)" || (echo "Evaluated egress proxy image does not match the shipped policy" >&2; exit 1)
	@test -n "$(CODEGEN_EGRESS_SOCKET_VOLUME)" || (echo "CODEGEN_EGRESS_SOCKET_VOLUME must name the reviewed proxy socket volume" >&2; exit 1)
	@case "$(CODEGEN_EGRESS_SOCKET_VOLUME)" in *[!A-Za-z0-9_.-]*|'') echo "CODEGEN_EGRESS_SOCKET_VOLUME is not a canonical Docker volume name" >&2; exit 1;; esac
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_ROLLOUT_STAGE=reviewed_pr \
	CODEGEN_ROLLOUT_BUNDLE_PATH="$(CODEGEN_ROLLOUT_BUNDLE_PATH)" \
	CODEGEN_CONTROLLER_IMAGE="$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" \
	CODEGEN_EGRESS_SOCKET_VOLUME="$(CODEGEN_EGRESS_SOCKET_VOLUME)" \
	CODEGEN_EGRESS_POLICY_SHA256="$(CODEGEN_EGRESS_POLICY_SHA256)" \
	CODEGEN_EGRESS_PROXY_IMAGE="$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	CODEGEN_DOCKER_UID="$(CODEGEN_DOCKER_UID)" \
	CODEGEN_DOCKER_GID="$(CODEGEN_DOCKER_GID)" \
	CODEGEN_DOCKER_SOCKET_GID="$(CODEGEN_DOCKER_SOCKET_GID)" \
	$(COMPOSE) -f $(CODEGEN_EGRESS_COMPOSE_FILE) -f $(CODEGEN_ROLLOUT_COMPOSE_FILE) config --quiet

codegen-reviewed-up: codegen-reviewed-config
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_ROLLOUT_STAGE=reviewed_pr \
	CODEGEN_ROLLOUT_BUNDLE_PATH="$(CODEGEN_ROLLOUT_BUNDLE_PATH)" \
	CODEGEN_CONTROLLER_IMAGE="$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" \
	CODEGEN_EGRESS_SOCKET_VOLUME="$(CODEGEN_EGRESS_SOCKET_VOLUME)" \
	CODEGEN_EGRESS_POLICY_SHA256="$(CODEGEN_EGRESS_POLICY_SHA256)" \
	CODEGEN_EGRESS_PROXY_IMAGE="$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	CODEGEN_DOCKER_UID="$(CODEGEN_DOCKER_UID)" \
	CODEGEN_DOCKER_GID="$(CODEGEN_DOCKER_GID)" \
	CODEGEN_DOCKER_SOCKET_GID="$(CODEGEN_DOCKER_SOCKET_GID)" \
	$(COMPOSE) -f $(CODEGEN_EGRESS_COMPOSE_FILE) -f $(CODEGEN_ROLLOUT_COMPOSE_FILE) up -d --no-build --no-deps --force-recreate --wait codegen-egress-proxy
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_ROLLOUT_STAGE=reviewed_pr \
	CODEGEN_ROLLOUT_BUNDLE_PATH="$(CODEGEN_ROLLOUT_BUNDLE_PATH)" \
	CODEGEN_CONTROLLER_IMAGE="$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" \
	CODEGEN_EGRESS_SOCKET_VOLUME="$(CODEGEN_EGRESS_SOCKET_VOLUME)" \
	CODEGEN_EGRESS_POLICY_SHA256="$(CODEGEN_EGRESS_POLICY_SHA256)" \
	CODEGEN_EGRESS_PROXY_IMAGE="$(CODEGEN_EVALUATED_EGRESS_PROXY_IMAGE)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	CODEGEN_DOCKER_UID="$(CODEGEN_DOCKER_UID)" \
	CODEGEN_DOCKER_GID="$(CODEGEN_DOCKER_GID)" \
	CODEGEN_DOCKER_SOCKET_GID="$(CODEGEN_DOCKER_SOCKET_GID)" \
	$(COMPOSE) -f $(CODEGEN_EGRESS_COMPOSE_FILE) -f $(CODEGEN_ROLLOUT_COMPOSE_FILE) up -d --no-build --no-deps --force-recreate codegen

grant-codegen-repository:
	cd services/codegen && .venv/bin/python -m app.github.grant_cli $(ARGS)

revoke-codegen-repository:
	cd services/codegen && .venv/bin/python -m app.github.revoke_grant_cli $(ARGS)

# ─── Pipeline ────────────────────────────────────────────────

run-pipeline:
	cd pipeline/redis && $(if $(SERVICE_ENV_FILE),uv run --no-project $(SERVICE_ENV_FILE) -- .venv/bin/python,.venv/bin/python) clickhouse_writer.py

test-writer:
	cd pipeline/redis && .venv/bin/python -m pytest -q

lint-writer:
	cd pipeline/redis && .venv/bin/ruff check clickhouse_writer.py tests/

migrate-clickhouse:
	@CLICKHOUSE_COMPOSE_FILE="$(CLICKHOUSE_COMPOSE_FILE)" scripts/init-clickhouse.sh

migrate-postgres:
	@POSTGRES_COMPOSE_FILE="$(POSTGRES_COMPOSE_FILE)" scripts/init-postgres.sh

# ─── Docker ──────────────────────────────────────────────────

dev:
	docker compose -f infra/docker/docker-compose.deps.yml up -d
	@$(MAKE) --no-print-directory migrate-clickhouse CLICKHOUSE_COMPOSE_FILE=infra/docker/docker-compose.deps.yml
	@$(MAKE) --no-print-directory migrate-postgres POSTGRES_COMPOSE_FILE=infra/docker/docker-compose.deps.yml
	@echo "==> Dependencies running (Redis, ClickHouse, PostgreSQL)"
	@echo "    Run services individually: make run-ingestion, make run-config, make run-query, make run-agents, make run-codegen, make run-pipeline"

dev-core:
	$(COMPOSE) --profile agents --profile codegen stop -t 30 \
		ingestion config query agents codegen clickhouse-writer admin-api admin gateway
	$(COMPOSE) --profile agents --profile codegen rm -f -s agents codegen
	$(COMPOSE) up -d --build redis clickhouse postgres
	@$(MAKE) --no-print-directory migrate-clickhouse CLICKHOUSE_COMPOSE_FILE=$(COMPOSE_FILE)
	@$(MAKE) --no-print-directory migrate-postgres POSTGRES_COMPOSE_FILE=$(COMPOSE_FILE)
	$(COMPOSE) up -d --build --wait --wait-timeout 120 \
		ingestion config query clickhouse-writer admin-api admin gateway
	@echo "==> Core development stack is ready"
	@echo "    Agents and Codegen are stopped; run make dev-all to opt into their offline services."

dev-all: dev-core
	$(COMPOSE) --profile agents --profile codegen up -d --build --wait --wait-timeout 120 \
		agents codegen
	@echo "==> Full development stack is ready"
	@echo "    Agents is enabled; Codegen publication remains offline (no Docker socket mounted)."

dev-down:
	$(COMPOSE) --profile agents --profile codegen down
	docker compose -f infra/docker/docker-compose.deps.yml down
	@docker network rm "$(CODEGEN_DEVELOPMENT_SANDBOX_NETWORK)" >/dev/null 2>&1 || true

smoke-fresh:
	@bash scripts/smoke_fresh_install.sh core

smoke-experiment-fresh:
	@bash scripts/smoke_fresh_install.sh experiment

test-clickhouse-upgrade:
	@bash scripts/test_clickhouse_upgrade.sh

# Container status + service health endpoints.
status:
	@bash scripts/dev.sh status

# End-to-end smoke test against the running stack (event → flag → query).
smoke:
	@bash scripts/dev.sh smoke

# ─── CI ──────────────────────────────────────────────────────

ci: check build verify-release test-packed-sdk-contract test-packed-python-sdk audit-dependencies
