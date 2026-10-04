"""rosetta.engine.normalizer: route, decode, map, validate and de-duplicate a batch."""
from __future__ import annotations

import copy
from typing import Any

import orjson
import pytest

from rosetta.domain import errors as E
from rosetta.engine import normalizer as N
from rosetta.engine.decoders import MAX_PAYLOAD_BYTES
from rosetta.engine.normalizer import BatchStats, Normalizer
from rosetta.engine.router import ST_ACTIVE, ST_CANARY, MappingRow, RoutingTable, canary_bucket
from rosetta.ports.broker import Record
from rosetta.simulator.dialects import DIALECTS, SPEC_NORDVIK, SPEC_PACIFICA, SPEC_PACIFICA_V2

NOW = 1_790_000_100_000
RX = NOW - 40


def records(truth: dict[str, list], dialect: str, oem: str | None = None, count: int = 20, start: int = 0,
            **headers: Any) -> list[Record]:
    d = DIALECTS[dialect]
    payloads = d.encode(truth)[start:start + count]
    devices = truth["device_id"][start:start + count]
    h = {"oem": oem or d.oem, "rx": RX, **headers}
    return [Record(key=dev.encode(), value=p, headers=h) for dev, p in zip(devices, payloads)]


def table(*rows: MappingRow) -> RoutingTable:
    t = RoutingTable(rows)
    assert t.errors == []
    return t


def nordvik_only() -> RoutingTable:
    return table(MappingRow("nordvik", 1, ST_ACTIVE, SPEC_NORDVIK))


def events(result) -> list[dict[str, Any]]:
    return [orjson.loads(r.value) for r in result.canonical]


def wrong_unit_spec() -> dict[str, Any]:
    """Pacifica v1 with the odometer scaled a million times too high: every event is out of range."""
    spec = copy.deepcopy(SPEC_PACIFICA)
    spec["fields"]["odo_km"]["transforms"] = [{"op": "scale", "factor": 1e6}]
    return spec


def broken_transform_spec() -> dict[str, Any]:
    """Pacifica v1 that reads the ignition text as the odometer: the conversion fails."""
    spec = copy.deepcopy(SPEC_PACIFICA)
    spec["fields"]["odo_km"] = {"path": "ign", "transforms": ["to_float"]}
    return spec


# ------------------------------------------------------------------ happy path
def test_batch_of_valid_records(truth):
    result = Normalizer(nordvik_only()).process(records(truth, "nordvik"), now_ms=NOW)
    assert len(result.canonical) == 20 and result.dead == []
    s = result.stats
    assert s.received == 20
    assert s.bytes_in == sum(len(r.value) for r in records(truth, "nordvik"))
    assert dict(s.ok) == {("nordvik", 1): 20}
    assert not s.failed and not s.duplicates and s.fallbacks == 0


def test_canonical_record_shape(truth):
    result = Normalizer(nordvik_only()).process(records(truth, "nordvik", count=1), now_ms=NOW)
    rec = result.canonical[0]
    ev = orjson.loads(rec.value)
    assert rec.key == truth["vin"][0].encode(), "keyed by VIN so a vehicle keeps its order downstream"
    assert rec.headers == {"oem": "nordvik", "v": 1}
    assert ev["vin"] == truth["vin"][0] and ev["seq"] == truth["seq"][0]
    assert (ev["oem"], ev["map_v"], ev["rx_ts"], ev["norm_ts"]) == ("nordvik", 1, RX, NOW)
    assert "replayed" not in ev


def test_latency_is_measured_from_the_receive_time(truth):
    result = Normalizer(nordvik_only()).process(records(truth, "nordvik", count=5), now_ms=NOW)
    assert result.stats.latency_ms == [NOW - RX] * 5


def test_record_without_receive_time_has_zero_latency(truth):
    recs = [Record(key=r.key, value=r.value, headers={"oem": "nordvik"}) for r in records(truth, "nordvik", count=3)]
    result = Normalizer(nordvik_only()).process(recs, now_ms=NOW)
    assert result.stats.latency_ms == [0, 0, 0]
    assert all(ev["rx_ts"] == NOW for ev in events(result))


