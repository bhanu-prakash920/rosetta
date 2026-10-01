"""The words of the Solution Document. Numbers come from docs/evidence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(ev: Path, name: str) -> Any:
    p = ev / name
    return json.loads(p.read_text()) if p.exists() else None


def n(x: float, d: int = 0) -> str:
    return f"{x:,.{d}f}"


def build_sections(d: Any, ev: Path, root: Path) -> dict[str, Any]:
    steady = _load(ev, "bench_steady.json")
    burst = _load(ev, "bench_burst.json")
    soak = _load(ev, "bench_soak.json")
    chaos = _load(ev, "chaos.json")
    ml = _load(ev, "ml_field_mapper.json")
    sql = _load(ev, "sql_explain.json")
    alg = _load(ev, "algorithms.json")["results"]
    cov = _load(ev, "coverage.json")
    locust = _load(ev, "locust.json")
    dg = root / "docs" / "diagrams"
    sh = root / "docs" / "screenshots"

    st_t, st_l, st_a = steady["throughput"], steady["latency_ms"], steady["accounting"]
    cov_pct = 100 * cov["totals"]["covered_lines"] / cov["totals"]["num_statements"] if cov else 0   # lines, not branches
    ms, bs = ml["model_scores"], ml["baseline_scores"]

    A: dict[str, str] = {}
    T: dict[str, Any] = {}
    AF: dict[str, str] = {}

    # ------------------------------------------------------------------ 1
    A["1. Executive Summary"] = "".join([
        d.p("**Problem.** A connected-vehicle platform receives data from many car makers, and every maker reports the same "
            "facts (position, speed, charge, faults) in its own format and units. Adding a maker, or surviving an over-the-air "
            "update that renames a field, normally means new code, a release, and messages lost while it ships. The people it "
            "affects: the platform engineers who carry that work, and every fleet, lender and dealer downstream who sees gaps."),
        d.p("**Solution.** Rosetta translates every dialect into one canonical event at 100,000 events per second. When it meets "
            "a format it cannot read, it parks the messages, an agent works out the mapping and proves it on known answers, a person "
            "approves it, and the running workers pick it up between two batches and replay what was parked. No release, no "
            "restart, no lost data."),
        d.p(f"**Results, measured on one laptop.** {n(st_t['sent_per_s_mean'])} events per second sent by 100,000 simulated vehicles "
            f"and {n(st_t['normalized_per_s_mean'])} translated per second over 90 s; ingest to dashboard p95 "
            f"{n(st_l['ingest_to_dashboard_p95_median'])} ms and p99 {n(st_l['ingest_to_dashboard_p99_median'])} ms against a 2 s "
            f"target; {n(st_a['simulator_sent'])} events and {st_a['unaccounted']} unaccounted. Four worker processes killed with "
            f"SIGKILL under load: all recovered, nothing lost, nothing stored twice. The field-mapping model is "
            f"{ms['exact_label_accuracy']:.1%} correct on formats whose names it never saw, against {bs['exact_label_accuracy']:.1%} "
            f"for a name-matching baseline. {cov['meta'] and ''}{n(2737)} automated tests, {cov_pct:.0f}% line coverage. The production stack runs from one command in Docker Compose, and a restart or SIGKILL of the MQTT gateway under load loses nothing."),
        d.p("**What is new.** Mappings are data from a whitelist, compiled to specialised code at run time, so an agent can propose "
            "one without being able to run arbitrary code. The agent identifies units from physics: a field always 0.278 times the "
            "GPS-derived speed is metres per second, whatever it is called. And onboarding is zero downtime by construction: park, "
            "prove, approve, hot reload, replay."),
    ])

    # ------------------------------------------------------------------ 2
    A["2.1 Problem Statement"] = "".join([
        d.p("**Platform engineers at a connected-vehicle data company need a way to onboard a new OEM telemetry format, or a changed "
            "one, without stopping ingestion, because each change today is hand-written adapter code and a release during which "
            "messages from that maker are lost or delayed, which costs every downstream customer data gaps and the platform "
            "engineering time on every one of its 25+ OEM integrations.**"),
        d.bullet("**Primary user:** the platform (integration) engineer who owns OEM connectivity."),
        d.bullet("**Secondary stakeholders:** fleet managers, lenders and dealers who consume the data; OEM partner teams whose "
                 "firmware changes the format; compliance officers who must know who saw which location data."),
    ])
    T["2.2 Evidence & Validation"] = d.table(
        ["Evidence / Assumption", "Source or Method", "What It Shows", "Confidence"],
        [["Normalising many OEM formats into one schema is core, continuous work for a platform like the reference company",
          "Problem statement, section 3.1 (\"continuously maps data from 25+ brands\")", "The problem exists at the industry's centre, not the edge", "High"],
         ["Software-defined vehicles change their data format more often", "Problem statement, trends 2026-2030", "Format changes become routine, not rare events", "High"],
         ["A unit error is silent: mph read as km/h is 38% low but plausible", "Simulation: `pacifica` dialect, golden set", "Name matching is not enough; values and physics must be checked", "High"],
         ["A name-matching approach, the usual first automation, maps few fields right",
          "Baseline measured on 160 synthetic formats", f"{bs['exact_label_accuracy']:.1%} field-and-unit accuracy, 0% of whole formats right", "Medium (synthetic formats)"],
         ["Engineering cost per new OEM is days to weeks", "Assumption, from typical integration projects", "Order of magnitude only; not measured here", "Low"],
         ["Real OEM feeds are messier than our six simulated dialects", "Assumption", "Model accuracy will be lower on real data; hence golden set and human approval", "High"]],
        [3, 2.4, 2.8, 1.2])
    AF["2.2 Evidence & Validation"] = "".join([
        d.p("**Validation method.** Simulation: six dialects built from one ground truth, so every mapping can be scored exactly, and "
            "160 further synthetic formats with names never used in training to evaluate the model. No interviews were possible."),
        d.p("**Existing alternatives.** Aftermarket dongles avoid the problem by reading one standard port (OBD-II) but need hardware "
            "in every vehicle. OEM portals give one maker at a time. A commercial normalisation layer solves it with engineers and "
            "releases. Schema-mapping research tools match by names. Rosetta's difference: proposals checked against physics and known "
            "answers, a human gate, and onboarding without a restart."),
    ])
    T["2.3 Impact & Success Metrics"] = d.table(
        ["Metric", "Baseline Today", "Target", "How Measured / Estimated"],
        [["Messages lost while a new format is onboarded", "all messages until the release ships", "0",
          "Integration test and BDD scenario: every parked message is replayed"],
         ["Restarts needed to go live with a new format", "1 release and restart per change", "0",
          "Test asserts the worker object is the same before and after"],
         ["Time from first unreadable message to a validated draft", "days (hand-written adapter)", "under 1 minute",
          "Agent run: under 1 s for the demo formats, then a person reviews"],
         ["Formats mapped completely right without a person editing", "0% (name matching)", "most formats",
          f"{ml['whole_dialect']['model_fully_correct']:.1%} of 160 synthetic formats"],
         ["Ingest to dashboard latency", "not measured", "under 2 s p95", f"{n(st_l['ingest_to_dashboard_p95_median'])} ms p95 measured"]],
        [2.6, 2, 1.4, 3])
    A["2.3 Impact & Success Metrics"] = "".join([
        d.p("**Scale of impact.** At 10,000 vehicles one lost hour of a maker's data is 36 million events; at 100,000 it is 360 "
            "million. Every format change today risks that. With Rosetta the cost of a change is one review."),
        d.p("**Wider impact.** Safety: harsh-braking and fault alerts keep flowing during a format change. Compliance: every read of "
            "location data and every agent action is in a tamper-evident log, and personal data can be erased with proof. Cost: "
            f"the archive stores {n(41.8, 1)} bytes per event instead of about 330 for the JSON."),
    ])

    # ------------------------------------------------------------------ 3
    A["3.1 Solution Overview & User Journey"] = "".join([
        d.p("**What it does.** Rosetta is the translation layer between car makers and the people who use vehicle data. It reads six "
            "dialects today (nested JSON, flat JSON in imperial units, pipe-delimited text, Protobuf, a signal list, and one it has "
            "never seen) and writes one canonical event. When a message cannot be read, it is kept, not dropped."),
        d.p("**User journey: a new maker joins.**"),
        d.bullet("Helix Mobility vehicles start sending. The console shows a new source with messages parked as `NO_ADAPTER`."),
        d.bullet("The engineer clicks **Run the agent**. It samples the parked messages, profiles 15 fields, finds latitude and "
                 "longitude from how they move, sees that `fahrt.v` is 0.278 times GPS speed (metres per second), proposes 14 mappings, "
                 "learns the event codes from examples, and passes 200 of 200 known answers, half of which it never saw."),
        d.bullet("The engineer clicks **Try on parked traffic** (a dry run over the real parked messages) and then **Approve**."),
        d.bullet("Within a second the workers load the mapping between two batches and the parked messages are replayed. The live "
                 "chart shows Helix appear; the dead-letter count falls; no process restarted."),
        d.image(sh / "studio.png", 6.4, "The mapping studio after an agent run: every tool call, and the proposed field map with confidence and test results."),
        d.image(sh / "live.png", 6.4, "The live console: 100,000 vehicles, throughput per maker, what needs attention, scenarios and processes."),
    ])
    A["3.2 Key Value Proposition"] = "".join([
        d.bullet("**Customer job:** keep one clean, trustworthy stream of vehicle data while makers keep changing what they send."),
        d.bullet("**Pain relieved:** no adapter code per maker, no release per format change, no lost messages in between, and no "
                 "silent unit errors, because every mapping must reproduce known answers."),
        d.bullet("**Gain created:** onboarding in minutes by a reviewer instead of days by a developer; a record of who approved "
                 "what and why; two firmware versions of one maker running side by side during a rollout."),
        d.bullet("**Differentiation:** the agent cannot break production (it can only propose, from a whitelist), units are checked "
                 "against physics rather than guessed from names, and everything is measured: throughput, latency, accuracy against a "
                 "baseline, recovery from crashes."),
    ])
    A["3.3 Innovative Ideas"] = "".join([
        d.p("**1. Physics as evidence for units.** Consecutive messages of a vehicle give a GPS-derived speed and distance. Each numeric "
            "field is compared with them: its ratio to speed, and its growth per kilometre. *What is new:* schema matching usually "
            "looks at names and value distributions; this uses the physical relationship between fields. *Evidence:* in the ablation, "
            f"names alone reach {ml['ablations']['names_only']['exact_label_accuracy']:.1%}, values {ml['ablations']['values_only']['exact_label_accuracy']:.1%}, "
            f"values with physics {ml['ablations']['values_and_physics']['exact_label_accuracy']:.1%}; physics features carry "
            f"{ml['share_of_importance']['physics']:.0%} of the model's importance. Unit accuracy, when the field is right, is "
            f"{ms['unit_accuracy_when_field_right']:.0%}."),
        d.p("**2. Mappings as data, compiled to code.** A spec from a whitelist is validated and then specialised into one straight-line "
            "function per mapping version. *What is new:* safety of data with the speed of code, and an agent that can propose without "
            "being able to execute. *Evidence:* 1.6x faster than the interpreter on one core; a differential test across all dialects, "
            "corrupted payloads and odd inputs shows identical results; injection attempts in specs are refused."),
        d.p("**3. Zero downtime by construction: park, prove, approve, reload, replay.** *What is new:* onboarding is a data operation "
            "with a safety net at each step (hold-out golden set, dry run on real parked traffic, canary share with automatic rollback, "
            "replay with de-duplication). *Evidence:* integration and BDD tests assert no restart and full replay; a second replay adds "
            "nothing; during a firmware rollout both versions translate side by side."),
    ])

    # ------------------------------------------------------------------ 4
    feats = [
        ["F-01", "Normalise six OEM dialects", "As a customer I want one event format so that I never parse a maker's format", "Must", "Done", "rosetta/engine/", "1:20"],
        ["F-02", "100K-vehicle simulator with faults", "As an engineer I want realistic duplicates, reordering, outages and malformed data so that the pipeline is tested honestly", "Must", "Done", "rosetta/simulator/", "1:05"],
        ["F-03", "Dead-letter parking with reasons and families", "As an engineer I want unreadable messages kept and grouped so that nothing is lost and I see the problem, not 40,000 rows", "Must", "Done", "rosetta/pipeline/dlq.py", "1:45"],
        ["F-04", "Mapping agent (deterministic and Claude)", "As an engineer I want a proposed mapping with evidence so that onboarding takes minutes", "Must", "Done", "rosetta/agent/", "2:05"],
        ["F-05", "Golden-set validation with hold-out", "As a reviewer I want proof a mapping is right so that I approve with confidence", "Must", "Done", "rosetta/services/golden.py", "2:30"],
        ["F-06", "Hot reload, canary, auto rollback", "As an engineer I want a new version live for a share of vehicles without a restart so that a mistake stays small", "Must", "Done", "rosetta/engine/router.py", "2:45"],
        ["F-07", "Replay of parked messages", "As a customer I want the data from before the mapping existed so that there is no gap", "Must", "Done", "rosetta/pipeline/dlq.py", "2:50"],
        ["F-08", "Exactly-once results under crashes", "As a customer I want every event once so that counts are right", "Must", "Done", "rosetta/engine/dedup.py", "3:40"],
        ["F-09", "Live map with masking", "As a fleet manager I want my vehicles on a map; as an analyst I see cells, not positions", "Should", "Done", "web/src/pages/FleetMap.tsx", "3:10"],
        ["F-10", "Real-time alerts", "As a fleet manager I want harsh braking, low charge and new fault codes within seconds", "Should", "Done", "rosetta/pipeline/processor.py", "3:15"],
        ["F-11", "DP trip segmentation", "As a fleet manager I want trips and stops, not flicker", "Should", "Done", "rosetta/algorithms/trip_segmentation.py", "-"],
        ["F-12", "Hash-chained audit log", "As a compliance officer I want every read and agent action recorded, tamper-evident", "Must", "Done", "rosetta/services/audit.py", "4:20"],
        ["F-13", "Right to erasure with proof", "As a data subject's employer I want a driver's data removed and proven removed", "Must", "Done", "rosetta/services/erasure.py", "4:25"],
        ["F-14", "Batch analytics over Parquet", "As an analyst I want data quality and hotspots over history", "Should", "Done", "rosetta/batch/reports.py", "-"],
        ["F-15", "Chaos: kill processes from the console", "As an engineer I want to prove recovery", "Could", "Done", "rosetta/pipeline/runtime.py", "3:45"],
        ["F-16", "Protobuf mapping without a descriptor", "Binary data without a schema", "Won't", "-", "agent reports it", "-"],
    ]
    T["4. Feature List"] = d.table(["ID", "Feature", "User Story", "Priority", "Status", "Code Path", "Video"], feats,
                                   [0.6, 1.8, 3.2, 0.8, 0.7, 2.0, 0.6], size=15)
    AF["4. Feature List"] = d.p("Video timestamps refer to the storyboard in section 13; they are to be confirmed when the video is recorded.")

    # ------------------------------------------------------------------ 5
    A["5.1 Architecture Overview"] = "".join([
        d.image(dg / "c4_context.png", 6.2, "System context: who and what Rosetta talks to."),
        d.image(dg / "c4_containers.png", 6.4, "Containers, stores and protocols. Every store sits behind a port with a local and a production adapter."),
        d.p("**Path of one event, with measured latency.** Vehicle publishes over MQTT (TLS 1.3, client certificate, QoS 1) to EMQX; the "
            "gateway stamps the receive time and appends to `telemetry.raw` keyed by device (under 5 ms); a normaliser polls a batch, "
            "routes, decodes, maps, validates and de-duplicates (about 10 µs per event) and appends to `telemetry.canonical` keyed by "
            "VIN; a processor parses the batch into Arrow columns, updates the hot state and raises alerts (about 10 µs per event). "
            f"End to end from gateway to hot state: p50 {n(st_l['ingest_to_dashboard_p50_median'])} ms, p95 "
            f"{n(st_l['ingest_to_dashboard_p95_median'])} ms, p99 {n(st_l['ingest_to_dashboard_p99_median'])} ms at 100,000 events per "
            "second. Most of it is batching; the work itself is microseconds."),
    ])
    T["5.2 Technology Stack & Justification"] = d.table(
        ["Layer", "Choice", "Why This, and What You Rejected"],
        [["Ingestion / Messaging", "MQTT (EMQX) at the edge; Kafka inside", "MQTT suits devices (small, QoS 1, mTLS per device). Kafka for replay and per-key order; RabbitMQ rejected: no log replay. Direct Kafka from devices rejected: no per-device auth at scale."],
         ["Stream / Batch Processing", "Python workers with consumer groups; Arrow and Parquet for batch", "Consumer groups scale by adding replicas. Flink rejected for the hackathon: operational weight for logic that is per-event. Batch jobs read Parquet with pyarrow; the same files work in Spark, DuckDB, Snowflake."],
         ["Relational / NoSQL / Cache / Search / Vector", "PostgreSQL (3NF), Redis, Parquet on S3, optional TimescaleDB, pgvector", "Each justified in ADR 0002 with its CAP choice. Cassandra rejected: Parquet on S3 is cheaper for write-once history. A separate vector database rejected: pgvector sits next to the registry."],
         ["Backend / Frontend", "FastAPI, SQLAlchemy 2; React, TypeScript, Vite, Leaflet", "FastAPI gives OpenAPI and validation for free. React for the console; no chart library: charts are hand-written SVG, small and themeable."],
         ["ML / AI", "scikit-learn ExtraTrees; Hungarian assignment; Claude (claude-opus-5-5) via tool use, optional", "A tree ensemble on 304 features is fast, needs no GPU, and is explainable per feature. The language model is optional: a deterministic workflow uses the same tools."],
         ["Infrastructure / CI-CD / Observability", "Docker, Compose, Helm, Terraform (AWS), GitHub Actions; Prometheus, Grafana, Loki with Alloy, Tempo, OpenTelemetry, JSON logs; Pact", "Standard, cloud-agnostic pieces. No cloud SDK in the application: Kafka, PostgreSQL, Redis, S3 API and OIDC are available everywhere."]],
        [1.6, 2.4, 5])
    sq = sql["cases"] if sql else []
    T["5.3 Data Architecture"] = d.table(
        ["Query", "Before (ms)", "After (ms)", "Change Made"],
        [[c["name"], n(c["before_ms"], 2), n(c["after_ms"], 3), c["change"]] for c in sq],
        [2.6, 0.9, 0.9, 4.2], size=15)
    AF["5.3 Data Architecture"] = "".join([
        d.p(f"PostgreSQL 16, {n(sql['rows']['vehicles'])} vehicles, {n(sql['rows']['alerts'])} alerts, {n(sql['rows']['audit_entries'])} "
            "audit entries; median of five EXPLAIN (ANALYZE, BUFFERS) runs, each case from the same index-free baseline. Plans before and "
            "after: docs/evidence/sql_explain.md. One finding: an index I had planned was redundant (the idempotency constraint already "
            "serves the query), so it was removed.") if sql else "",
        d.image(dg / "er.png", 6.4, "Relational core in Third Normal Form (26 tables; the main ones)."),
        d.p("**3NF and deliberate denormalisation.** One fact in one place: a vehicle stores its fleet, not its tenant; personal data lives "
            "only in `driver`, so erasure has one place to act; temporal facts are rows with validity. Deliberately duplicated: the mapping "
            "spec as JSON next to its normalised field rows (a worker compiles a version with one read), one sample payload per dead-letter "
            "group, per-minute metric rollups, and a materialised view of the fleet mix."),
        d.table(["Store", "Holds", "CAP"],
                [["PostgreSQL", "tenants, fleets, vehicles, drivers, users, roles, subscriptions, mapping registry, golden cases, agent runs, audit, pgvector", "CP"],
                 ["Kafka", "raw, canonical, dead-letter, alert, metrics, control topics", "AP per partition, per-key order"],
                 ["Redis", "latest state per vehicle (92 bytes x 100,000 = 9.2 MB), late-event dedup keys", "AP, last write wins by event time"],
                 ["Parquet on S3", "every canonical event, dt=/hour= partitions, rows sorted by VIN", "append-only, eventually complete"],
                 ["TimescaleDB (optional)", "hypertable with compression, continuous aggregate per maker per minute", "as PostgreSQL"]],
                [1.6, 5.4, 2]),
        d.table(["Quantity", "Value", "Basis"],
                [["events per second", "100,000 sustained, 300,000 burst", "problem statement"],
                 ["raw bytes per event", "62 (Protobuf) to 391 (nested JSON), mean about 250", "measured per dialect"],
                 ["raw per day", "about 2.2 TB", "100,000 x 86,400 x 250 B"],
                 ["archived bytes per event", "41.8", "measured, Parquet + zstd"],
                 ["archive per day / year", "about 360 GB / 130 TB", "before down-sampling"],
                 ["partition keys", "device (raw), VIN (canonical), source (dead letters)", "even spread, per-key order"],
                 ["hot / warm / cold", "Redis + Kafka (24-72 h, dead letters 14 days) / S3 Standard 90 days / Glacier IR 13 months at 1 event per 10 s", "about 3,900 USD per month at list prices, mostly Kafka storage"]],
                [1.8, 4, 3]),
    ])
    A["5.4 Deployment View"] = "".join([
        d.image(dg / "deployment.png", 6.4, "Kubernetes deployment from the Helm chart, with the AWS managed services from Terraform."),
        d.p("**Replicas and scaling.** api 2 to 6 and normaliser 4 to 16 on HorizontalPodAutoscalers; processors 2 to 8; one dead-letter "
            "worker. PodDisruptionBudgets and topology spread keep replicas on different nodes and zones. Pods run as non-root with a "
            "read-only root filesystem and no capabilities; a NetworkPolicy limits traffic. Secrets come from a Kubernetes Secret or an "
            "external secret store; the chart never creates one. A pre-install Job creates the schema, applies the TimescaleDB and pgvector "
            "SQL and seeds the data. A second Job creates the Kafka topics with replication factor 3."),
        d.p("**Cloud-agnostic.** The application uses Kafka, PostgreSQL, Redis, the S3 API and OIDC, nothing cloud-specific. Kafka security "
            "settings pass through as `ROSETTA_KAFKA_CFG_*` (TLS on MSK, SASL on Event Hubs or Confluent); S3 static keys are optional so "
            "IRSA or workload identity works. On GCP: Managed Kafka, Cloud SQL, Memorystore, GCS via its S3 endpoint, GKE. On Azure: Event "
            "Hubs, Azure Database for PostgreSQL, Azure Cache for Redis, AKS. Same chart, different values. Locally: `make run` needs no "
            "infrastructure at all; `make up` runs the whole production stack in Docker Compose."),
        d.p("**Honest status.** The Compose stack was run end to end: from empty volumes to 20 healthy services in 108 s, data flowing "
            "from the simulator over MQTT to TimescaleDB, Redis and MinIO, logs in Loki and traces in Tempo. Running it found seven bugs, "
            "all fixed and tested. The Helm chart passes `helm lint` and kubeconform against Kubernetes 1.33; Terraform passes "
            "`terraform validate`; neither was installed on a cluster or applied to an AWS account. The production adapters pass 13 "
            "tests against real Kafka, PostgreSQL, TimescaleDB, pgvector and Redis. infra/VERIFICATION.md lists what is and is not verified."),
    ])

    # ------------------------------------------------------------------ 6
    AF["6.1 Layering & Separation of Concerns"] = "".join([
        d.p("**Style: hexagonal (ports and adapters).** Chosen because the same logic must run in three settings: unit tests, a local mode "
            "without infrastructure, and production. `Normalizer.process()` takes records and returns records; it does not know Kafka exists."),
        d.image(dg / "layers.png", 6.2, "The normaliser's layers. Dependencies point inwards; adapters implement ports."),
        d.code("""rosetta/        domain/ algorithms/ engine/ ports/ adapters/ pipeline/
                simulator/ ml/ agent/ services/ api/ batch/ observability/ db/
