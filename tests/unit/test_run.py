"""rosetta.simulator.run: settings, payload corruption and one tick of the runner."""
from __future__ import annotations

import time
from collections import Counter
from types import SimpleNamespace

import numpy as np
import orjson
import pytest

from rosetta.adapters.log_broker import LogBroker
from rosetta.domain.errors import NormalizeError
from rosetta.domain.vin import is_valid_vin
from rosetta.engine.compiler import compile_spec
from rosetta.engine.router import canary_bucket
from rosetta.factory import T_CONTROL
from rosetta.pipeline.gateway import Backpressure
from rosetta.ports.broker import T_METRICS, T_RAW
from rosetta.simulator import run as R
from rosetta.simulator.dialects import DIALECTS
from rosetta.simulator.fleet import OEM_KEYS, Fleet
from rosetta.simulator.run import SimSettings, SimulatorRunner, corrupt, send_control

T0 = 1_790_000_000_000
VIN = "1HGCM82633A004352"
CLEAN = dict(dup_pct=0.0, reorder_pct=0.0, malformed_pct=0.0)


@pytest.fixture()
def broker(tmp_path) -> LogBroker:
    return LogBroker(tmp_path / "log", partitions=2)


def runner(broker: LogBroker, vehicles: int = 300, seed: int = 7, **settings) -> SimulatorRunner:
    s = SimSettings(**{"mode": "all", "enabled": list(OEM_KEYS), **CLEAN, **settings})
    return SimulatorRunner(fleet=Fleet(vehicles, seed=seed, start_ms=T0), settings=s, broker=broker, seed=seed,
                           listen_control=False)


def raw(broker: LogBroker) -> list:
    c = broker.consumer(T_RAW, "test", start="beginning")
    out = []
    while True:
        got = c.poll(5000, 0.0)
        if not got:
            return out
        out.extend(got)


# ---------------------------------------------------------------- SimSettings
def test_defaults():
    s = SimSettings()
    assert (s.mode, s.hz, s.max_eps, s.paused) == ("realistic", 1.0, 0, False)
    assert s.enabled == ["nordvik", "pacifica", "stellaris", "kaizen", "voltaic"], "helix starts as the unknown OEM"
    assert (s.drift_pct, s.outage_pct, s.burst) == (0, 0, 1.0)
    assert (s.dup_pct, s.reorder_pct, s.malformed_pct) == (1.5, 2.0, 0.05)


def test_settings_do_not_share_the_enabled_list():
    a, b = SimSettings(), SimSettings()
    a.enabled.append("helix")
    assert "helix" not in b.enabled


def test_apply_changes_only_what_the_patch_names():
    s = SimSettings()
    s.apply({"hz": 2, "drift_pct": 30})
    assert (s.hz, s.drift_pct) == (2.0, 30)
    assert s.mode == "realistic" and s.dup_pct == 1.5


@pytest.mark.parametrize("key,given,kept", [
    ("hz", 0.01, 0.1), ("hz", 50, 10.0), ("hz", "2.5", 2.5),
    ("burst", -3, 0.0), ("burst", 500, 50.0), ("burst", 8, 8.0),
    ("dup_pct", -1, 0.0), ("dup_pct", 99, 50.0), ("dup_pct", "3.5", 3.5),
    ("reorder_pct", 75, 50.0), ("malformed_pct", -0.5, 0.0), ("malformed_pct", 0.25, 0.25),
    ("drift_pct", -10, 0), ("drift_pct", 40.9, 40), ("drift_pct", "25", 25),
    ("outage_pct", -1, 0), ("outage_pct", 30, 30), ("max_eps", -5, 0), ("max_eps", 2000, 2000),
    ("paused", 1, True), ("paused", 0, False), ("paused", True, True),
    ("mode", "all", "all"), ("mode", "realistic", "realistic"),
])
def test_apply_clamps_values(key, given, kept):
    s = SimSettings()
    s.apply({key: given})
    assert getattr(s, key) == kept
    assert type(getattr(s, key)) is type(kept)


