# ADR 0004: Zero-downtime onboarding through hot reload, canary routing and replay

Status: accepted

## Context

The question from the problem statement: how do we onboard a new OEM format without
downtime. Two situations: a maker we have never seen, and a maker whose firmware
update changed the format for part of its fleet.

## Decision

Four mechanisms, each small:

1. **Park, do not drop.** A message no live mapping can read goes to the dead-letter
   topic with a reason code (`NO_ADAPTER`, `SCHEMA_MISMATCH`, `DECODE_ERROR`,
   `TRANSFORM_ERROR`, `INVALID`, `OVERSIZE`). Nothing is lost while a mapping is
   written. Dead letters have their own, larger retention budget.
2. **Registry epoch.** Every change to what is live bumps one integer in the same
   transaction. Workers check it every half second and swap in a freshly compiled
   routing table between two batches. One attribute assignment: a batch in flight
   finishes on the table it started with.
3. **Canary routing.** A version can be live for a share of vehicles, chosen by
   hashing the device id, so a vehicle never flips between versions. Several
   versions can be live at once, which is the normal state after an over-the-air
   update. The router tries the version that last worked for the device first,
   then the others, and the canary last for messages nothing else reads.
   A guard in the API takes a canary out of traffic when it produces invalid events
   (more than 200 faults and more than 5 percent in a minute), recorded as a system
   action.
4. **Replay.** Approving or promoting a version queues a replay of the source's
   parked messages. Replayed events are flagged, keep their original receive time,
   and are excluded from latency figures.

## Consequences

- Measured in `tests/integration/test_pipeline.py`: the normaliser object is the
  same before and after onboarding, every parked message comes back, a second
  replay adds nothing, and during drift both firmware versions are read.
- A maker that sends binary data without a schema cannot be mapped automatically.
  The agent says so and asks for the descriptor instead of guessing.
