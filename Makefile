# Rosetta: one entry point for the things you do every day.
#
#   make setup     install the Python and web dependencies
#   make run       local mode, no infrastructure, http://127.0.0.1:8765
#   make up        production mode with Docker Compose, http://localhost:8080
#   make test      every test that needs no Docker
#
# `make help` lists all targets.
#
# Ports: 8000 is taken on the development machine, so `run` uses 8765 and the
# Compose stack publishes the API on 8080.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PY      ?= .venv/bin/python
PORT    ?= 8765
NPM     ?= npm
# The Docker CLI is not on the PATH of every machine that has Docker Desktop.
DOCKER  ?= $(shell command -v docker 2>/dev/null || echo /Applications/Docker.app/Contents/Resources/bin/docker)
COMPOSE ?= $(DOCKER) compose
SBOM    ?= docs/sbom.txt
COVERAGE_MIN ?= 80

.PHONY: help setup run up up-mtls down logs test test-unit test-integration test-infra pact pact-consumer pact-provider bdd chaos coverage \
        lint security bench bench-burst load train web certs sbom helm-sync infra-check infra-verify clean distclean

help: ## list the targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-18s %s\n", $$1, $$2}'

# --------------------------------------------------------------------- setup
.venv/bin/python:
	python3.11 -m venv .venv

setup: .venv/bin/python ## create the virtual environment, install Python and web dependencies
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	$(NPM) --prefix web ci --no-audit --no-fund

# ----------------------------------------------------------------------- run
run: ## local mode: file-backed broker, SQLite, Parquet on disk. No infrastructure
	$(PY) -m rosetta up --port $(PORT)

.env:
	@echo "No .env file. Create one first:"
	@echo "    cp .env.example .env     and replace the placeholders"
	@exit 1

up: .env ## production mode: the whole stack with Docker Compose
	$(COMPOSE) up --build --detach
	@echo "Rosetta     http://localhost:$${API_PORT:-8080}"
	@echo "Grafana     http://localhost:$${GRAFANA_PORT:-3000}"
	@echo "Prometheus  http://localhost:$${PROMETHEUS_PORT:-9090}"
	@echo "MinIO       http://localhost:$${MINIO_CONSOLE_PORT:-9001}"

up-mtls: .env certs ## the same stack, MQTT with client certificates
	$(COMPOSE) -f docker-compose.yml -f infra/emqx/docker-compose.mtls.yml up --build --detach

down: .env ## stop the Compose stack. Volumes are kept
	$(COMPOSE) -f docker-compose.yml -f infra/emqx/docker-compose.mtls.yml down --remove-orphans

logs: ## follow the logs of the Rosetta services
	$(COMPOSE) logs --follow --tail 100 api gateway normalizer processor dlq simulator

# --------------------------------------------------------------------- tests
test: ## every pytest suite that needs no Docker, then BDD
	$(PY) -m pytest tests -m "not infra"
	$(PY) -m behave tests/bdd

test-unit: ## unit tests
	$(PY) -m pytest tests/unit

test-integration: ## integration and contract tests with the local adapters
	$(PY) -m pytest tests/integration tests/contract -m "not infra"

test-infra: ## production adapters against real Kafka, PostgreSQL and Redis. Needs Docker
	$(PY) -m pytest tests/integration -m infra -rs

# Pact: the consumers write the contracts (tests/contract/pacts), the providers
# are verified against them. Offline, no Pact Broker.
pact-consumer: ## write the pacts: web console (pact-js) and pipeline consumers (pact-python)
	$(NPM) --prefix web run test:pact
	$(PY) -m pytest -q tests/contract/pact -k consumer

pact-provider: ## verify the API, the normaliser and the processor against the pacts
	$(PY) -m pytest -q tests/contract/pact -k provider

pact: pact-consumer pact-provider ## both sides of every contract

bdd: ## behaviour tests (behave)
	$(PY) -m behave tests/bdd

chaos: ## kill pipeline processes under load and check that nothing is lost
	$(PY) tests/chaos/kill_and_recover.py

# Measured over every suite that needs no Docker. The unit tests alone cover about
# half of the code: adapters, API and pipeline are covered by the integration tests.
coverage: ## all tests that need no Docker, with coverage. Fails below 80 percent
	$(PY) -m pytest tests -m "not infra" --cov=rosetta --cov-report=term-missing:skip-covered \
		--cov-report=xml:coverage.xml --cov-report=html:htmlcov --cov-fail-under=$(COVERAGE_MIN)
	@echo "HTML report: htmlcov/index.html"

# ------------------------------------------------------------------- quality
lint: ## ruff
	$(PY) -m ruff check rosetta tests scripts infra

security: ## bandit, pip-audit, npm audit, security tests. Semgrep and Trivy when installed
	$(PY) -m bandit -c pyproject.toml -r rosetta
	$(PY) -m pip_audit --desc
	$(NPM) --prefix web audit --audit-level=high
	$(PY) -m pytest tests/security
	@if command -v semgrep > /dev/null; then \
		semgrep scan --config p/python --config p/security-audit --config p/secrets --config p/dockerfile --metrics off --error --timeout 120 \
			--exclude web/node_modules --exclude web/dist --exclude tests --exclude design; \
	else echo "semgrep is not installed: skipped (it runs in CI)"; fi
	@if command -v trivy > /dev/null; then \
		trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
			--skip-dirs web/node_modules --skip-dirs design --skip-dirs .venv .; \
	else echo "trivy is not installed: skipped (it runs in CI)"; fi

