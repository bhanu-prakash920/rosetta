# What was verified, and what was not

The deployment files were first written on a machine with no Docker daemon, so
the first pass (section 2) could only check them statically. Docker became
available later, and the stack was then **built and run for real** (section 1).
Running it found seven bugs that no static check could have found; they are
fixed and each has a test (section 1, last table).

Rule used here: a file is called "checked" only for the property that was
checked. A Compose file that parses is a Compose file that parses, not a stack
that works.

Date of the checks: 2026-09-30. Machine: Apple M4, 10 cores, Docker Desktop
29.8.1. Evidence files are in `docs/evidence/`.

## 1. Run for real

| # | What | How | Result |
|---|---|---|---|
| R1 | the image builds | `docker build -t rosetta:dev .` | builds. Dependencies are a separate layer, so a code change rebuilds in seconds |
| R2 | the whole stack starts from nothing with one command | `docker compose down -v && make up` | 20 services, 21 containers with two normalisers: 16 long-running, all healthy in 108 s, and 4 one-shot jobs (`minio-init`, `migrate`, `db-extras`, `seed`) that exit 0. 100,000 vehicles, TimescaleDB 2.30.1, pgvector 0.8.6, one hypertable. `docs/evidence/compose_stack.json` |
| R3 | data flows end to end over the production adapters | simulator over MQTT (EMQX) -> gateway -> Kafka -> 2 normalisers -> processor -> Redis, TimescaleDB, MinIO | about 4,500 events/s (the Compose simulator is capped at 5,000), consumer lag near 0, 1.7 M rows in the hypertable within minutes, no worker restart |
| R4 | no message is lost when the gateway is restarted or killed | pause, drain, count; resume; `docker compose restart gateway`; `docker kill --signal KILL`; pause, drain, count | 615,464 sent, 615,464 received by the normalisers. EMQX dropped 0. `docs/evidence/compose_mqtt_restart.json` |
| R5 | Prometheus scrapes with the bearer token, alert rules load | Prometheus API, `promtool check rules`, `promtool check config` | target up, 12 rules, both checks pass. `/metrics` answers 401 without the token |
| R6 | logs are centralised | Alloy -> Loki, queried through Grafana | lines from every container of the project, with level, logger and trace id. `docs/screenshots/grafana_logs.png` |
| R7 | traces join across processes | Tempo search through Grafana | `gateway.submit` -> `normalizer.batch` -> `processor.batch` in one trace; API request spans as `rosetta-api` |
| R8 | the Grafana dashboard renders with data | headless Chrome | `docs/screenshots/grafana_pipeline.png` |
| R9 | the Helm chart | `helm lint` (Helm 3.18.4, in Docker), `helm template` with ServiceMonitor, token and Ingress on, then kubeconform 0.7.0 `-strict` against Kubernetes 1.33 | lint: 0 failed. 21 objects: 20 valid, 1 skipped (ServiceMonitor, a CRD with no built-in schema) |
| R10 | Terraform | `terraform init -backend=false && terraform validate` (Terraform 1.13.3, in Docker), `terraform fmt -check -recursive` | valid, formatted |
| R11 | the CI workflow | actionlint 1.7.7 (with shellcheck); every `uses:` pin compared with `git ls-remote` of its tag | clean; 11 actions, every commit matches its tag |
| R12 | production adapters against real servers | `make test-infra` (Testcontainers: PostgreSQL 16, pgvector, TimescaleDB, Redis 7, Kafka 3.9) | 13 passed. The TimescaleDB test is new and runs `postgres_extras.sql` twice |
| R13 | security scans | bandit, Semgrep, pip-audit, npm audit, Trivy (file system and image), OWASP ZAP baseline and API scan | reports and triage in `docs/evidence/security/README.md` |
| R14 | contracts between services | Pact: web console -> API (pact-js), normaliser -> processor, normaliser -> dead-letter worker, processor -> alert subscribers (pact-python), providers verified | `make pact`, pact files in `tests/contract/pacts/` |
| R15 | the CI workflow on GitHub | pushed to GitHub Actions and the run read back (`gh run view`) | every job green on ubuntu-24.04: lint, unit, coverage gate, integration and Pact, BDD, chaos, front end, security, infrastructure, image, ZAP. The load test is manual and stays skipped. Run 36885803294 |

Bugs found by running it, all fixed:

