"""rosetta.simulator.dialects: six ways to say the same thing, and their ground-truth mappings."""
from __future__ import annotations

import base64
import copy

import orjson
import pytest
from google.protobuf import descriptor_pb2

from rosetta.domain import errors as E
from rosetta.domain.canonical import REQUIRED, validate
from rosetta.domain.errors import NormalizeError
from rosetta.engine.compiler import compile_spec
from rosetta.engine.decoders import build_message_class
from rosetta.simulator import dialects as D
from rosetta.simulator.dialects import DIALECTS, TOLERANCE, expected_events

ALL = sorted(DIALECTS)
JSON_DIALECTS = ["nordvik", "pacifica", "pacifica_v2", "voltaic", "helix"]


def first(truth: dict[str, list], key: str) -> bytes:
    return DIALECTS[key].encode(truth)[0]


def one_vehicle(truth: dict[str, list], **overrides) -> dict[str, list]:
    t = {k: [v[0]] for k, v in truth.items()}
    for k, v in overrides.items():
        t[k] = [v]
    return t


# ------------------------------------------------------------------------ iso
@pytest.mark.parametrize("ts_ms,text", [
    (0, "1970-01-01T00:00:00.000Z"),
    (946684800000, "2000-01-01T00:00:00.000Z"),
    (1790000000123, "2026-09-21T14:13:20.123Z"),
    (1790000000007, "2026-09-21T14:13:20.007Z"),
])
def test_iso(ts_ms, text):
    assert D.iso(ts_ms) == text


def test_iso_round_trips_through_the_iso8601_transform():
    from rosetta.domain.transforms import compile_transform

    parse = compile_transform("iso8601")
    for ts in (1790000000000, 1790000000001, 1790000000999, 946684800000):
        assert parse(D.iso(ts)) == ts


def test_iso_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(D, "_ISO_CACHE", {})
    for ts in range(1790000000000, 1790000000000 + 5000):
        D.iso(ts)
    assert len(D._ISO_CACHE) <= 4097
    assert D.iso(1790000000000) == "2026-09-21T14:13:20.000Z"


# ---------------------------------------------------------------------- table
def test_the_seven_dialects():
    assert set(DIALECTS) == {"nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix", "pacifica_v2"}
    for key, d in DIALECTS.items():
        assert d.key == key
    assert {k for k, d in DIALECTS.items() if d.seeded} == {"nordvik", "pacifica", "stellaris", "kaizen", "voltaic"}
    assert DIALECTS["pacifica_v2"].oem == "pacifica", "the same brand after an over-the-air update"
    assert {d.oem for k, d in DIALECTS.items() if k != "pacifica_v2"} == set(DIALECTS) - {"pacifica_v2"}


def test_content_types():
    assert {k: d.content_type for k, d in DIALECTS.items()} == {
        "nordvik": "application/json", "pacifica": "application/json", "pacifica_v2": "application/json",
        "voltaic": "application/json", "helix": "application/json", "stellaris": "text/plain",
        "kaizen": "application/x-protobuf"}


@pytest.mark.parametrize("key", ALL)
def test_spec_compiles_and_maps_every_required_field(key):
    spec = DIALECTS[key].spec
    assert set(REQUIRED) <= set(spec["fields"])
    adapter = compile_spec(DIALECTS[key].oem, 1, spec)
    assert adapter.specialised is True
    assert spec["oem"] == DIALECTS[key].oem


@pytest.mark.parametrize("key", ALL)
def test_spec_is_plain_json_data(key):
    spec = DIALECTS[key].spec
    assert orjson.loads(orjson.dumps(spec)) == spec


def test_kaizen_descriptor_is_a_file_descriptor_set():
    fds = descriptor_pb2.FileDescriptorSet.FromString(base64.b64decode(D.KAIZEN_DESCRIPTOR_B64))
    assert [f.package for f in fds.file] == ["kaizen.v1"]
    assert [m.name for m in fds.file[0].message_type] == ["Position", "Telemetry"]
    assert len(D.KAIZEN_DESCRIPTOR_B64) < 200_000


# ------------------------------------------------------------------- encoders
@pytest.mark.parametrize("key", ALL)
def test_encoder_returns_one_payload_per_vehicle(key, truth):
    payloads = DIALECTS[key].encode(truth)
    assert len(payloads) == len(truth["vin"])
    assert all(type(p) is bytes and p for p in payloads)


