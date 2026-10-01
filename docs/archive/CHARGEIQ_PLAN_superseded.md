# ChargeIQ — Implementation Plan

**Problem space:** EV Fleet Charging Optimization + Battery Health Intelligence, with an Agentic Charging Copilot.
**Question answered (from the problem statement, §5):** *"When and where should each vehicle charge at lowest cost?"*
**Primary user:** Fleet manager of a mixed ICE/EV commercial fleet. **Secondary:** fleet finance teams, drivers, OEMs, sustainability teams.

---

## 1. Why this problem

- **Named 2026→2030 trend** in the problem statement: "mixed ICE/EV fleets need charging optimisation, battery state-of-health tracking and range prediction."
- **The DSA rubric (§9) uses this exact problem as its examples**: Dijkstra/A* for nearest reachable charger under remaining range, DP for charging schedule under time-of-use tariffs, DP for trip segmentation, geohash indexing, sliding windows, Bloom-filter dedup. Our algorithms section is therefore real, not decorative.
- **Less crowded** than predictive maintenance / driver safety, which most teams will pick.
- **Agentic AI fits naturally** (Charge Copilot ≈ Motorq Fuse analogue) with genuine guardrails and audit-trail requirements.
- **Demo has dollar impact**: "smart overnight schedule saves ₹X vs charge-on-arrival" — the doc explicitly rewards estimating dollar impact of recommendations.

**Problem statement (template §2.1 format):**
> A fleet manager operating a mixed ICE/EV fleet needs a way to decide when and where each EV should charge, because uncoordinated charging causes peak-tariff energy costs, depot power overloads, mid-route range failures and accelerated battery degradation, which today costs fleets an estimated 20–30% excess energy spend and unplanned downtime per vehicle incident.

---

## 2. Architecture

### 2.1 Services

| Service | Language | Responsibility |
|---|---|---|
| `simulator` | Python (or Go if throughput demands) | Generate 100K vehicles' telemetry: EV/ICE mix, drive cycles, SoC physics, DTC faults, bursts, duplicates, out-of-order, late events |
| `ingest` | Python (FastAPI worker) or Go | MQTT subscribe → JSON-Schema validate → dedupe (Bloom filter + Redis fallback) → produce to Kafka, idempotent, back-pressure aware |
| `stream-processor` | Python (confluent-kafka consumers) | Sliding-window aggregates, trip segmentation (DP), low-SoC / anomaly alerts, write hot state to Redis + telemetry to TimescaleDB, archive batches to Parquet/MinIO |
| `optimizer` | Python | A* nearest-reachable-charger, DP time-of-use charging schedule, SoH regression |
| `api` | FastAPI | OAuth2/JWT, RBAC, tenant isolation, keyset pagination, rate limiting; REST + OpenAPI |
| `agent` | Python (LangGraph + Claude API) | Charge Copilot: scoped tools, pgvector RAG, audit log, human-approval gate |
| `web` | React (Vite) + deck.gl/Leaflet | Live map, charging plan + savings, SoH dashboard, copilot chat |

### 2.2 Data flow

```
simulator ──MQTT (EMQX, mTLS)──► ingest ──► Kafka `telemetry.raw` (keyed by VIN, 32+ partitions)
                                              │
                              stream-processor┤
                                              ├─► Redis        (latest state per VIN; TTL)
                                              ├─► TimescaleDB  (hypertable telemetry, time+vehicle)
                                              ├─► Kafka `alerts` ─► api (SSE/WebSocket) ─► web
                                              └─► MinIO Parquet (5-min micro-batches, partitioned dt=/hour=)
optimizer ◄── reads Redis + Postgres (stations, tariffs) ── writes ChargingPlan to Postgres
agent     ◄── tools call api/optimizer; RAG via pgvector; every action → audit_log
```

**Latency budget (template §5.1 asks for per-hop):** MQTT→ingest < 50 ms; ingest→Kafka < 20 ms; Kafka→processor < 200 ms; processor→Redis/alert < 100 ms; alert→UI push < 1 s. Total ingest→dashboard well under the 2 s NFR; critical alert under 5 s.

### 2.3 Polyglot storage map (with CAP choices)

| Store | Data | Why | CAP |
|---|---|---|---|
| PostgreSQL | 3NF core: fleets, vehicles, drivers, stations, tariffs, sessions, users, audit | ACID for ownership/billing/RBAC/audit | CP |
| TimescaleDB (extension on same PG) | telemetry hypertable | High-ingest time-series, compression, continuous aggregates; one engine, two guarantees — deliberate simplification, documented in ADR-2 | effectively AP usage (async, tolerate lag) |
| Redis | latest state per VIN, dedup set, rate-limit counters, cache | Sub-ms hot reads for map/agent | AP |
| MinIO + Parquet (+ DuckDB) | raw archive, batch analytics | Columnar, cheap, S3-API = cloud-agnostic | AP |
| pgvector | embeddings of alerts, incident notes, fleet docs | Agent RAG without another server | CP |