@pytest.mark.parametrize("mode", ["turbo", "", None, 1, "ALL"])
def test_apply_ignores_an_unknown_mode(mode):
    s = SimSettings(mode="all")
    s.apply({"mode": mode})
    assert s.mode == "all"


def test_apply_filters_unknown_sources():
    s = SimSettings()
    s.apply({"enabled": ["helix", "acme", "nordvik", 5]})
    assert s.enabled == ["helix", "nordvik"]
    s.apply({"enabled": []})
    assert s.enabled == []


def test_apply_ignores_an_enabled_value_that_is_not_a_list():
    s = SimSettings()
    s.apply({"enabled": "helix"})
    assert "helix" not in s.enabled and len(s.enabled) == 5


def test_apply_ignores_unknown_keys():
    s = SimSettings()
    before = dict(vars(s))
    s.apply({"vehicles": 5, "__class__": "x", "apply": None})
    assert vars(s) == before
    assert callable(s.apply)


@pytest.mark.parametrize("patch", [{"hz": "fast"}, {"drift_pct": None}, {"dup_pct": [1]}])
def test_apply_raises_on_values_that_cannot_be_converted(patch):
    with pytest.raises((ValueError, TypeError)):
        SimSettings().apply(patch)


# -------------------------------------------------------------------- corrupt
def test_corrupt_is_deterministic_for_a_seed():
    payload = orjson.dumps({"vin": VIN, "speed": 12.5, "odo": 18234.5})
    a = [corrupt(payload, VIN, np.random.default_rng(3)) for _ in range(3)]
    assert a[0] == a[1] == a[2]


def test_corrupt_always_changes_the_payload():
    payload = orjson.dumps({"vin": VIN, "speed": 12.5, "odo": 18234.5})
    rng = np.random.default_rng(1)
    assert all(corrupt(payload, VIN, rng) != payload for _ in range(300))


def test_corrupt_truncates_or_breaks_the_check_digit():
    payload = orjson.dumps({"vin": VIN, "speed": 12.5, "odo": 18234.5})
    rng = np.random.default_rng(2)
    kinds = Counter()
    for _ in range(400):
        bad = corrupt(payload, VIN, rng)
        if len(bad) < len(payload):
            assert payload.startswith(bad) and 4 <= len(bad) < len(payload)
            kinds["truncated"] += 1
        else:
            assert len(bad) == len(payload)
            changed = [i for i in range(len(payload)) if bad[i] != payload[i]]
            assert changed == [payload.find(VIN.encode()) + 8], "only the check digit differs"
            assert not is_valid_vin(orjson.loads(bad)["vin"])
            kinds["vin"] += 1
    assert kinds["truncated"] > 120 and kinds["vin"] > 120


@pytest.mark.parametrize("vin", ["1HGCM82603A004352", "1HGCM82633A004352", "11111111111111111"])
def test_corrupt_breaks_the_check_digit_whatever_it_was(vin):
    payload = b"|" + vin.encode() + b"|"
    rng = np.random.default_rng(5)
    for _ in range(50):
        bad = corrupt(payload, vin, rng)
        if len(bad) == len(payload):
            assert not is_valid_vin(bad[1:18].decode())


def test_corrupt_halves_a_payload_that_does_not_contain_the_vin():
    payload = b"0123456789abcdefghij"
    rng = np.random.default_rng(4)
    results = {corrupt(payload, VIN, rng) for _ in range(100)}
    assert payload[:10] in results
    assert all(payload.startswith(r) and len(r) < len(payload) for r in results)


def test_corrupt_of_a_tiny_payload():
    rng = np.random.default_rng(4)
    assert {corrupt(b"abcd", VIN, rng) for _ in range(20)} == {b"ab"}
    assert corrupt(b"", VIN, rng) == b""


@pytest.mark.parametrize("key", sorted(DIALECTS))
def test_corrupted_payloads_are_rejected_by_the_mapping(key, truth):
    d = DIALECTS[key]
    adapter = compile_spec(d.oem, 1, d.spec)
    rng = np.random.default_rng(6)
    rejected = 0
    payloads = d.encode(truth)[:120]
    for p, vin in zip(payloads, truth["vin"]):
        try:
            adapter.normalize(corrupt(p, vin, rng))
        except NormalizeError:
            rejected += 1
    # A truncated Protobuf message can still be a complete, shorter message.
    assert rejected >= (100 if key == "kaizen" else 120)