@pytest.mark.parametrize("key", ALL)
def test_encoder_is_deterministic_and_leaves_the_truth_alone(key, truth):
    before = copy.deepcopy(truth)
    assert DIALECTS[key].encode(truth) == DIALECTS[key].encode(truth)
    assert truth == before


@pytest.mark.parametrize("key", ALL)
def test_encoder_with_no_vehicles(key, truth):
    assert DIALECTS[key].encode({k: [] for k in truth}) == []


def test_nordvik_is_nested_json_in_metric_units(truth):
    msg = orjson.loads(first(truth, "nordvik"))
    assert msg["vehicle"] == {"vin": truth["vin"][0], "deviceId": truth["device_id"][0]}
    assert msg["recordedAt"] == D.iso(truth["ts"][0])
    assert msg["motion"] == {"speedKmh": truth["speed_kmh"][0], "odometerKm": truth["odo_km"][0]}
    assert msg["position"]["latitude"] == truth["lat"][0]
    assert msg["diagnostics"]["troubleCodes"] == truth["dtc"][0]


def test_pacifica_uses_imperial_units(truth):
    t = one_vehicle(truth, speed_kmh=64.37, odo_km=160.9344, ambient_c=37.0, ignition=True, dtc=["P0301", "U0100"],
                    evt="HARSH_BRAKE", soc_pct=None, fuel_pct=55.5)
    msg = orjson.loads(D.encode_pacifica(t)[0])
    assert msg["speed_mph"] == pytest.approx(40.0, abs=0.01)
    assert msg["odometer_mi"] == pytest.approx(100.0)
    assert msg["oat_f"] == pytest.approx(98.6)
    assert msg["ts"] == t["ts"][0] / 1000.0
    assert (msg["ign"], msg["dtcs"], msg["evt_code"]) == ("ON", "P0301,U0100", "HB")
    assert "batt_pct" not in msg and msg["fuel_pct"] == 55.5


def test_pacifica_v2_renames_two_fields_and_goes_metric(truth):
    old = orjson.loads(first(truth, "pacifica"))
    new = orjson.loads(first(truth, "pacifica_v2"))
    assert set(new) - set(old) == {"speed", "odometer", "fw"}
    assert set(old) - set(new) == {"speed_mph", "odometer_mi"}
    assert new["speed"] == truth["speed_kmh"][0] and new["odometer"] == truth["odo_km"][0]
    assert {k: v for k, v in new.items() if k in old} == {k: v for k, v in old.items() if k in new}


def test_stellaris_is_pipe_delimited_with_sixteen_columns(truth):
    t = one_vehicle(truth, lat=12.971600, lon=-77.594600, speed_kmh=36.0, odo_km=18234.5, soc_pct=76.5,
                    fuel_pct=None, ambient_c=-3.5, ignition=False, dtc=["P0301", "U0100"], evt=None)
    cols = D.encode_stellaris(t)[0].decode().split("|")
    assert len(cols) == 16 == len(D._STL_COLS)
    assert cols[:3] == ["STL", "2", t["vin"][0]]
    assert cols[5:7] == ["12971600", "-77594600"]
    assert cols[8:10] == ["10.000", "18234500"]
    assert cols[10:] == ["765", "", "-35", "0", "P0301 U0100", "0"]