### 2.4 Relational core (3NF)

Entities: `fleet`, `vehicle` (FK fleet, VIN unique w/ check-digit constraint), `driver`, `vehicle_driver_assignment` (temporal), `trip` (derived from segmentation), `charging_station`, `connector` (station 1-N, type/power), `tariff` (station/utility, time-of-use slots), `charging_session`, `charging_plan` + `charging_plan_item`, `alert`, `subscription`, `app_user`, `role`, `user_role`, `audit_log`.
Deliberate denormalization (documented): `vehicle.latest_soc/lat/lon/ts` mirror of Redis for cheap joins; continuous aggregates in Timescale.

### 2.5 Kafka topics

| Topic | Key | Partitions | Semantics |
|---|---|---|---|
| `telemetry.raw` | VIN | 32 | at-least-once in, idempotent consumers (dedupe on (vin,seq)) |
| `telemetry.clean` | VIN | 32 | validated + deduped |
| `alerts` | VIN | 8 | at-least-once, idempotent alert upsert |
| `dlq.telemetry` | — | 4 | poison messages for replay |

Keying by VIN preserves per-vehicle ordering; VIN hash spreads load (no hot-spot since all vehicles emit similarly).

---

## 3. Algorithms (template §6.5)

1. **Nearest reachable charger — A\*** over a graph of geohash-bucketed stations. Feasibility prune: `energy_needed(path) ≤ SoC × capacity × safety_factor`. Heuristic: haversine × min-consumption-rate (admissible). Complexity O(E log V); measure at 5–10K stations.
2. **Charging schedule — DP.** Discretize night into 15-min slots. State `dp[slot][soc_bucket]` = min cost to reach that SoC by that slot; transition = charge at station power (cost = kWh × tariff(slot)) or idle; constraint: depot max concurrent kW (post-process greedy/Lagrangian across vehicles, documented). Output: per-vehicle plan + total ₹ saving vs naive charge-on-arrival baseline. O(slots × soc_buckets) per vehicle — trivially parallel across vehicles.
3. **Trip segmentation — DP/heuristic** on noisy GPS: stop detection via speed+dwell windows, penalized segmentation. Feeds `trip` table and utilization metrics.
4. **Streaming structures:** Bloom filter for (vin,seq) dedup (sized for ~10^9 events, 1% FP, Redis-backed escape hatch); sliding-window averages for SoC drain rate; geohash prefix index for map queries; Count-Min Sketch for top-K "most alerting vehicles" (nice-to-have).
5. **VIN validation:** regex `^[A-HJ-NPR-Z0-9]{17}$` + ISO 3779 check-digit; DTC parser regex `^[PCBU][0-3][0-9A-F]{3}$`.
6. **SoH model:** linear/gradient-boosted regression on (age, cycles, fast-charge fraction, avg depth-of-discharge, temperature proxy) → capacity fade; evaluated vs "SoH = f(age only)" baseline with MAE. Simulator generates ground-truth fade so evaluation is honest.

---

## 4. Simulator design (graded deliverable)

- 100K vehicles: ~60% ICE, 30% BEV, 10% PHEV; per-vehicle profile (battery kWh, efficiency, depot, shift pattern).
- Drive cycles: depot → route waypoints → depot; speed profiles with noise; SoC drain = f(speed, distance, HVAC factor); DTC injection (~0.1%/day); harsh-brake events.
- **Realism knobs required by rubric:** 3x burst at shift start (and on "network recovery"), 1–2% duplicates, out-of-order delivery (jittered send queue), late-arriving batches, malformed events for DLQ path.
- Scaling trick: event-loop simulation of 100K vehicle *states* (not 100K threads); batch-publish via MQTT/Kafka producers. Target sustained 100K events/sec on demo hardware — if a laptop can't, run N simulator containers and document horizontal scaling (this is itself a talking point).

---

## 5. Security & compliance (template §8)

- OAuth2/OIDC password+client-credentials flows (Keycloak container or FastAPI-native JWT issuer), RBAC roles: admin / fleet_manager / analyst / agent_service; **tenant isolation** via fleet_id claim enforced in every query (+ Postgres RLS as defense-in-depth).
- mTLS between simulator and EMQX (per-"device" certs, demo CA); TLS 1.3 elsewhere; AES-256 at-rest (PG + MinIO encryption); secrets via .env → K8s secrets → (ADR: Vault as future).
- Privacy: location masking for non-privileged roles (geohash truncation), configurable retention, **right-to-erasure endpoint** (delete driver PII + anonymize trips) — demoable GDPR/DPDP flow.
- Agent safety: tools are read-mostly; any state-changing tool (e.g., `apply_charging_plan`) requires human approval flag; prompt-injection defense (tool results treated as data, system-prompt rules); every tool call → `audit_log`.
- STRIDE threat model over ingestion path + public API (top 5 threats + controls table).