# ----------------------------------------------------------------------- tick
def test_tick_sends_one_event_per_vehicle_in_mode_all(broker):
    sim = runner(broker)
    assert sim.tick(now_ms=T0 + 1000, dt=1.0) == 300
    records = raw(broker)
    assert len(records) == 300
    assert sorted(r.key.decode() for r in records) == sorted(sim.fleet.device_id)
    assert sim.sent == 300 and sum(sim.by_oem.values()) == 300
    assert (sim.dups, sim.malformed, sim.reordered, sim.throttled) == (0, 0, 0, 0)


def test_every_payload_is_readable_by_the_mapping_of_its_source(broker):
    sim = runner(broker)
    sim.tick(now_ms=T0 + 1000, dt=1.0)
    adapters = {k: compile_spec(k, 1, DIALECTS[k].spec) for k in OEM_KEYS}
    vin_of = dict(zip(sim.fleet.device_id, sim.fleet.vin))
    seen = Counter()
    for r in raw(broker):
        oem = r.headers["oem"]
        ev = adapters[oem].normalize(r.value)
        assert ev["vin"] == vin_of[r.key.decode()]
        assert ev["ts"] == T0 + 1000
        assert r.headers["ct"] == DIALECTS[oem].content_type
        seen[oem] += 1
    assert dict(seen) == sim.by_oem
    assert set(seen) == set(OEM_KEYS)


def test_source_matches_the_vehicle(broker):
    sim = runner(broker)
    sim.tick(now_ms=T0 + 1000, dt=1.0)
    oem_of = {d: OEM_KEYS[o] for d, o in zip(sim.fleet.device_id, sim.fleet.oem)}
    assert all(r.headers["oem"] == oem_of[r.key.decode()] for r in raw(broker))


def test_only_enabled_sources_send(broker):
    sim = runner(broker, enabled=["nordvik", "helix"])
    n = sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert {r.headers["oem"] for r in raw(broker)} == {"nordvik", "helix"}
    assert n == int(np.isin(sim.fleet.oem, [0, 5]).sum())


def test_nothing_enabled_sends_nothing(broker):
    sim = runner(broker, enabled=[])
    assert sim.tick(now_ms=T0 + 1000, dt=1.0) == 0
    assert raw(broker) == []


def test_sequence_numbers_advance_by_one_per_tick(broker):
    sim = runner(broker, enabled=["nordvik"])
    before = sim.fleet.seq.copy()
    for k in range(3):
        sim.tick(now_ms=T0 + (k + 1) * 1000, dt=1.0)
    assert (sim.fleet.seq == before + 3).all()
    adapter = compile_spec("nordvik", 1, DIALECTS["nordvik"].spec)
    per_vin: dict[str, list[int]] = {}
    for r in raw(broker):
        ev = adapter.normalize(r.value)
        per_vin.setdefault(ev["vin"], []).append(ev["seq"])
    assert all(sorted(seqs) == list(range(min(seqs), min(seqs) + 3)) for seqs in per_vin.values())


def test_realistic_mode_sends_fewer_events(broker):
    sim = runner(broker, mode="realistic")
    n = sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert 0 < n < 300


def test_two_runners_with_the_same_seed_send_the_same_bytes(tmp_path):
    out = []
    for name in ("a", "b"):
        b = LogBroker(tmp_path / name, partitions=1)
        sim = runner(b, vehicles=200, seed=11, dup_pct=5.0, malformed_pct=5.0)
        sim.tick(now_ms=T0 + 1000, dt=1.0)
        out.append([(r.key, r.value) for r in raw(b)])
    assert out[0] == out[1] and len(out[0]) > 150


def test_tick_uses_the_wall_clock_by_default(broker):
    sim = runner(broker, vehicles=50, hz=2.0)
    before = int(time.time() * 1000)
    sim.tick()
    assert before <= sim.fleet.now_ms <= int(time.time() * 1000)