web/            src/pages  src/components  src/lib  public/img
tests/          unit/ integration/ contract/ security/ bdd/ chaos/ load/
infra/          docker/ helm/ terraform/ prometheus/ grafana/ emqx/ sql/ minio/
docs/           adr/ api/ diagrams/ evidence/ screenshots/ security/
scripts/        benchmarks, OpenAPI export, SQL optimisation, diagrams"""),
    ])
    T["6.1 Layering & Separation of Concerns"] = d.table(
        ["Layer", "Responsibility", "Must Not"],
        [["Presentation / API: rosetta/api", "HTTP, SSE, validation (Pydantic, extra fields forbidden), auth and role checks, tenant filter, problem+json", "Contain business rules or SQL beyond query construction"],
         ["Application / Service: rosetta/services, rosetta/engine, rosetta/agent", "Use cases: registry transitions, validation, agent runs, normalisation, erasure; transactions", "Depend on a specific broker or database (they use ports and SQLAlchemy)"],
         ["Domain: rosetta/domain, rosetta/algorithms", "Canonical event, transforms whitelist, VIN, DTC, reason codes, algorithms", "Import a framework or infrastructure code"],
         ["Infrastructure: rosetta/adapters, rosetta/db", "Kafka, file log, Redis, mmap, Parquet/S3, TimescaleDB, SQLAlchemy models", "Leak vendor types into the domain"]],
        [2.3, 4, 2.7])
    AF["6.2 Design Principles Applied"] = "".join([
        d.bullet("**SOLID.** Single responsibility: decoders only decode, the compiler only compiles, the router only routes "
                 "(engine/). Open-closed: a new wire format is a new decoder in `_BUILDERS`, a new transform one entry in the whitelist. "
                 "Liskov and interface segregation: `Broker`, `HotState`, `Archive` protocols in ports/ with interchangeable adapters. "
                 "Dependency inversion: workers get adapters from `factory.py`, never construct Kafka or Redis clients themselves."),
        d.bullet("**12-Factor.** All configuration from environment variables (`config.py`); stateless processes apart from a small "
                 "checkpoint; disposability: SIGTERM drains and commits, SIGKILL is survived (chaos test); logs as JSON to stdout; "
                 "dev and prod parity through the same code on both adapter sets."),
        d.bullet("**Idempotency** at every sink (ADR 0003). **Fail-fast**: a spec that does not compile is never stored; required "
                 "fields are evaluated first. **Least privilege**: roles per endpoint, the agent cannot change what runs, containers "
                 "non-root. **DRY**: one toolbox for both agent engines, one canonical schema produces validation, JSON Schema and "
                 "docs. **KISS**: a file-backed log instead of an embedded Kafka for local mode."),
    ])
    T["6.3 Design Patterns Used"] = d.table(
        ["Pattern", "Problem It Solves in Your System", "Location in Code"],
        [["Adapter", "Each OEM payload format to one canonical event; each store behind one interface", "engine/compiler.py, adapters/"],
         ["Interpreter + code generation", "Mapping specs as data, executed fast", "engine/compiler.py, engine/codegen.py"],
         ["Strategy", "Decoders by wire format; agent engines (workflow or Claude)", "engine/decoders.py, services/agent_service.py"],
         ["Repository / Unit of Work", "Registry and audit changes commit together", "services/registry.py, db/session.py"],
         ["Event sourcing (light)", "Mapping history is an append-only action log; state is derived", "db/models.py MappingAction"],
         ["CQRS", "Writes go through the pipeline; reads from hot state, archive and rollups", "pipeline/processor.py, api/routers/fleet.py"],
         ["Outbox-like atomic write", "Parquet file carries its consumer offsets", "adapters/archive.py"],
         ["Circuit breaker / fallback", "Claude errors or refusals fall back to the workflow; registry unreachable keeps the last table", "agent/claude_agent.py, pipeline/normalizer_worker.py"],
         ["Observer / pub-sub", "Workers publish metric snapshots; the API aggregates", "pipeline/metrics.py"],
         ["Supervisor", "Restart crashed workers in local mode", "pipeline/runtime.py"]],
        [2, 4.4, 2.6])
    A["6.4 Interfaces, Contracts & Runtime Flows"] = "".join([
        d.p("**API contract.** OpenAPI 3.1, 50 operations, served at `/api/docs` and committed at docs/api/openapi.json. Path versioning "
            "(`/api/v1`); additive changes do not bump it. Keyset pagination with opaque cursors (no offsets). Errors are RFC 9457 "
            "problem+json. Rate limits per caller with `Retry-After`. A contract test fails the build if an operation disappears or a "
            "parameter becomes required, and another checks that every call the web console makes exists in the API."),
        d.table(["Topic", "Key", "Partitions", "Format", "Semantics"],
                [["telemetry.raw", "device id", "32", "bytes as sent, headers: oem, rx, ct", "at-least-once, 24 h"],
                 ["telemetry.canonical", "VIN", "32", "JSON, canonical schema v1", "at-least-once, idempotent sinks"],
                 ["telemetry.dlq", "source", "6", "original bytes, headers: reason, field, detail, attempts", "14 days, replayable"],
                 ["fleet.alerts", "VIN", "6", "JSON", "at-least-once, unique in the database"],
                 ["ops.metrics / ops.control", "worker / sim", "1", "JSON", "operational"]],
                [1.8, 1.1, 1, 3.2, 2]),
        d.p("**Schema evolution.** The canonical event is JSON Schema (draft 2020-12), generated from one definition. Rule: the seven "
            "required fields and their types never change; new fields are optional; consumers must tolerate unknown fields "
            "(`additionalProperties: true`). A contract test pins this."),
        d.image(dg / "seq_onboarding.png", 6.2, "Flow 1: a new maker is onboarded without downtime."),
        d.image(dg / "seq_failure.png", 6.2, "Flow 2 (failure path): a processor dies between writing a file and committing. It resumes from the offsets inside its newest file, so nothing is stored twice."),
    ])
    a = alg
    A["6.5 Algorithms & Data Structures"] = "".join([
        d.table(["Problem", "Algorithm", "Complexity", "Scale tested, measured"],
                [["Exact dedup of 100K events/s", "Per-vehicle replay window (64-bit mask); vectorised batch version", "O(1) per event, 2 ints per vehicle",
                  f"1M events, 100K vehicles: {a['replay_window_scalar']['seconds']} s scalar, {a['replay_window_vectorised']['seconds']} s vectorised"],
                 ["Late events", "Rotating Bloom filter, exact store only on 'maybe'", "O(k), m bits",
                  f"1M inserts: FP {a['bloom_filter']['measured_false_positive_rate']:.3%} (theory {a['bloom_filter']['theory_at_this_fill']:.3%}), {a['bloom_filter']['bytes'] / 1e6:.1f} MB"],
                 ["Most failing fields", "Count-Min sketch + lazy top-K heap", "O(d) add, O(log K) heap",
                  f"500K events, {n(a['count_min_topk']['distinct'])} keys: top-10 recall {a['count_min_topk']['top10_recall']:.0%}, {a['count_min_topk']['seconds']} s"],
                 ["Trips from noisy GPS", "Viterbi DP over STOP/MOVE, time-weighted costs", "O(n) time and memory",
                  f"86,400 points: {a['trip_segmentation_86400']['seconds']} s, {a['trip_segmentation_86400']['dp_segments']} segments vs {n(a['trip_segmentation_86400']['threshold_segments'])} by threshold"],
                 ["One-to-one field assignment", "Hungarian (Kuhn-Munkres, potentials)", "O(n² m)", f"200 x 205: {a['hungarian_200x205']['seconds'] * 1000:.0f} ms; checked against scipy"],
                 ["Group dead-letter shapes", "Union-find over Jaccard similarity", "O(s² f), α(n) per union",
                  f"120 shapes of 6 formats: {a['union_find_shape_clustering']['families_found']} families found"],
                 ["Map cells, masking", "Geohash, vectorised", "O(precision)", f"1M points: {a['geohash']['vectorised_seconds']} s"],
                 ["Percentiles over a stream", "Log-bucket histogram, mergeable", "O(1) record, O(B) query",
                  f"1M samples: p99 {n(a['latency_histogram']['p99_estimate'])} vs exact {n(a['latency_histogram']['p99_exact'])} (bound 8%)"],
                 ["VIN, DTC parsing", "Regex + ISO 3779 check digit; DTC with lookarounds", "O(length)",
                  f"100K each: {a['vin_check_digit']['per_item_us']} µs and {a['dtc_extraction']['per_item_us']} µs per item"]],
                [1.7, 2.6, 1.7, 3], size=15),
        d.p("**Pseudocode, the two central ones.**"),
        d.code("""check(vehicle, seq):                       # replay window, O(1)
    top, mask = state[vehicle]              # absent: store (seq, 1) -> NEW
    if seq > top: mask = (mask << (seq-top)) | 1 (64 bits); top = seq -> NEW
    back = top - seq
    if back >= 64: -> STALE (Bloom filter, then exact store on 'maybe')
    if mask has bit back: -> DUPLICATE
    set bit back -> NEW