def test_clock_is_used_when_no_time_is_given(truth):
    result = Normalizer(nordvik_only()).process(records(truth, "nordvik", count=1))
    assert events(result)[0]["norm_ts"] > NOW - 10 ** 12


def test_empty_batch():
    result = Normalizer(nordvik_only()).process([], now_ms=NOW)
    assert result.canonical == [] and result.dead == [] and result.stats.received == 0


def test_input_records_are_not_modified(truth):
    recs = records(truth, "nordvik", count=3)
    before = [(r.key, r.value, dict(r.headers)) for r in recs]
    Normalizer(nordvik_only()).process(recs + records(truth, "helix", count=3), now_ms=NOW)
    assert [(r.key, r.value, r.headers) for r in recs] == before


# ------------------------------------------------------------------ NO_ADAPTER
def test_unknown_source_is_dead_lettered_with_no_adapter(truth):
    result = Normalizer(nordvik_only()).process(records(truth, "helix", count=4), now_ms=NOW)
    assert result.canonical == [] and len(result.dead) == 4
    assert dict(result.stats.failed) == {("helix", E.NO_ADAPTER): 4}
    assert not result.stats.failed_field, "there is no field to blame"
    h = result.dead[0].headers
    assert (h["reason"], h["oem"], h["field"], h["tried_v"]) == (E.NO_ADAPTER, "helix", "", 0)


def test_record_without_a_source_header_is_unknown(truth):
    rec = Record(key=b"dev-1", value=records(truth, "nordvik", count=1)[0].value, headers={})
    result = Normalizer(nordvik_only()).process([rec], now_ms=NOW)
    assert dict(result.stats.failed) == {("unknown", E.NO_ADAPTER): 1}
    assert result.dead[0].key == b"unknown"


def test_normalizer_without_a_table_knows_no_source(truth):
    result = Normalizer().process(records(truth, "nordvik", count=2), now_ms=NOW)
    assert dict(result.stats.failed) == {("nordvik", E.NO_ADAPTER): 2}


def test_source_whose_only_version_is_broken_has_no_adapter(truth):
    t = RoutingTable([MappingRow("nordvik", 1, ST_ACTIVE, {"decoder": {"type": "json"}, "fields": {}})])
    result = Normalizer(t).process(records(truth, "nordvik", count=2), now_ms=NOW)
    assert dict(result.stats.failed) == {("nordvik", E.NO_ADAPTER): 2}


# -------------------------------------------------------------------- OVERSIZE
def test_oversize_payload(truth):
    big = Record(key=b"no-0000001", value=b"x" * (MAX_PAYLOAD_BYTES + 1), headers={"oem": "nordvik", "rx": RX})
    result = Normalizer(nordvik_only()).process([big], now_ms=NOW)
    assert dict(result.stats.failed) == {("nordvik", E.OVERSIZE): 1}
    h = result.dead[0].headers
    assert (h["reason"], h["detail"], h["tried_v"]) == (E.OVERSIZE, f"{MAX_PAYLOAD_BYTES + 1} bytes", 0)
    assert result.stats.bytes_in == MAX_PAYLOAD_BYTES + 1


def test_oversize_is_checked_before_the_source_is_looked_up():
    big = Record(key=b"x", value=b"x" * (MAX_PAYLOAD_BYTES + 1), headers={"oem": "nobody"})
    result = Normalizer(nordvik_only()).process([big], now_ms=NOW)
    assert dict(result.stats.failed) == {("nobody", E.OVERSIZE): 1}


def test_payload_at_the_limit_is_not_oversize():
    rec = Record(key=b"x", value=b"x" * MAX_PAYLOAD_BYTES, headers={"oem": "nordvik"})
    result = Normalizer(nordvik_only()).process([rec], now_ms=NOW)
    assert dict(result.stats.failed) == {("nordvik", E.DECODE_ERROR): 1}