# ---------------------------------------------------------------------- shards
def test_shards_split_the_fleet_without_overlap(tmp_path):
    keys = []
    for shard in range(3):
        b = LogBroker(tmp_path / f"s{shard}", partitions=1)
        s = SimSettings(mode="all", enabled=list(OEM_KEYS), **CLEAN)
        sim = SimulatorRunner(fleet=Fleet(300, seed=7, start_ms=T0), settings=s, broker=b, shard=shard, shards=3,
                              listen_control=False)
        assert sim.tick(now_ms=T0 + 1000, dt=1.0) == 100
        keys.append({r.key for r in raw(b)})
    assert not keys[0] & keys[1] and not keys[1] & keys[2] and not keys[0] & keys[2]
    assert len(keys[0] | keys[1] | keys[2]) == 300


def test_max_eps_caps_the_tick(broker):
    sim = runner(broker, max_eps=40)
    assert sim.tick(now_ms=T0 + 1000, dt=1.0) == 40
    assert len({r.key for r in raw(broker)}) == 40


def test_max_eps_is_per_second_not_per_tick(broker):
    sim = runner(broker, max_eps=40, hz=4.0)
    assert sim.tick(now_ms=T0 + 250, dt=0.25) == 10


# ---------------------------------------------------------------------- faults
def test_duplicates_are_sent_again_later(broker):
    sim = runner(broker, dup_pct=50.0)
    first = sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert first == 300, "every event is sent once right away"
    assert 100 < sim.dups < 200 and len(sim.delayed) == sim.dups
    released = sim._release(time.monotonic() + 60.0)
    assert released == sim.dups and sim.delayed == []
    counts = Counter((r.key, r.value) for r in raw(broker))
    assert sorted(counts.values()).count(2) == sim.dups
    assert set(counts.values()) == {1, 2}


def test_reordered_events_are_held_back(broker):
    sim = runner(broker, reorder_pct=50.0)
    first = sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert 100 < sim.reordered < 200
    assert first == 300 - sim.reordered
    assert sim._release(time.monotonic()) == 0, "held back for at least half a second"
    assert sim._release(time.monotonic() + 60.0) == sim.reordered
    assert len({r.key for r in raw(broker)}) == 300, "late, not lost"


def test_delayed_events_are_released_by_a_later_tick(broker, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(R, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock.now, sleep=time.sleep))
    sim = runner(broker, reorder_pct=50.0, enabled=["nordvik"])
    sent = sim.tick(now_ms=T0 + 1000, dt=1.0)
    held = len(sim.delayed)
    assert held > 0
    clock.now += 10.0
    sim.s.reorder_pct = 0.0
    total = sim.tick(now_ms=T0 + 2000, dt=1.0)
    assert sim.delayed == []
    assert total == (sent + held) + held, "the new tick plus what was held back"


def test_malformed_payloads_are_counted_and_unreadable(broker):
    sim = runner(broker, malformed_pct=50.0, enabled=["nordvik"])
    sim.tick(now_ms=T0 + 1000, dt=1.0)
    adapter = compile_spec("nordvik", 1, DIALECTS["nordvik"].spec)
    bad = 0
    for r in raw(broker):
        try:
            adapter.normalize(r.value)
        except NormalizeError:
            bad += 1
    assert bad == sim.malformed
    assert 0.3 < bad / sim.sent < 0.7


def test_outage_buffers_the_devices_that_are_offline(broker):
    sim = runner(broker, outage_pct=40)
    offline = {d for d in sim.fleet.device_id if canary_bucket(d.encode()) < 40}
    sent = sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert sent == 300 - len(offline)
    assert {dev for _, dev, _, _ in sim.held} == offline
    assert {r.key.decode() for r in raw(broker)} == set(sim.fleet.device_id) - offline

    sim.tick(now_ms=T0 + 2000, dt=1.0)
    assert len(sim.held) == 2 * len(offline)


