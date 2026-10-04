"""Consumer side: what the stream processor needs from a canonical event.

The processor reads the canonical topic with an explicit Arrow schema
(`rosetta.pipeline.processor.CANON`). The messages below state which of those
fields a producer must send, with types and formats but no exact values:

* every event: the seven required canonical fields plus the lineage fields the
  processor stores or measures with (`oem`, `map_v`, `rx_ts`, `norm_ts`)
* optional fields only in the message that is about them (an EV sends
  `soc_pct`, a combustion car `fuel_pct`, a driving event `evt`, ...)

Each message is fed through the real `Processor.handle`, so the contract fails
if the processor could not use what the pact promises.
"""
from __future__ import annotations

import orjson
import pytest
from pact import Pact, match

from rosetta.domain.canonical import EVENT_TYPES

from .conftest import NORMALIZER, PACT_DIR, PROCESSOR, as_bytes

VIN = r"^[A-HJ-NPR-Z0-9]{17}$"
SOURCE = r"^[a-z0-9_]+$"
EVENT = "^(" + "|".join(EVENT_TYPES) + ")$"
DTC = r"^[PCBU][0-3][0-9A-F]{3}$"

# Example values come from the seeded test fleet, so the processor recognises the vehicles.
ICE_VIN = "7NVGT4B73TA000000"      # nordvik, combustion
EV_VIN = "8VTEV1A22TA000000"       # voltaic, electric


def base(seq: int, vin: str = ICE_VIN, oem: str = "nordvik") -> dict:
    """What every canonical event carries. Each example has its own `seq`: the processor
    drops a repeated (vin, seq) pair as a duplicate, which is the behaviour we want."""
    return {
        "vin": match.regex(vin, regex=VIN),
        "ts": match.integer(1_790_000_000_000 + seq * 1000),
        "seq": match.integer(seq),
        "lat": match.number(17.331685),
        "lon": match.number(78.60904),
        "speed_kmh": match.number(54.2),
        "odo_km": match.number(83_782.1),
        "oem": match.regex(oem, regex=SOURCE),
        "map_v": match.integer(1),
        "rx_ts": match.integer(1_790_000_000_050 + seq * 1000),
        "norm_ts": match.integer(1_790_000_000_060 + seq * 1000),
    }


MESSAGES: dict[str, dict] = {
    "a canonical event": base(1),
    "a canonical event from an electric vehicle": {
        **base(2, EV_VIN, "voltaic"),
        "soc_pct": match.number(65.1),
        "heading_deg": match.number(239.4),
        "ambient_c": match.number(28.5),
        "ignition": match.boolean(True),
    },
    "a canonical event from a combustion vehicle": {
        **base(3),
        "fuel_pct": match.number(82.5),
        "heading_deg": match.number(195.4),
        "ambient_c": match.number(28.5),
        "ignition": match.boolean(True),
    },
    "a canonical event carrying a driving event": {
        **base(4),
        "evt": match.regex("HARSH_BRAKE", regex=EVENT),
    },
    "a canonical event carrying trouble codes": {
        **base(5),
        "dtc": match.each_like(match.regex("P0420", regex=DTC), min=1),
    },
    "a canonical event replayed from the dead-letter queue": {
        **base(6),
        "replayed": match.boolean(True),
    },
}


@pytest.fixture(scope="module")
def pact(seeded):
    p = Pact(PROCESSOR, NORMALIZER).with_specification("V4")
    for name, body in MESSAGES.items():
        (p.upon_receiving(name, "Async")
          .given("the normaliser has a live mapping for the source")
          .with_body(body, "application/json"))
    yield p
    p.write_file(PACT_DIR, overwrite=True)


@pytest.fixture(scope="module")
def processor(seeded):
    from rosetta.factory import make_broker
    from rosetta.pipeline.processor import Processor

    return Processor(broker=make_broker(), write_db_alerts=False)


def test_processor_can_use_every_canonical_message(pact, processor):
    from rosetta.ports.broker import T_ALERTS, Record

    alerts = processor.broker.consumer(T_ALERTS, "pact-alerts", start="beginning")
    seen: list[str] = []

    def handle(body: str | bytes | None, metadata: dict) -> None:
        raw = as_bytes(body)
        ev = orjson.loads(raw)
        before = processor.n_events
        n = processor.handle([Record(key=ev["vin"].encode(), value=raw)])
        assert n == 1, "the processor dropped the event"
        assert processor.n_events == before + 1
        state = processor.hot.get(ev["vin"])
        assert state is not None, "the event did not reach the hot state"
        assert state["ts"] == ev["ts"] and abs(state["lat"] - ev["lat"]) < 1e-6
        if "soc_pct" in ev:
            assert abs(state["soc_pct"] - ev["soc_pct"]) < 0.01
        if "fuel_pct" in ev:
            assert abs(state["fuel_pct"] - ev["fuel_pct"]) < 0.01
        if ev.get("evt") in ("HARSH_BRAKE", "HARSH_ACCEL", "SPEEDING", "LOW_SOC"):
            out = alerts.poll(100, 0.0)
            assert [orjson.loads(r.value)["kind"] for r in out] == [ev["evt"]], "no alert was raised"
        seen.append(ev["vin"])

    errors = pact.verify(handle, "Async", raises=False)
    assert not errors, "; ".join(f"{e.description}: {e.error!r}" for e in errors)
    assert len(seen) == len(MESSAGES)
    processor.flush_archive()          # the stored columns accept what the pact sends
    assert processor.n_archived == len(MESSAGES)