# ---------------------------------------------------------------- dead letters
def test_dead_letter_keeps_the_payload_and_explains_the_failure(truth):
    msg = orjson.loads(records(truth, "nordvik", count=1)[0].value)
    del msg["motion"]["odometerKm"]
    raw = orjson.dumps(msg)
    rec = Record(key=b"no-0000007", value=raw, headers={"oem": "nordvik", "rx": RX, "ct": "application/json"})
    result = Normalizer(nordvik_only()).process([rec], now_ms=NOW)
    dead = result.dead[0]
    assert dead.value == raw
    assert dead.key == b"nordvik", "dead letters are grouped by source"
    assert dead.headers == {
        "oem": "nordvik", "rx": RX, "ct": "application/json", "reason": E.SCHEMA_MISMATCH, "field": "odo_km",
        "detail": "source field 'motion.odometerKm' is missing", "tried_v": 1, "dead_ts": NOW,
        "dev": "no-0000007", "attempts": 1,
    }
    assert dict(result.stats.failed) == {("nordvik", E.SCHEMA_MISMATCH): 1}
    assert dict(result.stats.failed_field) == {("nordvik", E.SCHEMA_MISMATCH, "odo_km"): 1}


def test_attempts_are_counted_across_replays(truth):
    rec = Record(key=b"d", value=b"{}", headers={"oem": "nordvik", "attempts": 2, "replay": True})
    result = Normalizer(nordvik_only()).process([rec], now_ms=NOW)
    assert result.dead[0].headers["attempts"] == 3
    assert result.dead[0].headers["replay"] is True


def test_dead_letter_detail_is_bounded():
    spec = copy.deepcopy(SPEC_NORDVIK)
    spec["fields"]["vin"] = {"path": "k" * 190}
    t = table(MappingRow("nordvik", 1, ST_ACTIVE, spec))
    rec = Record(key=b"d", value=b'{"vehicle": {"vin": "x"}}', headers={"oem": "nordvik"})
    detail = Normalizer(t).process([rec], now_ms=NOW).dead[0].headers["detail"]
    assert len(detail) == 160
    assert detail.startswith("source field 'kkk")


def test_device_key_that_is_not_utf8_is_still_reported():
    rec = Record(key=b"\xff\xfedev", value=b"{}", headers={"oem": "nordvik"})
    result = Normalizer(nordvik_only()).process([rec], now_ms=NOW)
    assert result.dead[0].headers["dev"].endswith("dev")


@pytest.mark.parametrize("payload,reason", [(b"not json", E.DECODE_ERROR), (b"{}", E.SCHEMA_MISMATCH),
                                            (b"null", E.SCHEMA_MISMATCH)])
def test_single_version_failure_is_dead_lettered_with_its_reason(payload, reason):
    rec = Record(key=b"d", value=payload, headers={"oem": "nordvik"})
    result = Normalizer(nordvik_only()).process([rec], now_ms=NOW)
    assert dict(result.stats.failed) == {("nordvik", reason): 1}
    assert result.dead[0].headers["tried_v"] == 1
    assert not result.stats.canary_failed and result.stats.fallbacks == 0


def test_good_and_bad_records_in_one_batch(truth):
    recs = records(truth, "nordvik", count=6)
    recs.insert(2, Record(key=b"d", value=b"garbage", headers={"oem": "nordvik"}))
    recs.insert(5, Record(key=b"d", value=b"{}", headers={"oem": "mystery"}))
    result = Normalizer(nordvik_only()).process(recs, now_ms=NOW)
    assert len(result.canonical) == 6 and len(result.dead) == 2
    assert result.stats.received == 8
    assert [ev["vin"] for ev in events(result)] == truth["vin"][:6], "order is preserved"


# ------------------------------------------------------------------ duplicates
def test_duplicates_are_dropped(truth):
    recs = records(truth, "nordvik", count=5)
    result = Normalizer(nordvik_only()).process(recs + recs[:2], now_ms=NOW)
    assert len(result.canonical) == 5 and result.dead == []
    assert dict(result.stats.duplicates) == {"nordvik": 2}
    assert dict(result.stats.ok) == {("nordvik", 1): 5}
    assert len(result.stats.latency_ms) == 5


def test_duplicates_are_remembered_across_batches(truth):
    n = Normalizer(nordvik_only())
    recs = records(truth, "nordvik", count=5)
    n.process(recs, now_ms=NOW)
    again = n.process(recs, now_ms=NOW + 1000)
    assert again.canonical == [] and dict(again.stats.duplicates) == {"nordvik": 5}


