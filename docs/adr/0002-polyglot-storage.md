# ADR 0002: One store per kind of data, and the CAP choice for each

Status: accepted

## Context

The problem statement asks for relational, NoSQL and vector storage, each justified,
and explains why one SQL database fails at 100,000 events per second: index write
amplification, connection contention, analytics starving transactions, and a single
primary that cannot scale writes. Some data still needs strict ACID guarantees:
ownership, subscriptions, access control, audit, and here the mapping registry.

## Decision

| Data | Store | Why | CAP / PACELC |
|---|---|---|---|
| Tenants, fleets, vehicles, drivers, users, roles, subscriptions, mapping registry, golden cases, agent runs, audit log | PostgreSQL (SQLite in local mode), Third Normal Form | Transactions: an approval, its action row, its audit entry and the registry epoch commit together or not at all | CP. Under a partition we prefer refusing an approval to approving twice. PC/EC: consistency over latency |
| Latest state per vehicle | Redis (memory-mapped file in local mode) | Sub-millisecond reads for the map and the agent. One fixed-size row per vehicle, 92 bytes, 9.2 MB for the fleet | AP, PA/EL. A read may be a second old. Last-write-wins by event time makes replays harmless |
| Telemetry history | Parquet on S3-compatible storage (local disk in local mode), optionally TimescaleDB | Columnar and compressed: 41.8 bytes per event against about 330 for the JSON. Readable by Spark, DuckDB, Trino, Snowflake without an export | AP. Eventually complete: a file appears once its micro-batch is written |
| Dead-letter queue, raw and canonical streams | Kafka (file-backed partitioned log in local mode) | Durable, partitioned, replayable. Replay is how onboarding works (ADR 0003) | AP within a partition. Per-key order kept |
| Mapping memory (field embeddings) | pgvector in PostgreSQL, HNSW cosine index | Nearest known field for a new one. In the same database as the registry, so no second server | Same as the relational core |
| Per-minute metrics | `metric_minute` table, a deliberate rollup | Charts without scanning events | Derived data, rebuilt from the stream |

## Consequences

- Five stores in production is operational weight. Each one has a local adapter
  behind the same interface (`rosetta/ports/`), so development and the test suite
  need none of them, and the integration suite runs the production adapters
  against real Kafka, PostgreSQL, pgvector and Redis in Docker.
- Deliberate denormalisation is limited and documented in `docs/data-model.md`:
  the mapping spec JSON next to its normalised field rows, one sample payload on
  each dead-letter group, and the metric rollup.
- TimescaleDB is optional. Managed PostgreSQL services such as RDS do not offer it,
  so Parquet is the history of record and TimescaleDB an accelerator where it exists.