def test_recovery_floods_everything_that_was_buffered(broker):
    sim = runner(broker, outage_pct=40)
    offline = {d for d in sim.fleet.device_id if canary_bucket(d.encode()) < 40}
    for k in range(3):
        sim.tick(now_ms=T0 + (k + 1) * 1000, dt=1.0)
    sim.s.apply({"outage_pct": 0})
    sent = sim.tick(now_ms=T0 + 4000, dt=1.0)
    assert sent == 300 + 3 * len(offline)
    assert sim.held == []
    per_device = Counter(r.key.decode() for r in raw(broker))
    assert set(per_device.values()) == {4}, "every vehicle delivered all four events"


def test_total_outage(broker):
    sim = runner(broker, outage_pct=100)
    assert sim.tick(now_ms=T0 + 1000, dt=1.0) == 0
    assert len(sim.held) == 300 and raw(broker) == []


def test_format_drift_moves_a_share_of_pacifica_to_the_new_firmware(broker):
    sim = runner(broker, drift_pct=30, enabled=["pacifica", "nordvik"])
    sim.tick(now_ms=T0 + 1000, dt=1.0)
    old = new = 0
    for r in raw(broker):
        msg = orjson.loads(r.value)
        if r.headers["oem"] != "pacifica":
            assert "fw" not in msg
            continue
        updated = canary_bucket(r.key) < 30
        assert ("speed" in msg and "fw" in msg) == updated
        assert ("speed_mph" in msg) == (not updated)
        new += updated
        old += not updated
    assert old > 0 and new > 0
    assert sim.by_oem["pacifica"] == old + new, "both firmwares are the same source"


def test_full_drift(broker):
    sim = runner(broker, drift_pct=100, enabled=["pacifica"])
    sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert all("speed_mph" not in orjson.loads(r.value) for r in raw(broker))


def test_burst_starts_parked_vehicles(tmp_path):
    driving = {}
    for burst in (1.0, 50.0):
        b = LogBroker(tmp_path / f"b{burst}", partitions=1)
        sim = runner(b, vehicles=2000, burst=burst, mode="realistic")
        sim.fleet.state[:] = 0
        sim.fleet.speed[:] = 0.0
        sim.tick(now_ms=T0 + 1000, dt=1.0)
        driving[burst] = int((sim.fleet.state == 1).sum())
    assert driving[50.0] > 5 * max(1, driving[1.0])


# --------------------------------------------------------------- back-pressure
class FlakyGateway:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.items: list = []

    def submit(self, oem, items, content_type=""):
        self.calls += 1
        if self.calls <= self.failures:
            raise Backpressure(0.3)
        self.items.extend(items)
        return len(items)


def test_runner_backs_off_and_retries_when_intake_is_paused(broker, monkeypatch):
    sleeps = []
    monkeypatch.setattr(R, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=sleeps.append))
    gw = FlakyGateway(failures=2)
    s = SimSettings(mode="all", enabled=["nordvik"], **CLEAN)
    sim = SimulatorRunner(fleet=Fleet(100, seed=7, start_ms=T0), gateway=gw, settings=s, broker=broker,
                          listen_control=False)
    sent = sim.tick(now_ms=T0 + 1000, dt=1.0)
    assert sent == len(gw.items) > 0
    assert sleeps == [0.3, 0.3] and sim.throttled == 2


def test_runner_gives_up_after_two_hundred_attempts(broker, monkeypatch):
    monkeypatch.setattr(R, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda _s: None))
    gw = FlakyGateway(failures=10 ** 9)
    s = SimSettings(mode="all", enabled=["nordvik"], **CLEAN)
    sim = SimulatorRunner(fleet=Fleet(100, seed=7, start_ms=T0), gateway=gw, settings=s, broker=broker,
                          listen_control=False)
    assert sim.tick(now_ms=T0 + 1000, dt=1.0) == 0
    assert gw.calls == 200 and sim.sent == 0 and sim.by_oem == {}


# -------------------------------------------------------------------- control
def test_runner_without_control_channel_ignores_control_messages(broker):
    sim = runner(broker, vehicles=50)
    send_control(broker, {"paused": True})
    sim.poll_control()
    assert sim.control is None and sim.s.paused is False


