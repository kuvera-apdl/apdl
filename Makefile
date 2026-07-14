.PHONY: all setup deps build test clean lint check fmt fmt-check dev dev-all dev-down install-hooks lint-staged migrate-clickhouse migrate-postgres test-sdk-python lint-sdk-python setup-sdk release-sdk status smoke run-admin build-admin test-admin lint-admin clean-admin run-admin-api test-admin-api lint-admin-api create-admin-user test-writer lint-writer build-codegen-controller build-codegen-sandbox build-codegen-runtime evaluate-codegen codegen-reviewed-config codegen-reviewed-up grant-codegen-repository revoke-codegen-repository

# ─── Top-Level ───────────────────────────────────────────────

all: build

CLICKHOUSE_COMPOSE_FILE ?= infra/docker/docker-compose.deps.yml
POSTGRES_COMPOSE_FILE ?= infra/docker/docker-compose.deps.yml

# Full-stack Compose command. `-f infra/docker/docker-compose.yml` makes Compose
# use that folder as its project dir (and its default `.env` lookup), so the
# repo-root `.env` is otherwise ignored — load it explicitly when it exists.
COMPOSE_FILE ?= infra/docker/docker-compose.yml
COMPOSE := docker compose $(if $(wildcard .env),--env-file .env,) -f $(COMPOSE_FILE)

# One immutable identity binds the evaluation controller, production candidate,
# evidence bundle, and reviewed-PR deployment. Environment values may override
# these defaults, but the evaluation script rejects development-unversioned.
CODEGEN_REVISION ?= $(shell git rev-parse HEAD 2>/dev/null)
CODEGEN_MODEL ?= claude-opus-4-8
CODEGEN_EVALUATION_CONTROLLER_IMAGE ?= apdl-codegen-evaluation-controller:$(CODEGEN_REVISION)
CODEGEN_SANDBOX_IMAGE ?= apdl-codegen-sandbox:$(CODEGEN_REVISION)
CODEGEN_EVALUATION_ARTIFACT_DIR ?= $(CURDIR)/local-files/codegen-rollouts/$(CODEGEN_REVISION)
CODEGEN_ROLLOUT_POLICY ?= $(CURDIR)/services/codegen/app/evaluations/rollout_policy_v3.json
CODEGEN_ROLLOUT_BUNDLE_PATH ?= $(CODEGEN_EVALUATION_ARTIFACT_DIR)/publication-bundle.json
CODEGEN_EVALUATED_CONTROLLER_IMAGE ?= $(shell test -s "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/controller-image-id.txt" && cat "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/controller-image-id.txt")
CODEGEN_EVALUATED_SANDBOX_IMAGE ?= $(shell test -s "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/candidate-image-id.txt" && cat "$(CODEGEN_EVALUATION_ARTIFACT_DIR)/candidate-image-id.txt")
CODEGEN_ROLLOUT_COMPOSE_FILE ?= infra/docker/docker-compose.codegen-rollout.yml
CODEGEN_DOCKER_SOCKET ?= $(if $(wildcard $(HOME)/.docker/run/docker.sock),$(HOME)/.docker/run/docker.sock,/var/run/docker.sock)
CODEGEN_DOCKER_UID ?= $(shell id -u)
CODEGEN_DOCKER_GID ?= $(shell id -g)
CODEGEN_DOCKER_SOCKET_GID ?= $(shell stat -c '%g' "$(CODEGEN_DOCKER_SOCKET)" 2>/dev/null || stat -f '%g' "$(CODEGEN_DOCKER_SOCKET)" 2>/dev/null || echo 0)

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
	@echo "==> Setting up ETL framework"
	cd pipeline/etl && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Python SDK"
	cd sdk/python && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python

build: build-sdk build-admin

test: test-sdk test-sdk-python test-ingestion test-config test-query test-agents test-codegen test-writer test-etl test-admin-api test-admin

lint: lint-sdk lint-sdk-python lint-ingestion lint-config lint-query lint-agents lint-codegen lint-writer lint-etl lint-admin-api lint-admin

clean: clean-sdk clean-admin

# Parallel local CI mirror: lint + test every package at once.
check:
	@bash scripts/check.sh

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
	cd services/admin-api && APDL_ADMIN_COOKIE_SECURE=false .venv/bin/uvicorn app.main:app --reload --port 8085 --env-file ../../.env

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
	cd services/ingestion && .venv/bin/uvicorn app.main:app --reload --port 8080

# ─── Config Service (Python) ────────────────────────────────

test-config:
	cd services/config && .venv/bin/python -m pytest -v

lint-config:
	cd services/config && .venv/bin/ruff check app/

run-config:
	cd services/config && .venv/bin/uvicorn app.main:app --reload --port 8081

# ─── Query Service (Python) ──────────────────────────────────

test-query:
	cd services/query && .venv/bin/python -m pytest -v

lint-query:
	cd services/query && .venv/bin/ruff check app/

run-query:
	cd services/query && .venv/bin/uvicorn app.main:app --reload --port 8082

# ─── Agents Service (Python) ─────────────────────────────────

test-agents:
	cd services/agents && .venv/bin/python -m pytest -v

lint-agents:
	cd services/agents && .venv/bin/ruff check app/

run-agents:
	cd services/agents && .venv/bin/uvicorn app.main:app --reload --port 8083

# ─── Codegen Service (Python) ────────────────────────────────

test-codegen:
	cd services/codegen && .venv/bin/python -m pytest -v

lint-codegen:
	cd services/codegen && .venv/bin/ruff check app/