| Bug | Symptom | Fix | Test |
|---|---|---|---|
| TimescaleDB sink formatted timestamps with `%f`, which Arrow does not have | every batch rejected by PostgreSQL; the aborted transaction then failed every later batch | the CSV writer formats timestamps itself; rollback on error | `tests/unit/test_timescale_csv.py`, `test_telemetry_sink_writes_a_hypertable_idempotently` |
| PostgreSQL health check used the Unix socket | on first start `migrate` ran while the image's temporary init server was up, and failed | `pg_isready -h 127.0.0.1` | R2 from empty volumes |
| MQTT gateway: a new client id per restart and 2 h sessions | offline members stayed in the shared group and received a share of the traffic, which they dropped (1.4 M in one session). About 2.8% of events missing in the R4 procedure | stable client id, 5 min session expiry, messages acknowledged only after they are in Kafka, session kept on shutdown | `tests/unit/test_mqtt_gateway.py`, R4 |
| EMQX defaults once acknowledgements wait for Kafka | 32 in flight capped the gateway near 640/s; a 1,000-message mailbox limit disconnected it | `max_inflight` 4096, `max_mqueue_len` 200,000, mailbox 100,000, heap 512 MB | R3, R4 |
| Kafka commit during a consumer-group rebalance raised | two normalisers starting together crashed each other in a loop | rebalance errors defer the commit; revoked partitions are forgotten, never committed | `tests/unit/test_kafka_consumer.py`, R2 |
| Prometheus replaced the metrics' `service` label with the target's | per-service alerts and two dashboard panels matched nothing | `honor_labels: true` (and `honorLabels` in the ServiceMonitor) | R5 |
| OpenTelemetry metrics sent to Tempo | an error every export interval | `OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none` | R6 |

## 2. Static checks from the first pass

| # | What | How | Result |
|---|---|---|---|
| 1 | `docker-compose.yml` parses and interpolates | `cp .env.example .env; docker compose -f docker-compose.yml config -q; rm .env` | exit 0 |
| 2 | the mTLS overlay merges with it | `docker compose -f docker-compose.yml -f infra/emqx/docker-compose.mtls.yml config -q` | exit 0 |
| 3 | Compose refuses to start without secrets | `docker compose config -q` with no `.env` | exit 1, one message per missing variable |
| 4 | every published port is bound to 127.0.0.1, no image uses `latest`, the dependency conditions are as intended | script over the output of `docker compose config` | 4 ports (8080, 3000, 9090, 9001), all on 127.0.0.1. 17 services |
| 5 | every pinned image tag exists | Docker Hub API, one request per tag | HTTP 200 for all eight images |
| 6 | `infra/terraform` parses and is formatted | `terraform fmt -check -recursive infra/terraform` (Terraform 1.15.8) | exit 0 |
| 7 | Terraform references are consistent: variables declared and used, module arguments, required variables passed, outputs, resource and data names | a script with regular expressions, not Terraform | no finding |
| 8 | the `telemetry` columns equal `COLUMNS` in `rosetta/adapters/timescale.py`, in order | script that imports `COLUMNS` and parses the SQL | equal, 18 columns |
| 9 | `vector(304)` equals `DIM` in `rosetta/agent/memory.py` | `.venv/bin/python -c "from rosetta.agent.memory import DIM; print(DIM)"` | 304 (48 + 256) |
| 10 | the metric names and label values in `alerts.yml` and in the Grafana dashboard are the ones the code emits | script that feeds snapshots to `Aggregator`, calls `prometheus()` and compares. Also read from `/metrics` of a running local instance | 5 metric names, all matched |
| 11 | `prometheus.yml`, `alerts.yml` and the Grafana provisioning files are valid YAML, the dashboard is valid JSON with unique panel ids and no overlapping panels | Python `yaml` and `json` | ok |
| 12 | `infra/docker/entrypoint.sh` maps every role to the right command | run under `dash` with stub `python` and `uvicorn` executables | 13 cases, all as expected |
| 13 | `infra/docker/healthcheck.py` | run against a fake HTTP server (status `ok`, status `degraded`, no server), against fresh and stale checkpoint files, and against the real API in local mode | exit codes as designed |
| 14 | `scripts/make_certs.sh` | run with LibreSSL 3.3.6 into a temporary directory, twice, and with an invalid name | CA, server and client certificates created, second run reuses the CA, invalid name refused |
| 15 | the certificates work for mutual TLS with the gateway's TLS settings | Python TLS server that requires a client certificate, client context built as in `mqtt_gateway.py` (CA file, TLS 1.3 minimum, host name check) | handshake TLSv1.3 with a certificate, refused without |
| 16 | key material cannot be committed | copy of the project's `.gitignore` in a scratch repository, `git check-ignore` | `*.pem`, `*.key` and `.env` are ignored |
| 17 | `infra/minio/create_bucket.py` fails cleanly when the store is unreachable | run against a closed port with `BUCKET_WAIT_S=5` | exit 1 after 6 s, clear message |
| 18 | the Helm templates produce well-formed YAML with consistent objects (superseded by the real `helm lint` and kubeconform in section 1) | **not Helm.** A stand-in written for this purpose that implements the subset of Go templates the chart uses. Default values and two variants | 20 objects. Selectors match labels, HPAs and PDBs point at existing Deployments, every container has resources, probes, read-only root file system, dropped capabilities. No Secret is created |
| 19 | the `wait-for-seed` init script of the chart | extracted from the rendered chart, run in local mode against an empty and a seeded database | waits and gives up on the empty one, exits 0 on the seeded one |
| 20 | the `migrate` command of the entrypoint | run in local mode | schema created |
| 21 | `.github/workflows/ci.yml` is valid YAML, every action is pinned to a commit, every input used exists | Python `yaml`. Commits resolved with `git ls-remote`. Inputs read from the `action.yml` of each pinned commit | 40 `uses`, all pinned. 10 distinct actions |
| 22 | Makefile | `make -n` for every target. For real: `help`, `run`, `up` without `.env`, `sbom` and `certs` into a temporary directory, `infra-check`, `test-unit`, `bdd`, `lint` | see below |
| 23 | `make run` | started on port 8765 with 2,000 vehicles, `GET /api/v1/system/health` | `status: ok`, 8 workers reporting, UI and `/metrics` served |

