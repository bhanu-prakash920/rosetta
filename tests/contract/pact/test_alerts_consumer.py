"""Consumer side: what a subscriber of the `fleet.alerts` topic may rely on.

No service in this repository reads `fleet.alerts` yet (the console reads the
alert table the processor also writes). The topic is the integration point for
downstream systems such as notification or ticketing services, so this pact
pins down what such a subscriber gets: who, what, how bad, when, and where.
The handler below is the smallest realistic subscriber: it turns an alert into
a notification line.
"""
from __future__ import annotations

from datetime import UTC, datetime

import orjson
import pytest
from pact import Pact, match

from .conftest import ALERT_SUBSCRIBER, PACT_DIR, PROCESSOR, as_bytes

VIN = r"^[A-HJ-NPR-Z0-9]{17}$"
SEVERITY = r"^(warning|critical)$"
DTC = r"^[PCBU][0-3][0-9A-F]{3}$"


def alert(kind, kind_re: str, detail: dict) -> dict:
    return {
        "vin": match.regex("7NVGT4B73TA000000", regex=VIN),
        "kind": match.regex(kind, regex=kind_re),
        "severity": match.regex("warning", regex=SEVERITY),
        "ts": match.integer(1_790_000_002_000),
        "detected_ts": match.integer(1_790_000_002_080),
        "detail": {
            **detail,
            "lat": match.number(17.331685),
            "lon": match.number(78.60904),
            "oem": match.regex("nordvik", regex=r"^[a-z0-9_]+$"),
        },
    }


MESSAGES = {
    "an alert raised from a driving event": alert(
        "HARSH_BRAKE", r"^(HARSH_BRAKE|HARSH_ACCEL|SPEEDING|LOW_SOC)$", {"speed_kmh": match.number(64.0)}),
    "an alert raised from a new trouble code": alert(
        "DTC", r"^DTC$", {"codes": match.each_like(match.regex("P0420", regex=DTC), min=1)}),
}


def to_notification(alert: dict) -> str:
    """What a notification service would do with an alert."""
    when = datetime.fromtimestamp(alert["ts"] / 1000, UTC).strftime("%H:%M:%S")
    d = alert["detail"]
    what = ", ".join(d["codes"]) if alert["kind"] == "DTC" else f"{round(d['speed_kmh'])} km/h"
    lag_ms = alert["detected_ts"] - alert["ts"]
    return (f"[{alert['severity'].upper()}] {alert['kind']} {alert['vin']} ({d['oem']}) at {when}: {what} "
            f"near {d['lat']:.3f},{d['lon']:.3f}, detected after {lag_ms} ms")


@pytest.fixture(scope="module")
def pact():
    p = Pact(ALERT_SUBSCRIBER, PROCESSOR).with_specification("V4")
    for name, body in MESSAGES.items():
        (p.upon_receiving(name, "Async")
          .given("the vehicle is known to the processor")
          .with_body(body, "application/json"))
    yield p
    p.write_file(PACT_DIR, overwrite=True)


def test_subscriber_can_turn_every_alert_into_a_notification(pact):
    lines: list[str] = []

    def handle(body: str | bytes | None, _meta: dict) -> None:
        lines.append(to_notification(orjson.loads(as_bytes(body))))

    errors = pact.verify(handle, "Async", raises=False)
    assert not errors, "; ".join(f"{e.description}: {e.error!r}" for e in errors)
    assert len(lines) == len(MESSAGES)
    assert any("P0420" in x for x in lines) and any("64 km/h" in x for x in lines)
