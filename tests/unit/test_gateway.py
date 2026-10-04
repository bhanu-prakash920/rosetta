"""rosetta.pipeline.gateway: the front door for raw payloads, with back-pressure."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rosetta.engine.decoders import MAX_PAYLOAD_BYTES
from rosetta.observability import tracing
from rosetta.pipeline import gateway as G
from rosetta.pipeline.gateway import Backpressure, Gateway
from rosetta.ports.broker import T_RAW


class FakeBroker:
    def __init__(self) -> None:
        self.produced: list[tuple[str, list]] = []

    def produce(self, topic, records) -> None:
        self.produced.append((topic, list(records)))

    @property
    def records(self) -> list:
        return [r for _, batch in self.produced for r in batch]


class Lag:
    """A lag probe whose reading the test controls."""

    def __init__(self, value: int = 0) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.value


def gateway_with_lag(lag: Lag, **kw) -> tuple[Gateway, FakeBroker]:
    broker = FakeBroker()
    return Gateway(broker, lag_fn=lag, high_watermark=1000, low_watermark=200, check_every_s=0.0, **kw), broker


ITEMS = [("dev-1", b"payload-1"), ("dev-2", b"payload-2")]


# --------------------------------------------------------------------- submit
def test_submit_appends_to_the_raw_topic_keyed_by_device():
    broker = FakeBroker()
    gw = Gateway(broker)
    assert gw.submit("nordvik", ITEMS, rx_ms=1_790_000_000_000) == 2
    assert [topic for topic, _ in broker.produced] == [T_RAW]
    assert [(r.key, r.value) for r in broker.records] == [(b"dev-1", b"payload-1"), (b"dev-2", b"payload-2")]
    assert all(r.headers == {"oem": "nordvik", "rx": 1_790_000_000_000} for r in broker.records)
    assert (gw.accepted, gw.rejected, gw.throttled) == (2, 0, 0)


def test_receive_time_is_stamped_when_not_given(monkeypatch):
    monkeypatch.setattr(G, "time", SimpleNamespace(time=lambda: 1_790_000_000.4567, monotonic=lambda: 1.0))
    broker = FakeBroker()
    Gateway(broker).submit("nordvik", ITEMS)
    assert {r.headers["rx"] for r in broker.records} == {1_790_000_000_456}


def test_content_type_and_extra_headers_are_carried():
    broker = FakeBroker()
    Gateway(broker).submit("kaizen", ITEMS, content_type="application/x-protobuf", rx_ms=5,
                           extra={"replay": True, "attempts": 2})
    assert broker.records[0].headers == {"oem": "kaizen", "rx": 5, "ct": "application/x-protobuf", "replay": True,
                                         "attempts": 2}


def test_payload_is_not_parsed_or_changed():
    broker = FakeBroker()
    blob = b"\x00\xff not json at all \x01"
    Gateway(broker).submit("nordvik", [("dev-1", blob)])
    assert broker.records[0].value == blob


def test_accepted_counts_accumulate():
    gw = Gateway(FakeBroker())
    gw.submit("nordvik", ITEMS)
    gw.submit("pacifica", ITEMS[:1])
    assert gw.accepted == 3


def test_submitting_nothing():
    broker = FakeBroker()
    gw = Gateway(broker)
    assert gw.submit("nordvik", []) == 0
    assert broker.records == [] and gw.accepted == 0


@pytest.mark.parametrize("oem", ["", None, "x" * 33])
def test_missing_or_overlong_source_id_is_rejected(oem):
    broker = FakeBroker()
    gw = Gateway(broker)
    with pytest.raises(ValueError, match="source id"):
        gw.submit(oem, ITEMS)
    assert broker.produced == []
    assert (gw.accepted, gw.rejected) == (0, 2)


def test_source_id_of_32_characters_is_accepted():
    assert Gateway(FakeBroker()).submit("x" * 32, ITEMS) == 2


def test_source_id_is_checked_before_back_pressure():
    lag = Lag(10_000)
    gw, _ = gateway_with_lag(lag)
    with pytest.raises(ValueError):
        gw.submit("", ITEMS)
    assert lag.calls == 0 and gw.throttled == 0


def test_item_without_a_device_id_is_dropped():
    broker = FakeBroker()
    gw = Gateway(broker)
    assert gw.submit("nordvik", [("", b"a"), ("dev-2", b"b")]) == 1
    assert [r.key for r in broker.records] == [b"dev-2"]
    assert (gw.accepted, gw.rejected) == (1, 1)


def test_absurdly_large_payload_is_refused_at_the_door():
    broker = FakeBroker()
    gw = Gateway(broker)
    limit = MAX_PAYLOAD_BYTES * 4
    assert gw.submit("nordvik", [("dev-1", b"x" * (limit + 1)), ("dev-2", b"x" * limit), ("dev-3", b"ok")]) == 2
    assert [r.key for r in broker.records] == [b"dev-2", b"dev-3"]
    assert gw.rejected == 1


def test_payload_above_the_normaliser_limit_still_passes_the_gateway():
    # It must reach the normaliser, which dead-letters it as OVERSIZE where operators can see it.
    assert Gateway(FakeBroker()).submit("nordvik", [("dev-1", b"x" * (MAX_PAYLOAD_BYTES + 1))]) == 1


# -------------------------------------------------------------- back-pressure
def test_without_a_lag_probe_the_gateway_always_accepts():
    gw = Gateway(FakeBroker())
    assert gw.accepting() is True


def test_intake_pauses_above_the_high_watermark():
    lag = Lag(1001)
    gw, broker = gateway_with_lag(lag)
    with pytest.raises(Backpressure) as info:
        gw.submit("nordvik", ITEMS)
    assert info.value.retry_after_s == gw.check_every_s
    assert broker.produced == []
    assert (gw.accepted, gw.throttled) == (0, 2)


def test_hysteresis():
    lag = Lag(0)
    gw, _ = gateway_with_lag(lag)
    readings = [(100, True), (900, True), (1000, True), (1001, False),      # pauses above high
                (900, False), (500, False), (200, False),                   # stays paused down to low
                (199, True),                                                # resumes below low
                (900, True), (1000, True),                                  # and stays open up to high
                (5000, False), (0, True)]
    for value, expected in readings:
        lag.value = value
        assert gw.accepting() is expected, f"lag {value}"


def test_submit_works_again_after_the_backlog_is_gone():
    lag = Lag(5000)
    gw, broker = gateway_with_lag(lag)
    for _ in range(3):
        with pytest.raises(Backpressure):
            gw.submit("nordvik", ITEMS)
    lag.value = 500
    with pytest.raises(Backpressure):
        gw.submit("nordvik", ITEMS)
    lag.value = 100
    assert gw.submit("nordvik", ITEMS) == 2
    assert (gw.accepted, gw.throttled) == (2, 8)
    assert len(broker.records) == 2


def test_lag_is_probed_at_most_once_per_interval(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(G, "time", SimpleNamespace(time=lambda: 1.0, monotonic=lambda: clock.now))
    lag = Lag(5000)
    gw = Gateway(FakeBroker(), lag_fn=lag, high_watermark=1000, low_watermark=200, check_every_s=0.5)
    assert gw.accepting() is False and lag.calls == 1

    lag.value = 0
    clock.now += 0.49
    assert gw.accepting() is False, "the previous reading is reused inside the interval"
    assert lag.calls == 1

    clock.now += 0.02
    assert gw.accepting() is True
    assert lag.calls == 2


def test_default_watermarks():
    gw = Gateway(FakeBroker())
    assert (gw.high, gw.low, gw.check_every_s) == (600_000, 200_000, 0.5)


def test_backpressure_exception():
    err = Backpressure(1.25)
    assert err.retry_after_s == 1.25
    assert "1.2" in str(err) and "retry" in str(err)
    assert isinstance(err, Exception)


# -------------------------------------------------------------------- tracing
def test_no_trace_header_when_tracing_is_off():
    tracing.reset()
    broker = FakeBroker()
    Gateway(broker).submit("nordvik", ITEMS)
    assert "tp" not in broker.records[0].headers


def test_trace_context_travels_in_the_record_headers(monkeypatch):
    tracing.reset()
    exporter = InMemorySpanExporter()
    try:
        assert tracing.setup("gateway", exporter=exporter) is True
        monkeypatch.setattr(tracing, "RATIO", 1.0)
        broker = FakeBroker()
        Gateway(broker).submit("nordvik", ITEMS)
        tp = broker.records[0].headers["tp"]
        span = exporter.get_finished_spans()[0]
        assert span.name == "gateway.submit"
        assert dict(span.attributes) == {"oem": "nordvik", "records": 2}
        assert tp.split("-")[1] == format(span.context.trace_id, "032x")
    finally:
        tracing.reset()