## 6. Testing & NFR evidence (heavily weighted)

| Suite | Tooling | Evidence |
|---|---|---|
| Unit (80%+ on core) | pytest + coverage | VIN/DTC parsing, dedup, DP optimizer, A*, windowing |
| Integration | Testcontainers (Kafka, PG, Redis, MinIO) | end-to-end event → alert; DLQ path |
| Contract | Pact or schemathesis vs OpenAPI | api ↔ web, agent ↔ api |
| Acceptance | behave (BDD) | "vehicle below reachable-range threshold → alert within 5 s" |
| Load | k6 (API) + custom Kafka producer bench | 100K events/sec sustain, 3x burst 5 min, p95/p99 graphs, consumer lag |
| Security | Semgrep, Trivy (deps + images), ZAP baseline | reports in CI artifacts |
| Chaos | script: kill Kafka broker / processor pod | recovery proof + Grafana screenshot |
| SQL optimization | EXPLAIN ANALYZE before/after ×3 | composite + partial indexes, continuous aggregate, keyset pagination |

Observability: OpenTelemetry traces (ingest→alert), Prometheus metrics (events/sec, lag, latencies), Grafana dashboard (screenshot for doc), structured JSON logs → Loki (or ELK if time).

## 7. DevOps

- **One command:** `docker compose up` starts full stack + seeded 100K-vehicle dataset (seed script in compose init).
- K8s manifests + Helm chart (api, processor, ingest as HPA-scaled stateless deployments); Terraform for one cloud (e.g., a single GKE/EKS module); cloud-agnostic argument: S3-API (MinIO), standard Kafka/PG images, no managed-service SDKs.
- GitHub Actions: lint → unit → integration (Testcontainers) → build images → Trivy/Semgrep → (manual) load-test job. Badge in README.
- Tag `v1.0-submission` before deadline; code freeze respected.

## 8. Phase plan & team split

Phases (proportional to total time available):

- **P0 (10%)** Repo + compose + CI skeleton (CI green from first commit).
- **P1 (20%)** Simulator + ingest + Kafka path with dedup/validation/DLQ.
- **P2 (20%)** 3NF schema, hypertables, stream processor, alerts < 5 s, Redis hot state, Parquet archival.
- **P3 (15%)** Optimizer: A*, DP schedule, SoH model + baseline eval; complexity write-up with measured runtimes.
- **P4 (15%)** API (auth/RBAC/pagination/rate-limit) + React UI (map, plan+savings, SoH, alerts).
- **P5 (10%)** Agent: LangGraph, tools (`get_fleet_summary`, `get_vehicle_status`, `find_chargers`, `propose_charging_plan`, `apply_charging_plan`[approval]), pgvector RAG, audit log.
- **P6 (15%)** Load/chaos/security tests, EXPLAIN ANALYZE, Helm/Terraform, STRIDE, ADRs.
- **P7 (5%)** Solution document, demo video (≤5:00), final tag.

Team of 4: ① simulator+ingest+stream, ② storage+optimizer+ML, ③ api+web+agent, ④ devops+testing+observability+document owner.

**Cut list if time runs short (in order):** Helm→plain manifests; ELK→Grafana+Loki only; SoH ML→rule-based + "future work"; Count-Min top-K. **Never cut:** CI, load test, dedup path, ADRs, demo video quality.

## 9. ADRs to write (3–5)

1. Kafka vs RabbitMQ (replay + partitioned ordering; at-least-once + idempotent consumers vs exactly-once complexity).
2. TimescaleDB extension vs separate Cassandra/ClickHouse (operational simplicity at hackathon scale; migration path documented) — includes the CAP/PACELC discussion (billing CP, telemetry AP/EL).
3. MQTT→Kafka bridge pattern vs direct Kafka from devices (device constraints, mTLS at edge, back-pressure).
4. Python stream consumers vs Flink (team velocity; DuckDB-on-Parquet covers batch; scaling story via consumer groups).
5. Agent guardrails: read/write tool split + human approval + audit (safety over autonomy).

## 10. Demo video storyboard (5 min)

1. (0:00–0:40) Problem + one-slide architecture.
2. (0:40–1:40) Simulator firehose: Grafana showing ~100K events/sec, burst survival, consumer lag recovering.
3. (1:40–2:40) Live map; vehicle drops below reachable range → alert in <5 s → A* charger recommendation.
4. (2:40–3:40) Overnight DP charging plan: cost vs naive baseline, ₹ saved, depot power constraint respected; SoH dashboard.
5. (3:40–4:40) Charge Copilot: natural-language question → tool calls visible → plan proposed → human approves → audit log entry.
6. (4:40–5:00) Test/CI evidence flash: coverage, k6 graphs, chaos recovery, tag.