Results of the Makefile targets that were run:

| Target | Result |
|---|---|
| `make run` | works |
| `make bdd` | 21 scenarios passed |
| `make test-unit` | the target works. 8 of 2,261 tests failed at the time (in `test_baseline.py`, `test_profile.py`, `test_synth.py`) |
| `make lint` | **fails on this machine: ruff is not installed in the virtual environment.** `make setup` installs it |
| `make infra-check` | passes |
| `make sbom` | works, tested with `SBOM=` pointing to a temporary file |
| `make coverage` | not run as a target. The same pytest command measured 91% over all suites and 52% over the unit tests alone |

## 3. Still not verified, and what would verify it

| What | Why not | What would verify it |
|---|---|---|
| the chart installs on a cluster, hooks run in order, pods become ready | no Kubernetes cluster | `helm upgrade --install` on kind or a test cluster |
| the rendered manifests against a live API server, and NetworkPolicy enforcement | no cluster | `kubectl apply --dry-run=server`; a CNI that enforces policies |
| `terraform plan` and `apply` | no AWS account | `terraform plan` in a sandbox account |
| the mTLS overlay with real device certificates | not started in this pass | `make certs && make up-mtls`, then publish with a device certificate to its own topic (accepted) and another device's (refused) |
| alert rules fire when they should | no rule unit tests | `promtool test rules` with test files |
| Kafka and MSK with TLS or SASL | local Kafka is plain text | `ROSETTA_KAFKA_CFG_SECURITY_PROTOCOL=SASL_SSL` against MSK |

## 4. Decisions that differ from the brief

| Brief | What was done | Why |
|---|---|---|
| MinIO with a bucket-creation step | the image is `pgsty/minio`, a community fork, and the bucket is created by a Python script in the Rosetta image instead of `mc` | `minio/minio` and `minio/mc` are gone from Docker Hub (404) and Quay refuses anonymous pulls. `MINIO_IMAGE` in `.env` replaces the image |
| 80% threshold on the unit tests | the threshold is applied to all suites that need no Docker | the unit tests alone cover 52% |
| `depends_on` on health checks | MinIO has no health check. Services wait for `minio-init` to finish | a health check would depend on tools inside an image this project does not control |
| commands as in the service table | the image has an entrypoint that takes the role name (`api`, `normalizer`, ...) and adds a worker id. The commands of the table still work when passed as they are | replicas need distinct worker ids, see mismatch 4 |
| mTLS in the Compose stack | it is an overlay (`make up-mtls`) | certificates must not be in the repository, and `docker compose up` must work without a preparation step |

## 5. Mismatches between the application and a production deployment

Found by reading the code while writing the deployment files. The application
was changed since; the status of each is given in bold.