def test_control_messages_change_the_settings(broker):
    send_control(broker, {"hz": 5, "enabled": ["helix"]})
    sim = SimulatorRunner(fleet=Fleet(50, seed=7), settings=SimSettings(), broker=broker, listen_control=True)
    sim.poll_control()
    assert sim.s.hz == 5.0 and sim.s.enabled == ["helix"]

    send_control(broker, {"drift_pct": 25})
    send_control(broker, {"paused": True})
    sim.poll_control()
    assert (sim.s.drift_pct, sim.s.paused, sim.s.hz) == (25, True, 5.0)


def test_bad_control_messages_are_skipped(broker):
    from rosetta.ports.broker import Record

    broker.produce(T_CONTROL, [Record(key=b"sim", value=b"not json"),
                               Record(key=b"sim", value=orjson.dumps({"hz": "fast"})),
                               Record(key=b"sim", value=orjson.dumps([1, 2])),
                               Record(key=b"sim", value=orjson.dumps({"hz": 3}))])
    sim = SimulatorRunner(fleet=Fleet(50, seed=7), settings=SimSettings(), broker=broker, listen_control=True)
    sim.poll_control()
    assert sim.s.hz == 3.0


def test_send_control_writes_to_the_control_topic(broker):
    send_control(broker, {"burst": 9})
    rec = broker.consumer(T_CONTROL, "t", start="beginning").poll(10, 0.0)[0]
    assert rec.key == b"sim" and orjson.loads(rec.value) == {"burst": 9}


# -------------------------------------------------------------------- publish
def test_publish_reports_a_delta_and_resets_the_counters(broker):
    sim = runner(broker, dup_pct=10.0, malformed_pct=10.0, reorder_pct=10.0)
    sim.tick(now_ms=T0 + 1000, dt=1.0)
    sent, by_oem, dups, malformed, reordered = sim.sent, dict(sim.by_oem), sim.dups, sim.malformed, sim.reordered
    sim.publish()

    msg = orjson.loads(broker.consumer(T_METRICS, "t", start="beginning").poll(10, 0.0)[0].value)
    assert (msg["svc"], msg["id"]) == ("simulator", "s0")
    assert (msg["sent"], msg["by_oem"], msg["dups"], msg["malformed"], msg["reordered"]) == \
        (sent, by_oem, dups, malformed, reordered)
    assert msg["vehicles"] == 300 and msg["delayed"] == len(sim.delayed) and msg["held"] == 0
    assert msg["settings"]["mode"] == "all" and msg["settings"]["dup_pct"] == 10.0
    assert (sim.sent, sim.dups, sim.malformed, sim.reordered, sim.throttled, sim.by_oem) == (0, 0, 0, 0, 0, {})


def test_runner_precomputes_the_bucket_of_every_device(broker):
    sim = runner(broker, vehicles=100)
    assert sim.bucket.tolist() == [canary_bucket(d.encode()) for d in sim.fleet.device_id]


def test_mqtt_transport_publishes_one_topic_per_device():
    from rosetta.simulator.transports import MqttTransport

    class Client:
        def __init__(self, fail_at: int = -1) -> None:
            self.published, self.fail_at, self.calls = [], fail_at, []

        def publish(self, topic, payload, qos=0):
            if len(self.published) == self.fail_at:
                return SimpleNamespace(rc=1)
            self.published.append((topic, payload, qos))
            return SimpleNamespace(rc=0)

        def loop_stop(self):
            self.calls.append("loop_stop")

        def disconnect(self):
            self.calls.append("disconnect")

    client = Client()
    t = MqttTransport(client=client)
    assert t.submit("helix", [("hx-1", b"a"), ("hx-2", b"b")], "application/json") == 2
    assert client.published == [("oem/helix/hx-1/telemetry", b"a", 1), ("oem/helix/hx-2/telemetry", b"b", 1)]
    assert t.accepted == 2

    full = MqttTransport(client=Client(fail_at=1))
    with pytest.raises(Backpressure):
        full.submit("helix", [("hx-1", b"a"), ("hx-2", b"b"), ("hx-3", b"c")])
    assert full.throttled == 2 and full.accepted == 0