def test_kaizen_is_protobuf_with_scaled_integers(truth):
    t = one_vehicle(truth, lat=12.9716, lon=77.5946, heading_deg=181.5, speed_kmh=54.23, odo_km=18234.5,
                    soc_pct=None, fuel_pct=40.0, ambient_c=27.5, evt="IDLE")
    cls = build_message_class(D.KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Telemetry")
    msg = cls.FromString(D.encode_kaizen(t)[0])
    assert msg.vin == t["vin"][0] and msg.time_us == t["ts"][0] * 1000 and msg.counter == t["seq"][0]
    assert (msg.pos.lat_e7, msg.pos.lon_e7, msg.pos.heading_cdeg) == (129_716_000, 775_946_000, 18_150)
    assert (msg.speed_centi_kmh, msg.odometer_hm, msg.ambient_deci_c) == (5423, 182_345, 275)
    assert not msg.HasField("soc_permille") and msg.fuel_permille == 400
    assert msg.event == list(D.EVENT_TYPES).index("IDLE") + 1


def test_voltaic_sends_a_signal_list_and_never_fuel(truth):
    t = one_vehicle(truth, speed_kmh=36.0, ambient_c=26.85, soc_pct=76.5, fuel_pct=50.0, evt="LOW_SOC", ignition=True)
    msg = orjson.loads(D.encode_voltaic(t)[0])
    assert msg["meta"] == {"vin": t["vin"][0], "sent": t["ts"][0], "ctr": t["seq"][0], "schema": "vt.3"}
    sig = {s["k"]: s["v"] for s in msg["signals"]}
    assert len(sig) == len(msg["signals"]), "signal names are unique"
    assert sig["veh_speed"] == 10.0 and sig["env_temp"] == 300.0 and sig["bat_level"] == 0.765
    assert sig["pwr_state"] == "RUN" and sig["drv_event"] == "low_soc"
    assert not any("fuel" in k for k in sig)


def test_helix_speaks_german(truth):
    t = one_vehicle(truth, speed_kmh=36.0, odo_km=18234.5, soc_pct=76.5, fuel_pct=None, evt="HARSH_BRAKE",
                    ignition=True, dtc=["P0301", "U0100"])
    msg = orjson.loads(D.encode_helix(t)[0])
    assert msg["kopf"]["fin"] == t["vin"][0] and msg["kopf"]["lfd_nr"] == t["seq"][0]
    assert msg["fahrt"] == {"v": 10.0, "strecke": 18_234_500}
    assert msg["akku"] == {"ladung": 0.765} and "tank" not in msg
    assert (msg["zuendung"], msg["fehler"], msg["ereignis"]) == (1, "P0301;U0100", "VOLLBREMSUNG")


@pytest.mark.parametrize("key", ["pacifica", "pacifica_v2", "voltaic", "helix"])
def test_optional_values_are_left_out_not_sent_as_null(key, truth):
    t = one_vehicle(truth, soc_pct=None, fuel_pct=None, evt=None)
    assert b"null" not in DIALECTS[key].encode(t)[0]


# ------------------------------------------------------------- golden round trip
def matches(name: str, got, want) -> bool:
    if want is None or want == []:
        return got is None or got == []
    if got is None:
        return False
    if name in TOLERANCE:
        return abs(got - want) <= TOLERANCE[name]
    return got == want


def with_events_and_faults(truth: dict[str, list]) -> dict[str, list]:
    """Give every fifth vehicle a driving event and every seventh a trouble code."""
    for j in range(0, len(truth["vin"]), 5):
        truth["evt"][j] = D.EVENT_TYPES[(j // 5) % len(D.EVENT_TYPES)]
    for j in range(0, len(truth["vin"]), 7):
        truth["dtc"][j] = ["P0301", "U0100"][: 1 + j % 2]
    return truth


@pytest.mark.parametrize("key", ALL)
def test_golden_round_trip(key, truth):
    truth = with_events_and_faults(truth)
    d = DIALECTS[key]
    adapter = compile_spec(d.oem, 1, d.spec)
    expected = expected_events(truth, d.oem)
    payloads = d.encode(truth)
    assert len(expected) == len(payloads) == len(truth["vin"]) >= 200
    checked = set()
    for payload, want in zip(payloads, expected):
        got = adapter.normalize(payload)
        assert validate(got) is None
        for name, value in want.items():
            assert matches(name, got.get(name), value), (key, name, got.get(name), value)
            checked.add(name)
        assert set(got) - set(want) <= {"oem", "map_v", "dtc"}, "the mapping invents nothing"
    assert set(REQUIRED) | {"heading_deg", "ambient_c", "ignition", "dtc", "soc_pct", "evt"} <= checked


@pytest.mark.parametrize("key", ALL)
def test_golden_round_trip_for_each_event_type(key, truth):
    d = DIALECTS[key]
    adapter = compile_spec(d.oem, 1, d.spec)
    for evt in D.EVENT_TYPES:
        t = one_vehicle(truth, evt=evt)
        assert adapter.normalize(d.encode(t)[0])["evt"] == evt


@pytest.mark.parametrize("key", ALL)
def test_golden_round_trip_at_the_edges_of_the_ranges(key, truth):
    d = DIALECTS[key]
    adapter = compile_spec(d.oem, 1, d.spec)
    for lat, lon, speed, temp, heading in [(-89.9, -179.999999, 0.0, -40.0, 0.0), (89.9, 179.999999, 165.0, 55.0, 359.9),
                                           (0.0, 0.0, 0.01, 0.0, 180.0), (-0.000001, 0.000001, 99.99, -0.1, 0.1)]:
        t = one_vehicle(truth, lat=lat, lon=lon, speed_kmh=speed, ambient_c=temp, heading_deg=heading,
                        soc_pct=100.0, fuel_pct=0.0)
        got = adapter.normalize(d.encode(t)[0])
        want = expected_events(t, d.oem)[0]
        for name, value in want.items():
            assert matches(name, got.get(name), value), (name, got.get(name), value)


def test_tolerances_cover_every_measured_field():
    assert set(TOLERANCE) == {"lat", "lon", "heading_deg", "speed_kmh", "odo_km", "soc_pct", "fuel_pct", "ambient_c"}
    assert all(0 < v < 0.1 for v in TOLERANCE.values())


# ------------------------------------------------------------ expected_events
def test_expected_events_are_canonical_and_complete(truth):
    events = expected_events(truth, "nordvik")
    assert len(events) == len(truth["vin"])
    for j, ev in enumerate(events):
        assert validate(ev) is None
        assert (ev["vin"], ev["ts"], ev["seq"]) == (truth["vin"][j], truth["ts"][j], truth["seq"][j])
        for name in ("soc_pct", "fuel_pct", "evt"):
            assert (name in ev) == (truth[name][j] is not None)


def test_expected_events_for_voltaic_have_no_fuel(truth):
    assert any(v is not None for v in truth["fuel_pct"])
    assert all("fuel_pct" not in ev for ev in expected_events(truth, "voltaic"))
    assert any("fuel_pct" in ev for ev in expected_events(truth, "helix"))


def test_expected_events_do_not_share_lists_with_the_truth(truth):
    events = expected_events(truth, "nordvik")
    events[0]["dtc"].append("P0000")
    assert "P0000" not in truth["dtc"][0]


# ----------------------------------------------------------------- format drift
def failure(adapter, payload: bytes) -> NormalizeError:
    with pytest.raises(NormalizeError) as info:
        adapter.normalize(payload)
    return info.value


def test_firmware_update_breaks_the_old_mapping_with_a_schema_mismatch(truth):
    old = compile_spec("pacifica", 1, D.SPEC_PACIFICA)
    err = failure(old, first(truth, "pacifica_v2"))
    assert (err.reason, err.field) == (E.SCHEMA_MISMATCH, "speed_kmh")
    assert "speed_mph" in err.detail


def test_old_firmware_does_not_match_the_new_mapping(truth):
    new = compile_spec("pacifica", 2, D.SPEC_PACIFICA_V2)
    err = failure(new, first(truth, "pacifica"))
    assert (err.reason, err.field) == (E.SCHEMA_MISMATCH, "speed_kmh")


def test_v2_spec_differs_from_v1_only_in_the_two_renamed_fields():
    v1, v2 = D.SPEC_PACIFICA["fields"], D.SPEC_PACIFICA_V2["fields"]
    assert {k for k in v1 if v1[k] != v2[k]} == {"speed_kmh", "odo_km"}
    assert v2["speed_kmh"] == {"path": "speed"} and v2["odo_km"] == {"path": "odometer"}


@pytest.mark.parametrize("spec_key", [k for k in ALL if k != "pacifica_v2"])
def test_a_mapping_never_accepts_another_makers_payload(spec_key, truth):
    adapter = compile_spec(DIALECTS[spec_key].oem, 1, DIALECTS[spec_key].spec)
    for other in ALL:
        if DIALECTS[other].oem == DIALECTS[spec_key].oem:
            continue
        err = failure(adapter, first(truth, other))
        assert err.reason in (E.DECODE_ERROR, E.SCHEMA_MISMATCH, E.INVALID), (spec_key, other)