def test_same_event_in_two_dialects_is_one_event(truth):
    t = table(MappingRow("nordvik", 1, ST_ACTIVE, SPEC_NORDVIK), MappingRow("helix", 1, ST_ACTIVE, DIALECTS["helix"].spec))
    result = Normalizer(t).process(records(truth, "nordvik", count=3) + records(truth, "helix", count=3), now_ms=NOW)
    assert len(result.canonical) == 3
    assert dict(result.stats.duplicates) == {"helix": 3}, "de-duplication is by VIN and sequence, not by source"


def test_a_dead_letter_does_not_consume_the_sequence_number(truth):
    n = Normalizer(nordvik_only())
    good = records(truth, "nordvik", count=1)[0]
    broken = Record(key=good.key, value=good.value[:-5], headers=good.headers)
    assert len(n.process([broken], now_ms=NOW).dead) == 1
    assert len(n.process([good], now_ms=NOW).canonical) == 1


# ---------------------------------------------------------------------- replay
def test_replayed_messages_are_flagged_and_kept_out_of_the_latency_histogram(truth):
    hours = 3 * 3600 * 1000
    live = records(truth, "nordvik", count=4)
    replayed = [Record(key=r.key, value=r.value, headers={"oem": "nordvik", "rx": NOW - hours, "replay": True})
                for r in records(truth, "nordvik", count=3, start=4)]
    result = Normalizer(nordvik_only()).process(live + replayed, now_ms=NOW)
    evs = events(result)
    assert len(evs) == 7
    assert [ev.get("replayed", False) for ev in evs] == [False] * 4 + [True] * 3
    assert result.stats.replayed_ok == 3
    assert result.stats.latency_ms == [NOW - RX] * 4
    assert dict(result.stats.ok) == {("nordvik", 1): 7}
    assert evs[-1]["rx_ts"] == NOW - hours, "the original receive time is kept"


def test_replayed_duplicate_is_still_a_duplicate(truth):
    n = Normalizer(nordvik_only())
    recs = records(truth, "nordvik", count=2)
    n.process(recs, now_ms=NOW)
    replayed = [Record(key=r.key, value=r.value, headers={**r.headers, "replay": True}) for r in recs]
    result = n.process(replayed, now_ms=NOW)
    assert result.canonical == [] and result.stats.replayed_ok == 0
    assert dict(result.stats.duplicates) == {"nordvik": 2}


# -------------------------------------------------------- fallback, stickiness
def two_live_versions() -> RoutingTable:
    return table(MappingRow("pacifica", 1, ST_ACTIVE, SPEC_PACIFICA),
                 MappingRow("pacifica", 2, ST_ACTIVE, SPEC_PACIFICA_V2))


def test_mixed_format_traffic_is_handled_by_two_live_versions(truth):
    old = records(truth, "pacifica", count=10)
    new = records(truth, "pacifica_v2", count=10, start=10)
    mixed = [r for pair in zip(old, new) for r in pair]
    result = Normalizer(two_live_versions()).process(mixed, now_ms=NOW)
    assert result.dead == [] and len(result.canonical) == 20
    assert dict(result.stats.ok) == {("pacifica", 1): 10, ("pacifica", 2): 10}
    assert result.stats.fallbacks == 10, "version 2 is tried first, the old format falls back to version 1"
    assert not result.stats.failed and not result.stats.canary_failed
    by_vin = {ev["vin"]: ev for ev in events(result)}
    for j in range(20):
        ev = by_vin[truth["vin"][j]]
        assert ev["map_v"] == (1 if j < 10 else 2)
        assert ev["speed_kmh"] == pytest.approx(truth["speed_kmh"][j], abs=0.02)
        assert ev["odo_km"] == pytest.approx(truth["odo_km"][j], abs=0.06)


def test_the_version_that_worked_is_tried_first_next_time(truth):
    n = Normalizer(two_live_versions())
    first = n.process(records(truth, "pacifica", count=10), now_ms=NOW)
    assert first.stats.fallbacks == 10

    later = copy.deepcopy(truth)
    later["seq"] = [s + 1 for s in later["seq"]]
    second = n.process(records(later, "pacifica", count=10), now_ms=NOW + 1000)
    assert second.stats.fallbacks == 0
    assert dict(second.stats.ok) == {("pacifica", 1): 10}