1. **`README.md` does not exist, and the package cannot be built without it.** **Fixed**: the README exists and the image builds (R1).
   `pyproject.toml` has `readme = "README.md"`. `pip install .` fails when the
   file is missing, so the Docker build and every CI job that installs the
   package fail until it exists.
2. **Seeding a fresh PostgreSQL fails unless the schema and the extras exist first.** **Resolved by design**: `python -m rosetta migrate` exists and Compose runs migrate, db-extras, seed in order (R2).
   `python -m rosetta seed` creates the tables and then writes the mapping
   memory with `UPDATE embedding SET vec = ...`. The column `vec` is created by
   `postgres_extras.sql`, which needs the table. Hence the three steps
   `migrate`, `db-extras`, `seed`. There is no `migrate` command in the
   application: the entrypoint calls `rosetta.db.session.init_db()` directly.
3. **Topics are created with replication factor 1.** **Fixed**: `ROSETTA_KAFKA_REPLICATION`. `make_broker` calls
   `KafkaBroker(st.kafka_bootstrap)` and the default is `replication=1`. There
   is no setting for it. The Helm chart creates the topics first.
4. **Worker identity.** **Fixed**: the entrypoint derives worker ids from the host name. The worker id comes from the command line. Replicas of
   a Deployment share a command line, so they would share the checkpoint file
   name, the Parquet writer id (`part-p0-<ms>.parquet`, two writers in the same
   millisecond would overwrite each other) and the metrics identity (the API
   keeps one entry per identity, so the lag of two normalisers with the same
   id is reported as the lag of one). The entrypoint derives the id from the
   host name.
5. **The gateway cannot be scaled.** **Fixed**: `ROSETTA_MQTT_CLIENT_ID`, one per replica; the host name by default. Its MQTT client id is `gateway-<pid>`, and
   the pid is the same in every container. Two replicas would take over each
   other's session. The simulator has the same pattern (`sim-<pid>`).
6. **The Kafka client has no security settings.** **Fixed**: any librdkafka setting as `ROSETTA_KAFKA_CFG_*`. Only `bootstrap.servers` is
   passed, so TLS and SASL are impossible. Managed Kafka with mandatory
   authentication cannot be used, and on MSK the plain-text listener has to
   stay open inside the VPC.
7. **The object store client always sends static keys.** **Fixed**: keys only when set, else the default credential chain. `ParquetArchive.s3`
   passes `access_key` and `secret_key` to pyarrow even when they are empty,
   which disables the default credential chain. Workload identity (IRSA and
   its equivalents) cannot be used.
8. **`GET /api/v1/system/health` always answers 200.** **Fixed**: 503 when a dependency is down; `/system/live` for liveness. With the database down
   it answers 200 and `status: degraded`. An HTTP probe cannot tell ready from
   not ready. The readiness probe runs a script that reads the body.
9. **The workers have no health endpoint.** **Open**, reduced: `rosetta_workers_reporting` now tells a stopped worker from an idle one. For the gateway, the processor and
   the dead-letter worker a probe can only tell that the process exists. A
   worker that hangs is not detected. The normaliser is probed through the age
   of its checkpoint.
10. **`/metrics` cannot show that the workers are gone.** **Fixed**: `rosetta_workers_reporting{service}` and the alert `RosettaWorkersGone`. The lag gauge is the
    sum over the workers that report, and the sum over none is 0. The number of
    reporting workers is in the health endpoint, not in `/metrics`. The alert
    `RosettaNoEventsNormalised` is the only net.
11. **`/metrics` needs no authentication** **Fixed**: `ROSETTA_METRICS_TOKEN`; Prometheus and the ServiceMonitor send it., and the Ingress forwards every path.
12. **The API keeps its metrics in memory, per process.** **Open**: documented; dashboards should not sum over API replicas. Each replica reads
    `ops.metrics` from the moment it starts, so replicas report different
    counters, and counters start from zero after a restart. Dashboards that sum
    over API replicas count everything twice. The rate limiter is per replica
    for the same reason.
13. **The API reads the fleet once, at start-up.** **Fixed**: an API with an empty fleet reloads it every 20 s until it exists. Started before the seed job
    has finished, it serves an empty fleet until it is restarted. Hence the
    start order in Compose and the init container in the chart.
14. **TimescaleDB is not offered by Amazon RDS** **Open, by design**: the SQL falls back to a plain table., and by none of the large
    managed PostgreSQL services with compression and continuous aggregates.
    `postgres_extras.sql` falls back to a plain table when the extension is
    missing.