segment(ts, speed):                          # Viterbi, O(n)
    cost[i][s] = -log P(speed_i | s) * seconds_covered_i
    dp[i][s]   = cost[i][s] + min(dp[i-1][s], dp[i-1][1-s] + penalty)
    backtrack from argmin dp[n-1]; runs of equal state are trips and stops"""),
        d.p("Full write-up with the rest of the pseudocode: docs/algorithms.md. Every algorithm has unit tests, several against an oracle "
            "(scipy for Hungarian, the scalar window for the vectorised one, exact counts for Count-Min)."),
    ])

    # ------------------------------------------------------------------ 7
    b = burst
    T["7. Non-Functional Requirements & Performance Benchmarks"] = d.table(
        ["NFR", "Target (Case Study)", "Achieved", "How Measured"],
        [["Ingest Throughput", "100K+ events/sec", f"{n(st_t['sent_per_s_mean'])} sent, {n(st_t['normalized_per_s_mean'])} translated per s (90 s, 100K vehicles); 3x burst survived with 0 lost",
          "scripts/bench_pipeline.py, ten processes"],
         ["End-to-End Latency", "< 2 s dashboard; < 5 s critical alert", f"p95 {n(st_l['ingest_to_dashboard_p95_median'])} ms, p99 {n(st_l['ingest_to_dashboard_p99_median'])} ms; alerts p99 {n(st_l['alert_p99_worst'])} ms worst. During a burst: up to {n(b['latency_ms']['ingest_to_dashboard_p99_worst'] / 1000, 0)} s until the backlog drains",
          "receive stamp at the gateway to hot-state write, log histograms"],
         ["API Latency", "p95 < 200 ms; p99 < 500 ms", (f"p95 {locust['p95']} ms, p99 {locust['p99']} ms at {n(locust['rps'])} requests/s, {locust['failures']} failures" if locust else "see docs/evidence/locust"),
          "Locust, 3 user types, while the pipeline ran at full load"],
         ["Resilience", "Recovers after broker / pod failure", f"{len(chaos['killed'])} processes SIGKILLed under load: all restarted, 0 lost, 0 stored twice" if chaos else "",
          "tests/chaos/kill_and_recover.py, accounting from the topics"],
         ["Availability", "99.9%, no single point of failure", "Design: replicas, PDBs, Kafka RF 3, RDS Multi-AZ, readiness vs liveness probes. Not measured over time.",
          "Helm chart and Terraform; readiness returns 503 when a dependency is down"]],
        [1.4, 1.8, 3.8, 2])
    A["7. Non-Functional Requirements & Performance Benchmarks"] = "".join([
        d.p("**Setup.** One laptop: Apple M4, 10 cores, 16 GB. Local mode: file-backed partitioned log (8 partitions), SQLite, memory-mapped "
            "hot state, Parquet on disk. 100,000 simulated vehicles in two simulator processes, five normalisers, two processors, one "
            "dead-letter worker, all as separate processes. Injected faults: 1.5% duplicates, 2% reordered, 0.05% malformed."),
        d.image(dg / "bench_steady.png", 6.3, "Steady state: throughput, latency percentiles and backlog over 90 s."),
        d.image(dg / "bench_burst.png", 6.3, "Burst: 3x requested for 20 s (shaded). The simulator reached about 2x on this laptop. Percentiles are over a rolling 60 s window, so they lag."),
        d.p(f"**Burst, honestly.** The laptop could generate about {n(b['burst']['sent_per_s_mean'])} events per second on average in the burst "
            f"(peaks near 220,000), not 300,000, and for 20 s, not five minutes. The pipeline caught up at 140,000 to 175,000 per second, the "
            f"backlog peaked at {n(b['burst']['lag_max'])} events and drained within about 25 s of the burst ending, with 0 events lost. Latency "
            "targets are not met during a burst on one machine. The arithmetic for a cluster: one normaliser core handles about 25,000 events "
            "per second, so 300,000 needs 12 normaliser cores, within the HPA limit of 16. `make bench-burst` runs the full five-minute test; "
            "an attempt on this laptop was stopped because, with the disk 95% full and other workloads running, the same code reached only "
            "half its usual throughput."),
        (d.image(dg / "bench_soak.png", 6.3, "Soak: 50,000 events per second for 10 minutes.") +
         d.image(dg / "bench_soak_memory.png", 6.3, "Soak: resident memory of every process over 10 minutes.") +
         d.p(f"**Soak.** {soak['config']['seconds'] // 60} minutes at {n(soak['throughput']['normalized_per_s_mean'])} events per second: "
             f"{n(soak['accounting']['simulator_sent'])} events, {soak['accounting']['unaccounted']} unaccounted, p99 "
             f"{n(soak['latency_ms']['ingest_to_dashboard_p99_median'])} ms, no restarts. Memory: normalisers and processors move within "
             "about 20 MB with no clear trend; the simulator drifts up about 60 MB. Ten minutes cannot rule out a slow leak; an hours-long "
             "soak is on the list of next steps. The first soak run found a bug (two processors trimming the "
             "archive raced on one file); it was fixed and the soak repeated.")) if soak else "",
    ])

    # ------------------------------------------------------------------ 8
    A["8. Security & Compliance"] = "".join([
        d.table(["STRIDE", "Top threat", "Control", "Test"],
                [["Spoofing", "A device publishes as another; a forged or alg:none token", "mTLS with CN-to-topic ACL; fixed JWT algorithm list and required claims; OIDC option", "9 token variants rejected"],
                 ["Tampering", "A mapping that silently mistranslates; audit history edited", "Whitelist specs, hold-out golden set, human approval, canary with auto rollback; hash-chained audit with verify", "chain breaks detected at the exact row"],
                 ["Repudiation", "Who approved this? What did the agent do?", "Every transition and every agent tool call recorded with actor, input, output", "one audit entry per agent step"],
                 ["Information disclosure", "Cross-tenant reads; analysts seeing positions", "Tenant from the token only, 404 for others' objects; geohash masking and short VINs", "cross-tenant and masking tests"],
                 ["Denial of service", "Intake or API flood", "Back-pressure with hysteresis, size limits, token buckets, capped pages", "limits and 429 tests"],
                 ["Elevation of privilege", "Read-only role or agent changes production", "Role checks per endpoint and again in the registry (ALLOWED_ACTORS)", "10 endpoints x 2 roles; 5 agent actions refused"]],
                [1.3, 2.4, 3.3, 2], size=15),
        d.bullet("**Authentication and authorisation:** OAuth2 password grant with JWT locally, OIDC (RS256, JWKS) in production; Argon2id "
                 "password hashes; per-account login throttling; five roles; tenant isolation on every query."),
        d.bullet("**Device identity and encryption:** mTLS at EMQX (demo CA script included), TLS 1.3 minimum on the gateway, KMS encryption "
                 "at rest for RDS, MSK, ElastiCache and S3 in Terraform; secrets from environment or a secret store, never in the image; "
                 "a 32-character JWT secret is required outside local mode."),
        d.bullet("**Privacy (GDPR, DPDP Act 2023):** personal data only in `driver`; location masked to about 5 km for analysts; retention "
                 "tiers defined; erasure nulls identity, cuts every link from telemetry to the person, verifies by reading back, and records "
                 "the evidence."),
        d.bullet("**AI safety:** payload text reaches the model only as JSON inside tool results; strict tool schemas; the agent has no tool "
                 "that changes production and the registry refuses it anyway; hold-out golden set; a person approves; everything audited. "
                 "A test simulates a model that obeys injected text: the calls fail and nothing live changes."),
        d.p("**Scans, run and fixed.** bandit 0; Semgrep 5 findings fixed to 0; pip-audit 8 to 0; npm audit 10 (1 critical) to 0 by "
            "upgrading vite, vitest and react-router; Trivy on the file system 2 high to 0, on the image every fixable finding removed "
            "(271 Debian findings remain with no published fix); OWASP ZAP baseline 1 medium to 0, and an authenticated API scan over all "
            "50 operations with no server error (its one 'high' is a false positive on rate-limited 429 answers). The fixes include a "
            "stricter CSP without 'unsafe-inline', Cross-Origin-Resource-Policy, and a hardened image. Reports and triage: "
            "docs/evidence/security/. Threat model: docs/security/threat-model.md."),
    ])

    # ------------------------------------------------------------------ 9
    T["9. Test Strategy"] = d.table(
        ["Test Type", "Tools", "No. of Tests", "Coverage / Result", "In CI?"],
        [["Unit", "pytest, fakeredis, scipy as oracle", "2,575", "core modules 89-100% each; all pass", "Yes"],
         ["Integration & Contract", "pytest, real local adapters; Testcontainers for Kafka 3.9, PostgreSQL 16, TimescaleDB, pgvector, Redis 7; JSON Schema; OpenAPI snapshot; Pact (pact-js and pact-python, consumers and providers)", "76 + 13 + 20 + 8", "all pass", "Yes"],
         ["Acceptance (BDD)", "behave (Gherkin)", "21 scenarios, 143 steps", "21 passed", "Yes"],
         ["Performance / Load / Soak", "bench_pipeline.py, Locust", "steady, burst, soak; API load", f"{n(st_t['normalized_per_s_mean'])} events/s; p99 {n(st_l['ingest_to_dashboard_p99_median'])} ms", "manual job"],
         ["Security (SAST, DAST, Dependency, Image)", "66 security tests (OWASP API Top 10), bandit, Semgrep, pip-audit, npm audit, Trivy fs and image, ZAP baseline and API scan", "66 + 9 scans", "every gate passes after fixes; triage in docs/evidence/security", "Yes"],
         ["Compliance & Chaos", "erasure and audit-chain tests; SIGKILL chaos script; gateway restart and SIGKILL in Compose", "6 checks + 1", "all pass: 0 lost, 0 duplicated; 615,464 of 615,464 over MQTT", "Yes"]],
        [1.8, 3.2, 1.4, 1.8, 0.8], size=15)
    AF["9. Test Strategy"] = "".join([
        d.p(f"**Coverage:** {cov_pct:.1f}% of lines over every suite that needs no Docker (2,737 tests)."),
        d.p("**Edge cases covered:** duplicates up to 30%, out-of-order delivery, events older than the dedup window, truncated and empty "
            "payloads, corrupted VIN check digits, oversize payloads, unknown sources, format drift for part of a fleet, a worker killed "
            "between batches, after producing but before committing, and after writing a file but before committing; a replay run twice; "
            "forged tokens; cross-tenant access; injection strings in every parameter; path traversal."),
        d.p("**What the tests found.** Unit tests were written independently of the code with one rule: a test that finds a bug is marked "
            "as an expected failure, never weakened. That found 16 real defects, among them a torn log write that made every later record "
            "unreadable, a shutdown that dropped already-acknowledged MQTT messages, a train/test name leak in the model's data, and "
            "latitudes near the equator misread (Chennai, Kochi). All fixed; the model was retrained and its reported accuracy went "
            "slightly down, which is the honest number. An integration test found that the agent's repair loop never ran; the soak test "
            "found an archive race. Both fixed."),
        d.p("**Reports:** docs/evidence/ (coverage.json, bench_*.json, chaos.json, sql_explain.md, ml_field_mapper.json, algorithms.json)."),
    ])

    # ------------------------------------------------------------------ 10
    A["10. Observability"] = "".join([
        d.image(sh / "insights.png", 6.3, "Batch insights: data quality per source over the archive, and the model against its baseline."),
        d.p("**Signals.** Metrics: every worker publishes a snapshot per second (counts per source, version and reason, latency "
            "histograms, consumer lag, workers reporting); the API merges them and exposes Prometheus metrics at `/metrics` behind a "
            "bearer token; a Grafana dashboard and 12 alert rules are provisioned (lag, dead-letter rate, p95 over 2 s, workers gone, API "
            "down). Logs: JSON lines with request ids and trace ids, secrets redacted, collected from every container by Grafana Alloy into "
            "Loki. Traces: OpenTelemetry spans for sampled batches, the W3C traceparent carried in record headers from the gateway through "
            "the normaliser to the processor, stored in Tempo. Grafana links a log line to its trace. All of it was run in the Compose stack."),
        d.image(sh / "grafana_pipeline.png", 6.3, "The Grafana dashboard of the Compose stack, provisioned from infra/grafana."),
        d.p("**Troubleshooting a latency spike.** (1) Grafana: is ingest-to-dashboard p95 up for every source or one? (2) Consumer lag: "
            "which stage is behind, normaliser or processor? (3) If the normaliser: throughput per source and version shows whether a "
            "canary or a fallback-heavy source costs more attempts per event; the fallbacks counter confirms it. (4) Open a sampled trace "
            "for a slow batch: the span durations separate decode and map from produce. (5) Logs by request id or worker id for errors "
            "and restarts; the supervisor's restart count shows crash loops (this is how the archive race was found)."),
    ])

    # ------------------------------------------------------------------ 11
    A["11. AI / ML Component (if used)"] = "".join([
        d.p("**Purpose.** Decide which source field is which canonical field, and in which unit. Rules alone fail because names are "
            f"arbitrary (`fahrt.v`, `c7`, `SPD`) and units are invisible: a name-matching baseline gets {bs['exact_label_accuracy']:.1%}."),
        d.p(f"**Data and features.** No public corpus exists, so {ml['data']['train_dialects']} training and {ml['data']['test_dialects']} test "
            "formats are generated from the fleet simulator with random names, units, nesting, encodings and distractor fields. 304 features "
            "per field: 256 hashed character n-grams of the name, 38 value statistics, 10 physics features. Leakage controls: test names "
            "never occur in training (enforced in code; a test found and closed a leak), tokens of the two demo formats are removed from "
            "training, and training and test fleets use different seeds and regions."),
        d.p("**Model and agent design.** ExtraTrees (300 trees) predicts one of 37 labels (canonical field plus unit, or ignore); the "
            "Hungarian algorithm turns probabilities into a one-to-one assignment. The agent has seven tools: sample dead letters, profile "
            "fields (with physics), search mapping memory (pgvector, HNSW cosine), suggest mapping, learn event codes, validate on the "
            "development half of the golden set, submit one draft. A deterministic workflow uses them in a fixed order with a repair loop; "
            "with an API key, Claude (`claude-opus-5-5`) drives the same tools with strict schemas."),
        d.image(dg / "ml_vs_baseline.png", 6.2, "Model against the name-matching baseline on formats with names never seen in training."),
        d.table(["Metric", "Model", "Baseline"],
                [["field and unit right", f"{ms['exact_label_accuracy']:.1%}", f"{bs['exact_label_accuracy']:.1%}"],
                 ["field right", f"{ms['field_accuracy']:.1%}", f"{bs['field_accuracy']:.1%}"],
                 ["distractor fields wrongly mapped", f"{ms['distractors_wrongly_mapped']:.1%}", f"{bs['distractors_wrongly_mapped']:.1%}"],
                 ["macro F1", f"{ms['macro_f1']:.3f}", f"{bs['macro_f1']:.3f}"],
                 ["whole format right after assignment", f"{ml['whole_dialect']['model_fully_correct']:.1%}", f"{ml['whole_dialect']['baseline_fully_correct']:.1%}"]],
                [4, 2, 2]),
        d.p("**Guardrails and cost.** Hallucination is checked, not trusted: a draft becomes `validated` only at 99% or more of known answers "
            "including a hold-out half; a person approves; canary with automatic rollback. Failures: refusal or API error falls back to the "
            "deterministic engine with the reason on the run record; a step limit (40) and turn limit (16) bound cost. Cost: the deterministic "
            "engine runs in under 1 s locally at no API cost. The Claude engine sends a few thousand tokens per turn for 4 to 8 turns, a few "
            "cents per onboarding at list prices; it was tested against a stub client, not the live API (no key was available)."),
        d.p("**Limit.** All formats come from one simulator; real OEM feeds are messier. Expect lower accuracy, which the golden set and "
            "the human gate are there to catch."),
    ])

    # ------------------------------------------------------------------ 12
    A["12. Architecture Decisions, Risks & Future Enhancements"] = "".join([
        d.table(["ADR", "Decision", "Key consequence"],
                [["0001", "Mappings are data from a whitelist, compiled to specialised code; never scripts", "Agent cannot run code; 1.6x faster than interpreting; a missing transform is one reviewed code change"],
                 ["0002", "One store per kind of data, with a CAP choice each (PostgreSQL CP, Redis/Kafka/Parquet AP)", "Five stores in production, each with a local adapter so tests need none"],
                 ["0003", "At-least-once delivery with idempotent consumers, not Kafka transactions", "Sinks outside Kafka stay exact; offsets stored inside Parquet files"],
                 ["0004", "Registry epoch hot reload, canary routing by device hash, replay of parked messages", "Zero-downtime onboarding; several versions live at once"],
                 ["0005", "The agent proposes, people decide; one toolbox for two engines", "Useful without a model; the model adds judgement, never power"]],
                [0.6, 4.4, 4]),
        d.p("**Risks and technical debt.** Everything was measured on one laptop with the local adapters, not on a cluster. The Claude "
            "engine is untested against the live API. The Compose stack was run end to end; the Helm chart and Terraform were validated with "
            "their tools but not installed or applied. The model's accuracy "
            "is on synthetic formats. Python caps per-core throughput (about 25,000 events per second per normaliser); a compiled normaliser "
            "would cut the core count several-fold. The web console is a demonstration of the API, not a hardened product (for example, it "
            "keeps the token in session storage)."),
        d.p("**Next three steps to a pilot.** (1) Install the Helm chart on a managed cluster, and repeat the benchmarks at 3x for "
            "five minutes with real Kafka (`make bench-burst` is ready; this laptop was too loaded to run it fairly). (2) Put one real OEM feed from an open dataset through the agent, and build "
            "its golden set from hand-labelled samples. (3) Enable the Claude engine and compare it with the deterministic one on formats "
            "the model finds hard."),
    ])

    # ------------------------------------------------------------------ 13
    T["13. Demo Video (5 Minutes Maximum)"] = d.table(
        ["Time", "Segment", "What to Show"],
        [["0:00 – 0:30", "Problem", "Six makers, one braking event written six ways (landing page). One number: every format change means lost data today."],
         ["0:30 – 1:00", "Solution", "\"One canonical event from every maker, and a new maker joins without downtime.\" Architecture in one image."],
         ["1:00 – 3:00", "Live Demo", "Live console at 100K vehicles. Start \"A new maker starts sending\": Helix parked. Run the agent: physics finds m/s; 200/200 known answers. Try on parked traffic, approve. Helix appears on the chart, dead letters drain, no restart. Then \"Firmware update\": canary v2 at 25% beside v1."],
         ["3:00 – 4:15", "Under the Hood", "Kill a normaliser from the console: backlog rises and drains. Burst chart. Audit log with the chain intact; the agent's refused approval. Map as an analyst (masked)."],
         ["4:15 – 5:00", "Impact & Next Steps", "Results table; limits stated; next steps; team."]],
        [1.2, 1.6, 6.2])

    AF["14. Repository Checklist"] = d.table(
        ["Item", "Status"],
        [["README: problem, architecture, quick start, environment variables, tests, known issues", "Done (README.md)"],
         ["One-command run", "Done and run: `make run` (no infrastructure) or `make up` (full stack; 20 healthy services in 108 s from empty volumes)"],
         ["Structure: service folders, /docs, /infra, /tests", "Done"],
         ["CI pipeline: build, lint, all suites, Pact, security scans on every push", "Written (.github/workflows/ci.yml), passes actionlint, every action pin checked; has not run yet: the repository is not on GitHub"],
         ["Hygiene: no secrets, .env.example, commits from all members", "No secrets; .env.example present; commits are the team's to make"],
         ["Final tag v1.0-submission", "To do after the team commits"]],
        [5, 4])

    A["15. Conclusion"] = "".join([
        d.bullet("**Learnings.** Measuring changes the design: the specialised code path, the vectorised dedup and the columnar processor "
                 "all came from profiles, and together took the laptop from 79,000 to 100,000 translated events per second. Independent tests and "
                 "long runs find what careful writing misses: 18 real bugs, all fixed."),
        d.bullet("**Strengths.** A real answer to the problem statement's question \"how do we onboard a new OEM format without "
                 "downtime\", with every step measured: throughput, latency, loss, recovery, model against baseline, queries before and after."),
        d.bullet("**Challenges solved.** Exactly-once results on at-least-once delivery across four sinks; units without names; an agent "
                 "that is useful yet cannot break production; one codebase that runs with and without infrastructure."),
    ])
    A["16. Declarations"] = "".join([
        d.bullet("**Open-source components:** Python: FastAPI, Starlette, Pydantic, SQLAlchemy, psycopg2, uvicorn (BSD/MIT); numpy, scipy, "
                 "scikit-learn, joblib (BSD); pyarrow (Apache-2.0); orjson, xxhash (Apache-2.0/MIT, BSD); protobuf (BSD); PyJWT (MIT); "
                 "argon2-cffi (MIT); confluent-kafka (Apache-2.0); redis-py (MIT); paho-mqtt (EPL-2.0/EDL-1.0); anthropic (MIT); "
                 "OpenTelemetry (Apache-2.0); pytest, behave, locust, testcontainers (MIT/BSD/Apache). Web: React, React Router, Vite, "
                 "Leaflet (MIT/BSD-2), Manrope and JetBrains Mono fonts (OFL). Infrastructure images: Kafka, PostgreSQL, TimescaleDB, "
                 "pgvector, Redis, EMQX, MinIO, Prometheus, Grafana. `make sbom` writes the installed versions."),
        d.bullet("**AI tools used:** Claude (Anthropic) as a coding assistant for implementation, tests, infrastructure files and "
                 "documentation, and as an optional runtime component of the mapping agent (Claude API)."),
        d.bullet("**Data:** all vehicles, drivers, tenants and makers are synthetic; e-mail addresses use the reserved `.example` domain; "
                 "no real personal or vehicle-owner data is used."),
        d.bullet("**Assets:** one photograph is from Wikimedia Commons (CC BY 2.0, credited on the landing page and in "
                 "docs/CREDITS.md); two images were generated with Google Gemini; the rest were drawn for this project."),
    ])
    AF["17. Appendix (if any)"] = "".join([
        d.image(sh / "dead-letters.png", 6.2, "Dead letters grouped into format families by union-find."),
        d.image(sh / "source-detail.png", 6.2, "A maker with live version 1 and canary version 2 after a firmware update."),
        d.image(sh / "fleet.png", 6.2, "Fleet map and alerts."),
        d.image(sh / "compliance.png", 6.2, "Audit trail with a verified hash chain."),
        d.p("**References:** the problem statement; ADRs in docs/adr; docs/architecture.md, data-model.md, algorithms.md, testing.md, "
            "security/threat-model.md; evidence in docs/evidence."),
    ])

    cover = {"Problem Space Chosen:": "Multi-OEM data normalisation: onboarding new and changed OEM formats without downtime",
             "Date of Submission:": "30/09/2026"}
    return {"cover": cover, "answers": A, "tables": T, "after": AF, "video": None}
