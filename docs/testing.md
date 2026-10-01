# Testing

| Suite | Where | Tools | Count | Result |
|---|---|---|---|---|
| Unit | `tests/unit` | pytest, fakeredis, scipy as an oracle | 2,575 | all pass |
| Integration | `tests/integration` (not `test_infra.py`) | pytest, the real local adapters, FastAPI TestClient | 76 | all pass |
| Contract | `tests/contract` | JSON Schema, committed OpenAPI document, console call scan; **Pact** (below) | 20 | all pass |
| Contract, Pact | `tests/contract/pact`, `web/src/lib/api.pact.test.ts` | pact-js 17 (web console -> API, 8 interactions); pact-python 3 (normaliser -> processor, 6 messages; normaliser -> dead-letter worker, 2; processor -> alert subscribers, 2); providers verified against the real API, normaliser and processor | 8 + 7 | all pass; pact files committed in `tests/contract/pacts/` |
| Security | `tests/security` | pytest, organised by OWASP API Top 10 | 66 | all pass |
| Infrastructure | `tests/integration/test_infra.py` | Testcontainers: PostgreSQL 16, pgvector, TimescaleDB 2.30, Redis 7, Kafka 3.9 (KRaft) | 13 | all pass (needs Docker) |
| Acceptance | `tests/bdd` | behave, Gherkin | 21 scenarios, 143 steps | all pass |
| Chaos | `tests/chaos/kill_and_recover.py` | SIGKILL under load, then accounting | 6 checks | all pass |
| Chaos, Compose | `docs/evidence/compose_mqtt_restart.json` | graceful restart and SIGKILL of the MQTT gateway in the Compose stack | 1 procedure | 615,464 sent, 615,464 received |
| Load, pipeline | `scripts/bench_pipeline.py` | ten processes, 100,000 vehicles | steady, burst, soak | see evidence |
| Load, API | `tests/load/locustfile.py` | Locust, three user types | see evidence | see evidence |
| Security scans | local and CI | bandit, Semgrep, pip-audit, npm audit, Trivy (file system and image), OWASP ZAP baseline and authenticated API scan | 9 scans | every CI gate passes; reports and triage in `docs/evidence/security/` |

Line coverage over all 2,768 tests that need no Docker: **93.5%** (92.1% with
branches, `docs/evidence/coverage.json`). Unit tests alone cover the core modules
(domain, algorithms, engine, adapters) at 89 to 100% each.

## Bugs the tests found

Running the production stack in Docker Compose found seven more that no test with
local adapters could reach: a TimescaleDB write that always failed, a health check
that passed too early, message loss at the MQTT gateway on restart, broker limits
that throttled the gateway, Kafka commits that crashed during a rebalance, a
Prometheus label clash that silenced per-service alerts, and a failing metrics
exporter. Each is fixed and has a test; the list is in `infra/VERIFICATION.md`.

Writing the unit tests independently of the code, with the rule "a test that finds a
bug is marked as an expected failure, never weakened", found 16 real defects. All
are fixed and their tests now pass. The most important:

- A torn write at the end of a log segment made every later record of that
  partition unreadable. The writer now cuts off an incomplete frame before appending.
- The MQTT gateway dropped its buffer on shutdown, after the broker had already been
  told the messages were received. It now keeps trying for a grace period.
- Old-format traffic counted against a canary, which could have rolled back a good
  version. Only invalid output counts now.
- A malformed mapping spec could raise a plain `TypeError` and take down the loading
  of every mapping. Every malformed spec is now a `SpecError`, and one bad row is
  skipped and reported.
- A train and test leak in the synthetic dialects (`recordedAt` in both), and event
  code collisions. Fixed, the model retrained, and the reported accuracy went down
  slightly: 99.6% to 99.1%. The baseline was also handicapped by a bug and became
  stronger: 30.4% to 35.8%. The numbers in this repository are the corrected ones.
- The agent's repair loop never ran, because a successful test result carried an
  empty `error` key. Found by an integration test that sabotages the first proposal.
- Latitudes near the equator sent in 1e-7 degrees were read as micro-degrees, so
  the physics check could not find the position of vehicles in Chennai or Kochi.

## Edge cases covered

Duplicates (1.5% by default, 30% in some tests), out-of-order delivery, events
older than the de-duplication window, truncated payloads, corrupted VIN check
digits, empty and `null` payloads, oversize payloads, unknown sources, a format
change for part of a fleet, a worker killed between batches, a worker killed after
producing but before committing, a processor killed after writing a file but before
committing, a replay run twice, forged and expired tokens, cross-tenant access,
injection strings in every query parameter, path traversal against the static files.

## Running

```bash
make test               # everything that needs no Docker
make test-infra         # needs Docker
make pact               # both sides of every contract
make security           # bandit, pip-audit, npm audit, security tests (Semgrep, Trivy when installed)
make infra-verify       # helm lint, kubeconform, terraform validate, in Docker
make coverage
python -m behave tests/bdd/features
python tests/chaos/kill_and_recover.py
python scripts/bench_pipeline.py --seconds 90 --label steady
```
