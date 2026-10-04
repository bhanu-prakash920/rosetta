"""rosetta.pipeline.mqtt_gateway: from MQTT topics to the raw topic."""
from __future__ import annotations

import signal
import threading
import time
from types import SimpleNamespace

import orjson
import pytest

from rosetta.adapters.log_broker import LogBroker
from rosetta.pipeline import mqtt_gateway as MG
from rosetta.pipeline.gateway import Backpressure
from rosetta.pipeline.mqtt_gateway import MqttGateway, parse_topic
from rosetta.ports.broker import T_METRICS, T_RAW


class StubClient:
    """Stands in for the paho client: records what the gateway does with it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def loop_start(self) -> None:
        self.calls.append("loop_start")

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


@pytest.fixture()
def broker(tmp_path) -> LogBroker:
    return LogBroker(tmp_path / "log", partitions=2)


@pytest.fixture()
def gw(broker, monkeypatch) -> MqttGateway:
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 3600.0)      # the tests decide when to flush
    return MqttGateway(broker=broker, client=StubClient())


def raw_records(broker: LogBroker) -> list:
    consumer = broker.consumer(T_RAW, "test", start="beginning")
    out = []
    while True:
        got = consumer.poll(1000, 0.0)
        if not got:
            return sorted(out, key=lambda r: (r.key, r.value))
        out.extend(got)


# ----------------------------------------------------------------- parse_topic
@pytest.mark.parametrize("topic,expected", [
    ("oem/helix/hx-0000012/telemetry", ("helix", "hx-0000012")),
    ("oem/nordvik/no-0000001/telemetry", ("nordvik", "no-0000001")),
    ("oem/" + "s" * 32 + "/" + "d" * 64 + "/telemetry", ("s" * 32, "d" * 64)),
    ("oem/a b/c d/telemetry", ("a b", "c d")),
])
def test_parse_topic(topic, expected):
    assert parse_topic(topic) == expected


@pytest.mark.parametrize("topic", [
    "", "oem", "oem/helix", "oem/helix/hx-1", "oem/helix/hx-1/telemetry/extra", "/oem/helix/hx-1/telemetry",
    "OEM/helix/hx-1/telemetry", "fleet/helix/hx-1/telemetry", "oem/helix/hx-1/status", "oem/helix/hx-1/Telemetry",
    "oem//hx-1/telemetry", "oem/helix//telemetry", "oem/helix/hx-1/telemetry/",
    "oem/" + "s" * 33 + "/hx-1/telemetry", "oem/helix/" + "d" * 65 + "/telemetry",
    "$share/gateway/oem/helix/hx-1/telemetry",
])
def test_parse_topic_rejects_anything_else(topic):
    assert parse_topic(topic) is None


# ------------------------------------------------------------------ on_message
def test_message_is_buffered_until_the_next_flush(gw, broker):
    gw.on_message("oem/helix/hx-0000012/telemetry", b'{"kopf": {}}')
    assert (gw.received, gw.n, gw.forwarded) == (1, 1, 0)
    assert raw_records(broker) == []
    assert gw.flush() == 1
    assert (gw.n, gw.forwarded) == (0, 1)


def test_flushed_message_reaches_the_raw_topic_with_source_and_device(gw, broker):
    before = int(time.time() * 1000)
    gw.on_message("oem/helix/hx-0000012/telemetry", b'{"kopf": {}}')
    gw.flush()
    rec = raw_records(broker)[0]
    assert (rec.key, rec.value) == (b"hx-0000012", b'{"kopf": {}}')
    assert rec.headers["oem"] == "helix"
    assert before <= rec.headers["rx"] <= int(time.time() * 1000)


def test_messages_are_grouped_by_source(gw, broker):
    for i in range(3):
        gw.on_message(f"oem/helix/hx-{i}/telemetry", f"h{i}".encode())
        gw.on_message(f"oem/nordvik/no-{i}/telemetry", f"n{i}".encode())
    assert sorted(gw.buf) == ["helix", "nordvik"]
    assert gw.flush() == 6
    got = raw_records(broker)
    assert [(r.headers["oem"], r.key, r.value) for r in got] == \
        [("helix", f"hx-{i}".encode(), f"h{i}".encode()) for i in range(3)] + \
        [("nordvik", f"no-{i}".encode(), f"n{i}".encode()) for i in range(3)]


def test_payload_is_copied_out_of_the_network_buffer(gw, broker):
    buffer = bytearray(b"payload")
    gw.on_message("oem/helix/hx-1/telemetry", buffer)
    buffer[:] = b"XXXXXXX"                              # paho reuses its buffer
    gw.flush()
    assert raw_records(broker)[0].value == b"payload"


@pytest.mark.parametrize("topic", ["oem/helix/telemetry", "somewhere/else", "oem/helix/hx-1/status"])
def test_message_on_a_foreign_topic_is_counted_and_dropped(gw, broker, topic):
    gw.on_message(topic, b"x")
    assert (gw.received, gw.bad_topic, gw.n) == (1, 1, 0)
    assert gw.flush() == 0
    assert raw_records(broker) == []


def test_flush_of_nothing(gw):
    assert gw.flush() == 0
    assert gw.forwarded == 0


def test_buffer_is_flushed_when_it_is_full(gw, broker, monkeypatch):
    monkeypatch.setattr(MG, "FLUSH_AT", 5)
    for i in range(4):
        gw.on_message(f"oem/helix/hx-{i}/telemetry", b"x")
    assert gw.forwarded == 0
    gw.on_message("oem/helix/hx-4/telemetry", b"x")
    assert (gw.forwarded, gw.n) == (5, 0)
    assert len(raw_records(broker)) == 5


def test_buffer_is_flushed_when_it_is_old(broker, monkeypatch):
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 0.0)
    gw = MqttGateway(broker=broker, client=StubClient())
    gw.on_message("oem/helix/hx-1/telemetry", b"x")
    assert (gw.forwarded, gw.n) == (1, 0)


def test_per_device_order_is_kept(gw, broker):
    for i in range(50):
        gw.on_message("oem/helix/hx-7/telemetry", f"{i:03d}".encode())
        if i % 7 == 0:
            gw.flush()
    gw.flush()
    consumer = broker.consumer(T_RAW, "order", start="beginning")
    assert [r.value for r in consumer.poll(1000, 0.0)] == [f"{i:03d}".encode() for i in range(50)]


# --------------------------------------------------------------- back-pressure
def test_flush_waits_while_intake_is_paused(gw, broker, monkeypatch):
    attempts = []
    real_submit = gw.gateway.submit

    def submit(oem, items, *a, **kw):
        attempts.append(oem)
        if len(attempts) <= 3:
            raise Backpressure(0.25)
        return real_submit(oem, items, *a, **kw)

    sleeps = []
    monkeypatch.setattr(gw.gateway, "submit", submit)
    monkeypatch.setattr(MG, "time", SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic))
    gw.on_message("oem/helix/hx-1/telemetry", b"x")
    assert gw.flush() == 1
    assert sleeps == [0.25, 0.25, 0.25], "the callback blocks, so the MQTT broker holds the messages"
    assert gw.waits == 3
    assert len(raw_records(broker)) == 1, "nothing was dropped and nothing was sent twice"


def test_source_the_gateway_refuses_does_not_block_the_others(gw, broker, monkeypatch):
    real_submit = gw.gateway.submit

    def submit(oem, items, *a, **kw):
        if oem == "helix":
            raise ValueError("missing or invalid source id")
        return real_submit(oem, items, *a, **kw)

    monkeypatch.setattr(gw.gateway, "submit", submit)
    gw.on_message("oem/helix/hx-1/telemetry", b"x")
    gw.on_message("oem/nordvik/no-1/telemetry", b"y")
    assert gw.flush() == 1
    assert [r.headers["oem"] for r in raw_records(broker)] == ["nordvik"]


def test_buffered_messages_are_forwarded_on_shutdown(gw, broker):
    gw.on_message("oem/helix/hx-1/telemetry", b"last words")
    gw.running = False                                   # what the SIGTERM handler does
    assert gw.flush() == 1
    assert [r.value for r in raw_records(broker)] == [b"last words"]


# ------------------------------------------------------------------------- run
@pytest.fixture()
def keep_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_run_flushes_publishes_metrics_and_shuts_the_client_down(broker, monkeypatch, keep_sigterm):
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 0.01)
    gw = MqttGateway(broker=broker, client=StubClient())
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 3600.0)     # on_message must not flush by itself ...
    gw.on_message("oem/helix/hx-1/telemetry", b"a")
    gw.on_message("bad/topic", b"b")
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 0.01)       # ... the run loop does
    gw._last_pub = time.monotonic() - 5.0                # a metrics snapshot is due

    def stop_when_published() -> None:
        deadline = time.monotonic() + 5.0
        while gw.forwarded == 0 and gw.received and time.monotonic() < deadline:
            time.sleep(0.005)
        while gw.received and time.monotonic() < deadline:     # counters are reset by the snapshot
            time.sleep(0.005)
        gw.running = False

    stopper = threading.Thread(target=stop_when_published)
    stopper.start()
    gw.run()
    stopper.join(timeout=5.0)

    assert gw.client.calls == ["loop_start", "loop_stop", "disconnect"]
    assert [r.value for r in raw_records(broker)] == [b"a"]
    snapshots = [orjson.loads(r.value) for r in broker.consumer(T_METRICS, "t", start="beginning").poll(100, 0.0)]
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert (snap["svc"], snap["received"], snap["forwarded"], snap["bad_topic"], snap["backpressure_waits"]) == \
        ("gateway", 2, 1, 1, 0)
    assert (gw.received, gw.forwarded, gw.bad_topic, gw.waits) == (0, 0, 0, 0), "snapshots are deltas"


def test_run_installs_a_sigterm_handler_that_stops_the_loop(broker, keep_sigterm):
    gw = MqttGateway(broker=broker, client=StubClient())
    gw.running = False
    gw.run()
    handler = signal.getsignal(signal.SIGTERM)
    gw.running = True
    handler(signal.SIGTERM, None)
    assert gw.running is False
    assert gw.client.calls == ["loop_start", "loop_stop", "disconnect"]


# ------------------------------------------------ acknowledgement and sessions
class AckClient(StubClient):
    """A client with MQTT 5 manual acknowledgement, like paho with manual_ack=True."""

    def __init__(self) -> None:
        super().__init__()
        self.acked: list[int] = []
        self.disconnect_props = None

    def ack(self, mid: int, qos: int) -> None:
        assert qos == 1
        self.acked.append(mid)

    def unsubscribe(self, topic: str) -> None:
        self.calls.append(f"unsubscribe {topic}")

    def disconnect(self, properties=None) -> None:  # type: ignore[override]
        self.calls.append("disconnect")
        self.disconnect_props = properties


@pytest.fixture()
def agw(broker, monkeypatch) -> MqttGateway:
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 3600.0)
    return MqttGateway(broker=broker, client=AckClient())


def test_a_message_is_acknowledged_only_once_it_is_in_the_raw_topic(agw, broker):
    agw.on_message("oem/helix/hx-1/telemetry", b"a", 11)
    agw.on_message("oem/nordvik/no-1/telemetry", b"b", 12)
    assert agw.client.acked == [], "buffered is not delivered"
    agw.flush()
    assert agw.client.acked == [11, 12]
    assert len(raw_records(broker)) == 2


def test_a_message_on_a_foreign_topic_is_acknowledged_so_it_is_not_sent_again(agw):
    agw.on_message("bad/topic", b"x", 5)
    assert agw.client.acked == [5]


def test_messages_left_at_shutdown_stay_unacknowledged_for_the_broker(agw, monkeypatch):
    monkeypatch.setattr(MG, "SHUTDOWN_GRACE_S", 0.0)

    def refuse(*_a, **_k):
        raise Backpressure(0.0)

    monkeypatch.setattr(agw.gateway, "submit", refuse)
    agw.on_message("oem/helix/hx-1/telemetry", b"a", 21)
    agw.running = False
    assert agw.flush() == 0
    assert agw.client.acked == [], "the broker must keep and redeliver them"


def test_a_refused_source_is_acknowledged_and_dropped(agw, monkeypatch):
    def refuse(*_a, **_k):
        raise ValueError("unknown source")

    monkeypatch.setattr(agw.gateway, "submit", refuse)
    agw.on_message("oem/helix/hx-1/telemetry", b"a", 31)
    agw.flush()
    assert agw.client.acked == [31]


def test_shutdown_keeps_the_session_so_the_broker_queues_for_the_restart(broker, monkeypatch, keep_sigterm):
    """One gateway: leaving the group would make the broker drop what arrives until the
    gateway is back (messages.dropped.no_subscriber). So stop reading, forward what is
    buffered, and disconnect with the session kept."""
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 0.01)
    gw = MqttGateway(broker=broker, client=AckClient())
    gw.on_message("oem/helix/hx-1/telemetry", b"a", 41)
    gw.running = False
    gw.run()
    assert gw.client.calls == ["loop_start", "loop_stop", "disconnect"]
    assert gw.client.disconnect_props is None, "no session expiry override: the session stays"
    assert gw.client.acked == [41] and [r.value for r in raw_records(broker)] == [b"a"]


def test_shutdown_can_end_the_session_where_ids_change_and_others_run(broker, monkeypatch, keep_sigterm):
    monkeypatch.setattr(MG, "FLUSH_EVERY_S", 0.01)
    monkeypatch.setattr(MG, "END_SESSION_ON_EXIT", True)
    gw = MqttGateway(broker=broker, client=AckClient())
    gw.running = False
    gw.run()
    assert gw.client.calls == ["loop_start", f"unsubscribe {MG.SUBSCRIPTION}", "loop_stop", "disconnect"]
    assert gw.client.disconnect_props.SessionExpiryInterval == 0


def test_client_id_is_stable_and_configurable(monkeypatch):
    monkeypatch.delenv("ROSETTA_MQTT_CLIENT_ID", raising=False)
    first = MG.client_id()
    assert first == MG.client_id() and first.startswith("gateway-"), "no pid: same id after a restart"
    monkeypatch.setenv("ROSETTA_MQTT_CLIENT_ID", "gateway-0")
    assert MG.client_id() == "gateway-0"