def test_device_that_updates_its_firmware_moves_to_the_new_version(truth):
    n = Normalizer(two_live_versions())
    n.process(records(truth, "pacifica", count=5), now_ms=NOW)
    later = copy.deepcopy(truth)
    later["seq"] = [s + 1 for s in later["seq"]]
    after_ota = n.process(records(later, "pacifica_v2", count=5), now_ms=NOW + 1000)
    assert dict(after_ota.stats.ok) == {("pacifica", 2): 5}
    assert after_ota.stats.fallbacks == 5, "the sticky version 1 fails once, then version 2 sticks"
    later["seq"] = [s + 1 for s in later["seq"]]
    assert n.process(records(later, "pacifica_v2", count=5), now_ms=NOW + 2000).stats.fallbacks == 0


def test_message_no_version_understands_reports_the_first_attempt(truth):
    result = Normalizer(two_live_versions()).process(
        [Record(key=b"pa-1", value=b'{"VIN": "x"}', headers={"oem": "pacifica"})], now_ms=NOW)
    h = result.dead[0].headers
    assert (h["reason"], h["tried_v"]) == (E.SCHEMA_MISMATCH, 2)
    assert dict(result.stats.failed) == {("pacifica", E.SCHEMA_MISMATCH): 1}
    assert result.stats.fallbacks == 0


def test_invalid_event_stops_the_search(truth):
    msg = orjson.loads(records(truth, "pacifica_v2", count=1)[0].value)
    msg["lat"] = 123.0
    calls = []
    t = two_live_versions()
    routes = t.for_oem("pacifica")
    for adapter in routes.active:
        inner = adapter.normalize

        def spy(payload, _inner=inner, _v=adapter.version):
            calls.append(_v)
            return _inner(payload)

        adapter.normalize = spy
    result = Normalizer(t).process([Record(key=b"pa-1", value=orjson.dumps(msg), headers={"oem": "pacifica"})],
                                   now_ms=NOW)
    assert calls == [2], "an invalid value would be invalid under every mapping"
    assert dict(result.stats.failed_field) == {("pacifica", E.INVALID, "lat"): 1}


def test_swap_table_takes_effect_and_forgets_sticky_versions(truth):
    n = Normalizer(table(MappingRow("pacifica", 1, ST_ACTIVE, SPEC_PACIFICA)))
    assert len(n.process(records(truth, "pacifica_v2", count=5), now_ms=NOW).dead) == 5

    n.swap_table(two_live_versions())
    result = n.process(records(truth, "pacifica_v2", count=5), now_ms=NOW)
    assert dict(result.stats.ok) == {("pacifica", 2): 5}
    assert n._sticky == {r.key: 2 for r in records(truth, "pacifica_v2", count=5)}

    n.swap_table(nordvik_only())
    assert n._sticky == {}
    assert dict(n.process(records(truth, "pacifica", count=1), now_ms=NOW).stats.failed) == \
        {("pacifica", E.NO_ADAPTER): 1}


def test_sticky_table_is_bounded(truth, monkeypatch):
    monkeypatch.setattr(N, "MAX_STICKY", 4)
    n = Normalizer(two_live_versions())
    n.process(records(truth, "pacifica", count=10), now_ms=NOW)
    assert len(n._sticky) <= 4


# ---------------------------------------------------------------------- canary
def canary_table(canary_spec: dict[str, Any], pct: int = 100) -> RoutingTable:
    return table(MappingRow("pacifica", 1, ST_ACTIVE, SPEC_PACIFICA),
                 MappingRow("pacifica", 2, ST_CANARY, canary_spec, canary_pct=pct))


def test_canary_handles_the_devices_in_its_group(truth):
    recs = records(truth, "pacifica_v2", count=20)
    result = Normalizer(canary_table(SPEC_PACIFICA_V2, pct=40)).process(recs, now_ms=NOW)
    assert dict(result.stats.ok) == {("pacifica", 2): 20}
    in_group = sum(canary_bucket(r.key) < 40 for r in recs)
    assert 0 < in_group < 20
    assert result.stats.fallbacks == 20 - in_group, "the others reach the canary as the last resort"


