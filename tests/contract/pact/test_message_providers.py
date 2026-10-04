"""Provider side of the message pacts: the real code produces every message.

* `rosetta-normalizer` is verified against what the processor and the
  dead-letter worker expect. Raw payloads come from the fleet simulator in each
  OEM's own wire format and go through the real `Normalizer` with the routing
  table loaded from the seeded mapping registry, exactly as the worker does it.
* `rosetta-processor` is verified against what an alert subscriber expects.
  Its input is itself produced by the real normaliser, so the chain
  simulator -> normaliser -> processor -> alert is covered end to end.

The Pact verifier asks for each message by its description, compares the body
and metadata with the matchers in the committed pact file, and reports every
mismatch.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import orjson
import pytest
from pact import Verifier

from .conftest import (
    ALERT_SUBSCRIBER,
    DLQ_WORKER,
    NORMALIZER,
    PROCESSOR,
    SEED_VALUE,
    SEED_VEHICLES,
    pact_file,
)

STATES = {
    "the normaliser has a live mapping for the source",
    "the normaliser has a live mapping for nordvik and none for helix",
    "the vehicle is known to the processor",
}


class Traffic:
    """Raw telemetry from the simulated fleet (the same fleet the database was seeded with)."""

    def __init__(self) -> None:
        from rosetta.simulator.dialects import DIALECTS
        from rosetta.simulator.fleet import OEM_KEYS, Fleet

        self.dialects = {d.oem: d for k, d in DIALECTS.items() if k == d.oem}
        self.fleet = Fleet(SEED_VEHICLES, seed=SEED_VALUE)
        self.oem_keys = OEM_KEYS
        self.clock = 1_790_300_000_000
        self.step()

    def step(self) -> None:
        self.clock += 1000
        self.fleet.step(1.0, self.clock)
        self.fleet.emitting("all")          # every vehicle reports: advances each sequence number

    def vehicle(self, oem: str, pick: int = 0) -> int:
        idx = [i for i in range(self.fleet.n) if self.oem_keys[self.fleet.oem[i]] == oem]
        return idx[pick]

    def raw(self, i: int, **headers: Any):
        """The raw record the gateway would put on the raw topic for vehicle i."""
        import numpy as np

        from rosetta.ports.broker import Record

        oem = self.oem_keys[self.fleet.oem[i]]
        d = self.dialects[oem]
        payload = d.encode(self.fleet.truth(np.asarray([i])))[0]
        hdr = {"oem": oem, "rx": self.clock + 40, "ct": d.content_type, **headers}
        return Record(key=str(self.fleet.device_id[i]).encode(), value=payload, headers=hdr)


def normalise(*records) -> Any:
    """Run the real normaliser over raw records, with the live routing table from the registry."""
    from rosetta.engine.dedup import Deduplicator
    from rosetta.engine.normalizer import Normalizer
    from rosetta.pipeline.normalizer_worker import load_table

    return Normalizer(load_table(), Deduplicator()).process(list(records), now_ms=int(records[-1].headers["rx"]) + 5)


def as_message(rec) -> dict[str, Any]:
    """A produced record as Pact sees it: body plus record headers as metadata."""
    return {"contents": rec.value, "metadata": dict(rec.headers), "content_type": "application/json"}


def only(recs: list) -> Any:
    assert len(recs) == 1, f"expected one record, got {len(recs)}"
    return recs[0]


# --------------------------------------------------------------- normaliser
def normaliser_messages(traffic: Traffic) -> dict[str, Callable[[], dict[str, Any]]]:
    from rosetta.simulator.fleet import EVT_CODE

    def canonical(oem: str, pick: int = 0, **hdr: Any) -> Callable[[], dict[str, Any]]:
        def make() -> dict[str, Any]:
            traffic.step()
            res = normalise(traffic.raw(traffic.vehicle(oem, pick), **hdr))
            return as_message(only(res.canonical))
        return make

    def with_event() -> dict[str, Any]:
        i = traffic.vehicle("nordvik", 1)
        traffic.step()
        traffic.fleet.evt[i] = EVT_CODE["HARSH_BRAKE"]
        return as_message(only(normalise(traffic.raw(i)).canonical))

    def with_codes() -> dict[str, Any]:
        i = traffic.vehicle("nordvik", 2)
        traffic.step()
        traffic.fleet.dtc[i] = ["P0420"]
        return as_message(only(normalise(traffic.raw(i)).canonical))

    def unmapped() -> dict[str, Any]:
        traffic.step()
        return as_message(only(normalise(traffic.raw(traffic.vehicle("helix"))).dead))

    def impossible() -> dict[str, Any]:
        traffic.step()
        rec = traffic.raw(traffic.vehicle("nordvik", 3))
        body = orjson.loads(rec.value)
        body["motion"]["speedKmh"] = 999.0           # decodes fine, fails canonical validation
        rec.value = orjson.dumps(body)
        return as_message(only(normalise(rec).dead))

    return {
        # what the processor reads
        "a canonical event": canonical("stellaris"),
        "a canonical event from an electric vehicle": canonical("voltaic"),
        "a canonical event from a combustion vehicle": canonical("nordvik"),
        "a canonical event carrying a driving event": with_event,
        "a canonical event carrying trouble codes": with_codes,
        "a canonical event replayed from the dead-letter queue": canonical("kaizen", replay=1, attempts=1),
        # what the dead-letter worker reads
        "a message in a format no live mapping can read": unmapped,
        "a message that decoded but failed canonical validation": impossible,
    }


# ---------------------------------------------------------------- processor
def processor_messages(traffic: Traffic) -> dict[str, Callable[[], dict[str, Any]]]:
    from rosetta.factory import make_broker
    from rosetta.pipeline.processor import Processor
    from rosetta.ports.broker import T_ALERTS
    from rosetta.simulator.fleet import EVT_CODE

    proc = Processor(broker=make_broker(), group="pact-provider")
    alerts = proc.broker.consumer(T_ALERTS, "pact-provider-alerts", start="end")

    def run(*raw) -> Any:
        for r in raw:
            proc.handle(normalise(r).canonical)
        return as_message(only(alerts.poll(100, 0.0)))

    def driving_event() -> dict[str, Any]:
        i = traffic.vehicle("nordvik", 4)
        traffic.step()
        traffic.fleet.evt[i] = EVT_CODE["HARSH_BRAKE"]
        return run(traffic.raw(i))

    def new_code() -> dict[str, Any]:
        i = traffic.vehicle("voltaic", 1)
        traffic.step()
        traffic.fleet.dtc[i] = ["P0301"]
        first = traffic.raw(i)              # the first sighting is state, not news
        traffic.step()
        traffic.fleet.evt[i] = 0
        traffic.fleet.dtc[i] = ["P0301", "P0420"]
        return run(first, traffic.raw(i))

    return {
        "an alert raised from a driving event": driving_event,
        "an alert raised from a new trouble code": new_code,
    }


# ------------------------------------------------------------------- verify
def check_state(state: str, action: str = "setup", **_: Any) -> None:
    # Every state is true in the seeded world; an unknown one means the consumer
    # asked for something this provider test does not set up.
    assert state in STATES, f"unknown provider state: {state!r}"


def verify(provider: str, consumers: list[str], handlers: dict[str, Callable[[], dict[str, Any]]]) -> str:
    v = (Verifier(provider).message_handler(handlers).state_handler(check_state)
         .set_error_on_empty_pact(enabled=True).set_coloured_output(enabled=False))
    for c in consumers:
        f = pact_file(c, provider)
        assert f.exists(), f"missing {f.name}: run the consumer tests first"
        v.add_source(f)
    try:
        v.verify()
    except RuntimeError as e:        # the Rust core only says "failed": attach its report
        raise AssertionError(f"{provider} does not honour its pacts:\n{v.output()}") from e
    return str(v.output())


@pytest.fixture(scope="module")
def traffic(seeded):
    return Traffic()


def test_normaliser_honours_the_processor_and_dead_letter_pacts(traffic):
    out = verify(NORMALIZER, [PROCESSOR, DLQ_WORKER], normaliser_messages(traffic))
    assert out.count("has a matching body (OK)") == 8


def test_processor_honours_the_alert_subscriber_pact(traffic):
    out = verify(PROCESSOR, [ALERT_SUBSCRIBER], processor_messages(traffic))
    assert out.count("has a matching body (OK)") == 2


def test_a_producer_that_breaks_the_contract_is_caught(traffic):
    """The verification is not vacuous: drop one field the processor needs and it fails."""
    handlers = normaliser_messages(traffic)
    good = handlers["a canonical event"]

    def without_rx_ts() -> dict[str, Any]:
        m = good()
        ev = orjson.loads(m["contents"])
        del ev["rx_ts"]
        return {**m, "contents": orjson.dumps(ev)}

    handlers["a canonical event"] = without_rx_ts
    with pytest.raises(AssertionError) as e:
        verify(NORMALIZER, [PROCESSOR], handlers)
    assert "Actual map is missing the following keys: rx_ts" in str(e.value)
