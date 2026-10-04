"""Consumer side: what the dead-letter worker needs from a dead letter.

A dead letter is the original raw payload, unchanged, with the reason in the
record headers. The worker (`rosetta.pipeline.dlq.DlqWorker`) treats the payload
as opaque bytes (it only hashes its shape), so the body is "any JSON object".
What it does rely on is the headers: `oem`, `reason`, `field`, `detail`,
`dead_ts`, `dev` and `attempts` for grouping and tracking, and `rx` to give a
replayed message its original arrival time.

On the broker the headers are record headers; in the pact they are the message
metadata. Each message is fed through the real worker: indexed into a group,
then (for format problems) replayed onto the raw topic.
"""
from __future__ import annotations

import pytest
from pact import Pact, match

from .conftest import DLQ_WORKER, NORMALIZER, PACT_DIR, as_bytes

SOURCE = r"^[a-z0-9_]+$"


def headers(oem: str, reason: str, reason_re: str, fld: str, fld_re: str, detail: str, dead_ts: int) -> dict:
    return {
        "oem": match.regex(oem, regex=SOURCE),
        "reason": match.regex(reason, regex=reason_re),
        "field": match.regex(fld, regex=fld_re),
        "detail": match.string(detail),
        "dead_ts": match.integer(dead_ts),
        "dev": match.string("he-0000007"),
        "attempts": match.integer(1),
        "rx": match.integer(dead_ts - 20),
    }


MESSAGES: dict[str, dict] = {
    # NO_ADAPTER, SCHEMA_MISMATCH and TRANSFORM_ERROR mean "a format we cannot read yet":
    # the worker tracks devices for the mapping agent and can replay these later.
    "a message in a format no live mapping can read": headers(
        "helix", "NO_ADAPTER", r"^(NO_ADAPTER|SCHEMA_MISMATCH|TRANSFORM_ERROR)$", "", r"^[a-z_.\[\]0-9]*$",
        "no live mapping for this source", 1_790_000_001_000),
    # Everything else is a damaged or impossible message: kept as evidence, grouped by field.
    "a message that decoded but failed canonical validation": headers(
        "nordvik", "INVALID", r"^(INVALID|DECODE_ERROR|OVERSIZE)$", "speed_kmh", r"^[a-z_]+$",
        "OUT_OF_RANGE: 999.0", 1_790_000_002_000),
}


@pytest.fixture(scope="module")
def pact(seeded):
    p = Pact(DLQ_WORKER, NORMALIZER).with_specification("V4")
    for name, meta in MESSAGES.items():
        (p.upon_receiving(name, "Async")
          .given("the normaliser has a live mapping for nordvik and none for helix")
          # The payload is opaque to the worker: any JSON object will do.
          .with_body({}, "application/json")
          .with_metadata(meta))
    yield p
    p.write_file(PACT_DIR, overwrite=True)


@pytest.fixture(scope="module")
def worker(seeded):
    from rosetta.factory import make_broker
    from rosetta.pipeline.dlq import DlqWorker

    return DlqWorker(broker=make_broker())


def test_dlq_worker_can_index_and_replay_every_dead_letter(pact, worker):
    from sqlalchemy import select

    from rosetta.db.models import DlqGroup, DlqSample
    from rosetta.db.session import session_scope
    from rosetta.pipeline.dlq import FORMAT_REASONS, queue_replay
    from rosetta.ports.broker import T_DLQ, T_RAW, Record

    raw = worker.broker.consumer(T_RAW, "pact-raw", start="beginning")
    handled: list[str] = []

    def handle(body: str | bytes | None, meta: dict) -> None:
        payload = as_bytes(body)
        hdr = {k: v for k, v in meta.items() if k != "contentType"}
        worker.broker.produce(T_DLQ, [Record(key=str(hdr["oem"]).encode(), value=payload, headers=hdr)])
        assert worker.step(0.0) == 1, "the worker did not index the dead letter"
        with session_scope() as s:
            g = s.execute(select(DlqGroup).where(DlqGroup.oem_key == hdr["oem"],
                                                 DlqGroup.reason == hdr["reason"])).scalar_one()
            assert g.count == 1 and g.field == hdr["field"] and g.shape.startswith("json:")
            assert g.first_seen == hdr["dead_ts"]
            assert s.execute(select(DlqSample).where(DlqSample.group_id == g.id)).scalars().first().payload == payload
        if hdr["reason"] in FORMAT_REASONS:
            with session_scope() as s:
                job = queue_replay(s, hdr["oem"]).id
            assert worker.run_replay(job)["republished"] == 1
            out = raw.poll(10, 0.0)
            assert len(out) == 1 and out[0].value == payload
            assert out[0].headers["oem"] == hdr["oem"] and out[0].headers["rx"] == hdr["rx"]
            assert out[0].headers["replay"] == 1 and out[0].key == hdr["dev"].encode()
        handled.append(hdr["reason"])

    errors = pact.verify(handle, "Async", raises=False)
    assert not errors, "; ".join(f"{e.description}: {e.error!r}" for e in errors)
    assert len(handled) == len(MESSAGES)