def test_canary_failure_with_transform_error_is_counted_and_falls_back(truth):
    result = Normalizer(canary_table(broken_transform_spec())).process(records(truth, "pacifica", count=8), now_ms=NOW)
    assert dict(result.stats.canary_failed) == {("pacifica", 2): 8}
    assert dict(result.stats.ok) == {("pacifica", 1): 8}
    assert result.stats.fallbacks == 8 and result.dead == []


def test_canary_failure_with_invalid_is_counted(truth):
    result = Normalizer(canary_table(wrong_unit_spec())).process(records(truth, "pacifica", count=8), now_ms=NOW)
    assert dict(result.stats.canary_failed) == {("pacifica", 2): 8}
    assert dict(result.stats.failed) == {("pacifica", E.INVALID): 8}
    assert {d.headers["tried_v"] for d in result.dead} == {2}


def test_failure_of_an_active_version_is_not_a_canary_failure(truth):
    result = Normalizer(canary_table(SPEC_PACIFICA_V2, pct=0)).process(
        [Record(key=b"pa-1", value=b"{}", headers={"oem": "pacifica"})], now_ms=NOW)
    assert result.dead[0].headers["tried_v"] == 1
    assert dict(result.stats.failed) == {("pacifica", E.SCHEMA_MISMATCH): 1}


def test_old_format_traffic_does_not_count_against_the_canary(truth):
    # The canary speaks the new format. Vehicles that have not updated yet fail it with
    # SCHEMA_MISMATCH and are served by the active version: that says nothing about the canary.
    result = Normalizer(canary_table(SPEC_PACIFICA_V2)).process(records(truth, "pacifica", count=8), now_ms=NOW)
    assert dict(result.stats.ok) == {("pacifica", 1): 8}
    assert dict(result.stats.canary_failed) == {}


def test_undecodable_bytes_do_not_count_against_the_canary():
    result = Normalizer(canary_table(SPEC_PACIFICA_V2)).process(
        [Record(key=b"pa-1", value=b"\x00garbage", headers={"oem": "pacifica"})], now_ms=NOW)
    assert dict(result.stats.failed) == {("pacifica", E.DECODE_ERROR): 1}
    assert dict(result.stats.canary_failed) == {}


def test_canary_fault_reasons_are_declared():
    assert N.CANARY_FAULTS == frozenset({E.INVALID, E.TRANSFORM_ERROR})
    assert N.FALLBACK_REASONS == frozenset({E.SCHEMA_MISMATCH, E.DECODE_ERROR, E.TRANSFORM_ERROR})


# ----------------------------------------------------------------------- stats
def test_stats_to_dict_is_json_serialisable(truth):
    recs = records(truth, "pacifica", count=4) + records(truth, "pacifica", count=1) + \
        [Record(key=b"d", value=b"{}", headers={"oem": "pacifica"}),
         Record(key=b"d", value=b"{}", headers={"oem": "ghost"})]
    result = Normalizer(canary_table(SPEC_PACIFICA_V2, pct=0)).process(recs, now_ms=NOW)
    d = result.stats.to_dict()
    assert orjson.loads(orjson.dumps(d)) == d
    assert d["received"] == 7 and d["bytes_in"] == sum(len(r.value) for r in recs)
    assert d["ok"] == [["pacifica", 1, 4]]
    assert sorted(d["failed"]) == [["ghost", E.NO_ADAPTER, 1], ["pacifica", E.SCHEMA_MISMATCH, 1]]
    assert d["failed_field"] == [["pacifica", E.SCHEMA_MISMATCH, "vin", 1]]
    assert d["duplicates"] == {"pacifica": 1}
    assert d["replayed_ok"] == 0
    assert "latency_ms" not in d, "latency travels as histogram buckets, not as raw samples"


def test_fresh_stats_are_empty():
    assert BatchStats().to_dict() == {
        "received": 0, "bytes_in": 0, "ok": [], "failed": [], "failed_field": [], "canary_failed": [],
        "duplicates": {}, "fallbacks": 0, "replayed_ok": 0,
    }


def test_stats_objects_do_not_share_state():
    a, b = BatchStats(), BatchStats()
    a.ok[("x", 1)] += 1
    a.latency_ms.append(1.0)
    assert not b.ok and b.latency_ms == []