# --------------------------------------------------------------- performance
bench: ## pipeline throughput as separate processes, report in docs/evidence
	$(PY) scripts/bench_pipeline.py --seconds 60

# 3x for five minutes, as the problem statement asks. Needs a quiet machine and
# about 10 GB free: the file-backed log keeps up to 450 MB per partition while the
# backlog builds. Stop other workloads first; the result is only as good as the host.
bench-burst: ## 3x burst for 5 minutes at 100,000 vehicles, then drain and account
	ROSETTA_LOG_RETENTION_MB=450 $(PY) -u scripts/bench_pipeline.py --seconds 480 --burst-hz 3 --burst-seconds 300 \
		--shards 4 --normalizers 5 --processors 2 --label burst_5min --drain-max 900

load: ## API load test with locust. Start `make run` in another terminal first
	$(PY) -m locust -f tests/load/locustfile.py --headless -u 60 -r 20 -t 60s \
		--host http://127.0.0.1:$(PORT) --only-summary

# --------------------------------------------------------------------- build
train: ## train and evaluate the field-mapping model
	$(PY) -m rosetta train

web: ## build the web UI into web/dist
	@if [ ! -d web/node_modules ]; then $(NPM) --prefix web ci --no-audit --no-fund; fi
	$(NPM) --prefix web run build

certs: ## demo CA, server and client certificates for MQTT mTLS, into infra/emqx/certs
	scripts/make_certs.sh gateway simulator

sbom: ## list of installed Python packages with versions, into docs/sbom.txt
	@mkdir -p $(dir $(SBOM))
	@{ \
		echo "# Rosetta software bill of materials (Python)"; \
		echo "# $$($(PY) --version), $$($(PY) -m pip --version | cut -d' ' -f1-2)"; \
		echo "# Every package importable by the interpreter, name==version. Produced offline by 'make sbom'."; \
		echo "# It lists what is installed, which is more than what pyproject.toml asks for."; \
		$(PY) -m pip list --format=freeze --disable-pip-version-check; \
	} > $(SBOM)
	@echo "$(SBOM): $$(grep -vc '^#' $(SBOM)) packages"

helm-sync: ## copy infra/sql/postgres_extras.sql into the Helm chart
	cp infra/sql/postgres_extras.sql infra/helm/rosetta/files/postgres_extras.sql

infra-check: ## the checks of infrastructure files that need no running Docker daemon
	cmp infra/sql/postgres_extras.sql infra/helm/rosetta/files/postgres_extras.sql
	sh -n infra/docker/entrypoint.sh
	bash -n scripts/make_certs.sh
	terraform fmt -check -recursive infra/terraform
	@if [ -e .env ]; then \
		$(COMPOSE) -f docker-compose.yml config --quiet && \
		$(COMPOSE) -f docker-compose.yml -f infra/emqx/docker-compose.mtls.yml config --quiet; \
	else \
		cp .env.example .env; \
		trap 'rm -f .env' EXIT; \
		$(COMPOSE) -f docker-compose.yml config --quiet && \
		$(COMPOSE) -f docker-compose.yml -f infra/emqx/docker-compose.mtls.yml config --quiet; \
	fi
	@echo "infrastructure files: ok"

# The same checks with the real tools, run from their official images: needs Docker.
HELM_IMAGE        ?= alpine/helm:3.18.4
KUBECONFORM_IMAGE ?= ghcr.io/yannh/kubeconform:v0.7.0
TERRAFORM_IMAGE   ?= hashicorp/terraform:1.13.3
infra-verify: ## helm lint and template, kubeconform, terraform init and validate. Needs Docker
	docker run --rm -v "$(CURDIR)/infra/helm:/charts" $(HELM_IMAGE) lint /charts/rosetta
	docker run --rm -v "$(CURDIR)/infra/helm:/charts" $(HELM_IMAGE) template rosetta /charts/rosetta \
		--set serviceMonitor.enabled=true --set serviceMonitor.bearerTokenKey=ROSETTA_METRICS_TOKEN \
		--set ingress.enabled=true \
	| docker run --rm -i $(KUBECONFORM_IMAGE) -strict -summary -ignore-missing-schemas -kubernetes-version 1.33.0 -
	@tmp=$$(mktemp -d) && cp -R infra/terraform/. $$tmp && \
	docker run --rm -v "$$tmp:/tf" -w /tf $(TERRAFORM_IMAGE) init -backend=false -input=false -no-color > /dev/null && \
	docker run --rm -v "$$tmp:/tf" -w /tf $(TERRAFORM_IMAGE) validate -no-color && rm -rf $$tmp

# --------------------------------------------------------------------- clean
clean: ## remove caches, coverage and test reports. Data and the built UI are kept
	find . -name __pycache__ -type d -not -path "./.venv/*" -not -path "./web/node_modules/*" -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov reports build dist *.egg-info

distclean: clean ## also remove local data, the built UI and the demo certificates
	rm -rf data web/dist infra/emqx/certs