run-codegen:
	cd services/codegen && .venv/bin/uvicorn app.main:app --reload --port 8084

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

build-codegen-runtime: build-codegen-controller build-codegen-sandbox

evaluate-codegen:
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_EVALUATION_CONTROLLER_IMAGE="$(CODEGEN_EVALUATION_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_SANDBOX_IMAGE)" \
	CODEGEN_EVALUATION_ARTIFACT_DIR="$(CODEGEN_EVALUATION_ARTIFACT_DIR)" \
	CODEGEN_ROLLOUT_POLICY="$(CODEGEN_ROLLOUT_POLICY)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	./scripts/evaluate-codegen.sh

codegen-reviewed-config:
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
	@test -n "$(CODEGEN_SANDBOX_NETWORK)" || (echo "CODEGEN_SANDBOX_NETWORK must name an operator-managed egress-filtered Docker network" >&2; exit 1)
	@case "$(CODEGEN_SANDBOX_NETWORK)" in bridge|default|host|none) echo "CODEGEN_SANDBOX_NETWORK cannot use a built-in Docker network" >&2; exit 1;; esac
	@docker network inspect "$(CODEGEN_SANDBOX_NETWORK)" >/dev/null || (echo "Missing sandbox network: $(CODEGEN_SANDBOX_NETWORK)" >&2; exit 1)
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_ROLLOUT_STAGE=reviewed_pr \
	CODEGEN_ROLLOUT_BUNDLE_PATH="$(CODEGEN_ROLLOUT_BUNDLE_PATH)" \
	CODEGEN_CONTROLLER_IMAGE="$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" \
	CODEGEN_SANDBOX_NETWORK="$(CODEGEN_SANDBOX_NETWORK)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	CODEGEN_DOCKER_UID="$(CODEGEN_DOCKER_UID)" \
	CODEGEN_DOCKER_GID="$(CODEGEN_DOCKER_GID)" \
	CODEGEN_DOCKER_SOCKET_GID="$(CODEGEN_DOCKER_SOCKET_GID)" \
	$(COMPOSE) -f $(CODEGEN_ROLLOUT_COMPOSE_FILE) config --quiet

codegen-reviewed-up: codegen-reviewed-config
	CODEGEN_REVISION="$(CODEGEN_REVISION)" \
	CODEGEN_MODEL="$(CODEGEN_MODEL)" \
	CODEGEN_ROLLOUT_STAGE=reviewed_pr \
	CODEGEN_ROLLOUT_BUNDLE_PATH="$(CODEGEN_ROLLOUT_BUNDLE_PATH)" \
	CODEGEN_CONTROLLER_IMAGE="$(CODEGEN_EVALUATED_CONTROLLER_IMAGE)" \
	CODEGEN_SANDBOX_IMAGE="$(CODEGEN_EVALUATED_SANDBOX_IMAGE)" \
	CODEGEN_SANDBOX_NETWORK="$(CODEGEN_SANDBOX_NETWORK)" \
	CODEGEN_DOCKER_SOCKET="$(CODEGEN_DOCKER_SOCKET)" \
	CODEGEN_DOCKER_UID="$(CODEGEN_DOCKER_UID)" \
	CODEGEN_DOCKER_GID="$(CODEGEN_DOCKER_GID)" \
	CODEGEN_DOCKER_SOCKET_GID="$(CODEGEN_DOCKER_SOCKET_GID)" \
	$(COMPOSE) -f $(CODEGEN_ROLLOUT_COMPOSE_FILE) up -d --no-build --no-deps --force-recreate codegen

grant-codegen-repository:
	cd services/codegen && .venv/bin/python -m app.github.grant_cli $(ARGS)

revoke-codegen-repository:
	cd services/codegen && .venv/bin/python -m app.github.revoke_grant_cli $(ARGS)

# ─── Pipeline ────────────────────────────────────────────────

run-pipeline:
	cd pipeline/redis && .venv/bin/python clickhouse_writer.py

test-writer:
	cd pipeline/redis && .venv/bin/python -m pytest -q

lint-writer:
	cd pipeline/redis && .venv/bin/ruff check clickhouse_writer.py tests/

test-etl:
	cd pipeline/etl && .venv/bin/python -m pytest -v

lint-etl:
	cd pipeline/etl && .venv/bin/ruff check etl/ scripts/ tests/

new-transform:
	cd pipeline/etl && .venv/bin/python scripts/new_transform.py $(ARGS)

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

dev-all:
	$(COMPOSE) up -d --build redis clickhouse postgres
	@$(MAKE) --no-print-directory migrate-clickhouse CLICKHOUSE_COMPOSE_FILE=$(COMPOSE_FILE)
	@$(MAKE) --no-print-directory migrate-postgres POSTGRES_COMPOSE_FILE=$(COMPOSE_FILE)
	$(COMPOSE) up --build ingestion config query agents codegen clickhouse-writer admin-api admin gateway

dev-down:
	$(COMPOSE) down
	docker compose -f infra/docker/docker-compose.deps.yml down

# Container status + service health endpoints.
status:
	@bash scripts/dev.sh status

# End-to-end smoke test against the running stack (event → flag → query).
smoke:
	@bash scripts/dev.sh smoke

# ─── CI ──────────────────────────────────────────────────────

ci: lint-sdk test-sdk lint-sdk-python test-sdk-python lint-ingestion lint-config lint-query lint-agents lint-codegen lint-writer test-writer lint-etl test-etl lint-admin-api test-admin-api lint-admin test-admin
