# ADR 0003: At-least-once delivery with idempotent consumers, not Kafka transactions

Status: accepted

## Context

Vehicles resend on timeouts, networks duplicate, workers crash between writing an
output and committing their input position. We need every event stored once.

## Options considered

1. **Exactly-once with Kafka transactions** (`read_committed`, transactional
   producer, offsets committed in the transaction). Correct for Kafka-to-Kafka,
   but our sinks are Redis, Parquet and PostgreSQL, which are outside the
   transaction. Throughput cost and operational complexity are significant.
2. **At-most-once.** Commit before processing. Loses data on every crash.
3. **At-least-once plus idempotent consumers.** Commit after the outputs are
   durable. Make every consumer tolerate seeing a record twice.

## Decision

Option 3, with an idempotency mechanism per sink:

| Stage | Duplicate source | Mechanism |
|---|---|---|
| Normaliser | Device resends, simulator duplicates | Per-vehicle replay window: highest sequence number and a 64-bit mask of the ones below it. Exact, O(1). Older than the window: Bloom filter, then an exact store only on a "maybe" (Redis `SET NX EX` in production) |
| Processor | Normaliser redelivery after a crash | The same window, vectorised with numpy over the whole batch |
| Hot state | Replays | Last-write-wins by event time |
| Archive | Crash between "file written" and "offsets committed" | The consumer positions are stored inside the Parquet file's metadata. A restarted processor resumes from its newest file. Data and position become durable in one atomic rename |
| Alerts | Replays | Unique constraint (vehicle, kind, event time) with `ON CONFLICT DO NOTHING` |
| Registry, audit | Retries | Database transactions |

## Evidence

- `tests/chaos/kill_and_recover.py` sends SIGKILL to two normalisers, a processor
  and the dead-letter worker under load. Result in `docs/evidence/chaos.json`: all
  restarted, 119 canonical records were redelivered, the archive holds 717,448 rows
  and 717,448 distinct events, and every raw message is accounted for.
- `tests/integration/test_pipeline.py::test_crash_between_archive_write_and_commit_stores_nothing_twice`
  exercises the narrowest window directly.

## Consequences

The window is 64 sequence numbers per vehicle. A vehicle that resends something
older takes the Bloom path, which is exact but costs a store lookup on a "maybe".
Devices that reset their counter must be reset in the window (`ReplayWindow.reset`).
