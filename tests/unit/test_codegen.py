"""rosetta.engine.codegen: the specialised normalize function must equal the interpreter.

The differential tests feed the same bytes to both and compare the event, or
the reason, field and detail of the error.
"""
from __future__ import annotations

import copy
from typing import Any

import numpy as np
import orjson
import pytest

from rosetta.domain import canonical as C
from rosetta.domain import errors as E
from rosetta.domain.errors import NormalizeError
from rosetta.engine import codegen
from rosetta.engine.compiler import compile_spec
from rosetta.engine.decoders import MAX_PAYLOAD_BYTES, build_decoder
from rosetta.simulator.dialects import DIALECTS
from rosetta.simulator.run import corrupt

from .conftest import build_truth

VIN = "1HGCM82633A004352"
ODD_INPUTS = [b"", b"{}", b"null", b"[]", b"0", b'"text"', b"true", b"\xff\xfe\x00", b"a|b|c", b"k=v;x=y",
              b"{" * 40, b" " * (MAX_PAYLOAD_BYTES + 1)]


def outcome(adapter, payload: bytes) -> tuple:
    try:
        return ("ok", adapter.normalize(payload))
    except NormalizeError as e:
        return ("error", e.reason, e.field, e.detail)


def both(oem: str, spec: dict[str, Any]):
    fast = compile_spec(oem, 5, spec, specialise=True)
    slow = compile_spec(oem, 5, spec, specialise=False)
    assert fast.specialised and not slow.specialised
    return fast, slow


@pytest.fixture(scope="module")
def fleet_truth() -> dict[str, list]:
    return build_truth(vehicles=250, seed=23, steps=20)


# ------------------------------------------------------------ differential test
@pytest.mark.parametrize("key", sorted(DIALECTS))
def test_specialised_and_interpreted_agree_on_every_dialect(key, fleet_truth):
    dialect = DIALECTS[key]
    fast, slow = both(dialect.oem, dialect.spec)
    rng = np.random.default_rng(99)
    clean = dialect.encode(fleet_truth)
    damaged = [corrupt(p, vin, rng) for p, vin in zip(clean, fleet_truth["vin"])]
    foreign = [p for other in sorted(DIALECTS) if other != key for p in DIALECTS[other].encode(fleet_truth)[:5]]

    ok = failed = 0
    for payload in clean + damaged + foreign + ODD_INPUTS:
        a, b = outcome(fast, payload), outcome(slow, payload)
        assert a == b, payload[:120]
        ok += a[0] == "ok"
        failed += a[0] == "error"
    assert ok >= len(clean), "every clean payload must normalise"
    assert failed >= len(ODD_INPUTS), "the damaged and odd inputs must actually exercise the error paths"


@pytest.mark.parametrize("key", sorted(DIALECTS))
def test_clean_payloads_normalise_without_error(key, fleet_truth):
    dialect = DIALECTS[key]
    fast, _ = both(dialect.oem, dialect.spec)
    for payload, vin, seq in zip(dialect.encode(fleet_truth), fleet_truth["vin"], fleet_truth["seq"]):
        ev = fast.normalize(payload)
        assert (ev["vin"], ev["seq"], ev["oem"], ev["map_v"]) == (vin, seq, dialect.oem, 5)
        assert C.validate(ev) is None


def test_corrupted_payloads_cover_several_reason_codes(fleet_truth):
    rng = np.random.default_rng(7)
    fast, _ = both("nordvik", DIALECTS["nordvik"].spec)
    reasons = set()
    for p, vin in zip(DIALECTS["nordvik"].encode(fleet_truth), fleet_truth["vin"]):
        result = outcome(fast, corrupt(p, vin, rng))
        assert result[0] == "error"
        reasons.add(result[1])
    assert reasons >= {E.DECODE_ERROR, E.INVALID}


SPEC: dict[str, Any] = {
    "decoder": {"type": "json"},
    "fields": {
        "vin": {"path": "vin", "transforms": ["strip", "upper"]},
        "ts": {"path": "t", "transforms": ["iso8601"]},
        "seq": {"path": "n", "transforms": ["to_int"]},
        "lat": {"path": "p[0]", "transforms": [{"op": "unit", "quantity": "angle", "from": "e7"}]},
        "lon": {"path": "p[1]", "transforms": [{"op": "unit", "quantity": "angle", "from": "e7"}]},
        "speed_kmh": {"path": "v", "transforms": [{"op": "scale", "factor": 3.6}, {"op": "round", "digits": 2}]},
        "odo_km": {"path": "o", "transforms": [{"op": "affine", "factor": 1.0, "offset": 0.5}]},
        "heading_deg": {"path": "h", "transforms": [{"op": "unit", "quantity": "angle", "from": "deg"}]},
        "soc_pct": {"path": "s", "transforms": [{"op": "scale", "factor": 1.0}]},
        "fuel_pct": {"path": "f", "transforms": [{"op": "affine", "factor": 100.0}]},
        "ambient_c": {"path": "c", "transforms": [{"op": "unit", "quantity": "temperature", "from": "f"}]},
        "ignition": {"path": "i", "transforms": ["to_bool"]},
        "dtc": {"path": "d", "transforms": [{"op": "split", "sep": ";"}]},
        "evt": {"path": "e", "transforms": ["upper"]},
    },
}


def message(**overrides: Any) -> bytes:
    msg = {"vin": VIN.lower() + " ", "t": "2026-09-25T10:00:00Z", "n": "41", "p": [129_716_000, 775_946_000],
           "v": 12.345, "o": 1000, "h": 181, "s": "76.5", "f": 0.4, "c": 98.6, "i": "yes", "d": "P0301;U0100",
           "e": "idle"}
    msg.update(overrides)
    return orjson.dumps(msg)


def test_every_inlined_transform_matches_the_interpreter():
    fast, slow = both("acme", SPEC)
    ev = fast.normalize(message())
    assert ev == slow.normalize(message())
    assert ev["vin"] == VIN and ev["seq"] == 41 and ev["lat"] == pytest.approx(12.9716)
    assert ev["speed_kmh"] == 44.44 and ev["odo_km"] == 1000.5 and ev["ambient_c"] == pytest.approx(37.0)
    assert ev["fuel_pct"] == pytest.approx(40.0) and ev["soc_pct"] == 76.5 and ev["heading_deg"] == 181.0
    assert ev["dtc"] == ["P0301", "U0100"] and ev["evt"] == "IDLE" and ev["ignition"] is True
    assert {type(ev[k]) for k in ("lat", "lon", "speed_kmh", "odo_km", "heading_deg", "soc_pct", "fuel_pct",
                                  "ambient_c")} == {float}


@pytest.mark.parametrize("overrides", [
    {"n": True}, {"n": 1.5}, {"n": "x"}, {"n": None}, {"n": ""}, {"n": 2 ** 63 - 1}, {"n": -1},
    {"v": True}, {"v": "12"}, {"v": "fast"}, {"v": [1]}, {"v": {"x": 1}}, {"v": "nan"}, {"v": "inf"}, {"v": 1e308},
    {"o": "1e400"}, {"o": None}, {"o": ""}, {"o": False},
    {"p": [1]}, {"p": []}, {"p": "ab"}, {"p": None}, {"p": [None, None]}, {"p": ["1", "2"]}, {"p": [2 ** 40, 1]},
    {"t": "soon"}, {"t": 5}, {"t": "1969-01-01T00:00:00Z"}, {"t": "2026-09-25T10:00:00+05:30"},
    {"vin": ""}, {"vin": None}, {"vin": 5}, {"vin": [VIN]}, {"vin": {"v": VIN}}, {"vin": "1HGCM82633A004353"},
    {"h": 360}, {"h": 360.5}, {"h": None}, {"h": "x"}, {"h": True},
    {"s": ""}, {"s": None}, {"s": "101"}, {"s": []},
    {"c": -500}, {"c": "warm"},
    {"i": "maybe"}, {"i": None}, {"i": 0}, {"i": []},
    {"d": ""}, {"d": None}, {"d": "bogus"}, {"d": ["P0301"]}, {"d": 5},
    {"e": "unknown"}, {"e": ""}, {"e": None}, {"e": 3},
])
def test_both_paths_agree_on_awkward_values(overrides):
    fast, slow = both("acme", SPEC)
    assert outcome(fast, message(**overrides)) == outcome(slow, message(**overrides))


def test_both_paths_report_the_same_first_error_whatever_the_field_order():
    spec = copy.deepcopy(SPEC)
    spec["fields"] = dict(reversed(list(spec["fields"].items())))
    fast, slow = both("acme", spec)
    payload = orjson.loads(message(t="soon"))          # ts cannot be converted ...
    del payload["o"]                                   # ... and the odometer is missing
    raw = orjson.dumps(payload)
    assert outcome(fast, raw) == outcome(slow, raw)


def test_field_order_does_not_matter_for_valid_payloads():
    spec = copy.deepcopy(SPEC)
    spec["fields"] = dict(reversed(list(spec["fields"].items())))
    fast, slow = both("acme", spec)
    assert fast.normalize(message()) == slow.normalize(message()) == compile_spec("acme", 5, SPEC).normalize(message())


# ------------------------------------------------------------------ generation
def test_generate_returns_a_function_and_its_source():
    decode = build_decoder(SPEC["decoder"])
    fn, src = codegen.generate("acme", 2, SPEC, decode)
    assert callable(fn)
    assert src.startswith("def normalize(payload):")
    compile(src, "<check>", "exec")
    assert fn(message())["map_v"] == 2


def test_source_contains_no_imports_or_attribute_tricks():
    src = compile_spec("acme", 1, SPEC).source
    assert "import" not in src and "__import__" not in src and "eval(" not in src and "exec(" not in src


HOSTILE = ["x' or __import__('os') or '", 'a"); raise SystemExit #', "line\nbreak", "back\\slash", "{curly}",
           "%s", "tab\there", "'" * 3, "\\'"]


@pytest.mark.parametrize("path", ["x'] or __import__('os').system('echo pwned') or ['", "a[0]; import os",
                                  "a[__import__('os')]"])
def test_hostile_path_that_tries_to_close_the_subscript_is_rejected(path):
    spec = copy.deepcopy(SPEC)
    spec["fields"]["odo_km"] = {"path": path}
    with pytest.raises(E.SpecError):
        compile_spec("acme", 1, spec)


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_path_is_only_ever_a_dictionary_key(name):
    spec = copy.deepcopy(SPEC)
    spec["fields"]["odo_km"] = {"path": name}
    fast, slow = both("acme", spec)
    raw = orjson.dumps({**orjson.loads(message()), name: 77.0})
    assert fast.normalize(raw)["odo_km"] == 77.0
    assert outcome(fast, raw) == outcome(slow, raw)
    assert outcome(fast, message()) == outcome(slow, message()) == \
        ("error", E.SCHEMA_MISMATCH, "odo_km", f"source field {name!r} is missing")


@pytest.mark.parametrize("oem", ["o'brien", 'quo"te', "new\nline", "x'; import os; '"])
def test_hostile_source_id_is_only_ever_a_string(oem):
    fast, slow = both(oem, SPEC)
    assert fast.normalize(message())["oem"] == oem
    assert fast.normalize(message()) == slow.normalize(message())


def test_hostile_enum_values_stay_data():
    spec = copy.deepcopy(SPEC)
    spec["fields"]["evt"] = {"path": "e", "transforms": [{"op": "enum", "map": {"__import__('os')": "IDLE"}}]}
    fast, slow = both("acme", spec)
    raw = message(e="__import__('os')")
    assert fast.normalize(raw)["evt"] == "IDLE"
    assert fast.normalize(raw) == slow.normalize(raw)


def test_version_is_embedded_as_an_integer():
    fn, src = codegen.generate("acme", "7", SPEC, build_decoder(SPEC["decoder"]))
    assert fn(message())["map_v"] == 7
    assert "ev['map_v'] = 7" in src


def test_generate_refuses_a_spec_without_every_required_field():
    spec = copy.deepcopy(SPEC)
    del spec["fields"]["odo_km"]
    with pytest.raises(ValueError, match="odo_km"):
        codegen.generate("acme", 1, spec, build_decoder(spec["decoder"]))


@pytest.mark.parametrize("transform", [{"op": "scale", "factor": float("inf")},
                                       {"op": "affine", "factor": 1.0, "offset": float("nan")}])
def test_generate_refuses_non_finite_numbers(transform):
    spec = copy.deepcopy(SPEC)
    spec["fields"]["odo_km"]["transforms"] = [transform]
    with pytest.raises(ValueError):
        codegen.generate("acme", 1, spec, build_decoder(spec["decoder"]))


def test_generated_function_checks_the_size_before_decoding():
    calls = []

    def decode(payload: bytes):
        calls.append(len(payload))
        return orjson.loads(payload)

    fn, _ = codegen.generate("acme", 1, SPEC, decode)
    with pytest.raises(NormalizeError) as info:
        fn(b" " * (MAX_PAYLOAD_BYTES + 1))
    assert info.value.reason == E.OVERSIZE and calls == []
    fn(message())
    assert len(calls) == 1


# -------------------------------------------------------------------- helpers
@pytest.mark.parametrize("transform,expected", [
    ({"op": "unit", "quantity": "speed", "from": "mph"}, (1.609344, 0.0, False)),
    ({"op": "unit", "quantity": "temperature", "from": "k"}, (1.0, -273.15, False)),
    ({"op": "unit", "quantity": "time", "from": "epoch_s"}, (1000.0, 0.0, True)),
    ({"op": "scale", "factor": 2}, (2.0, 0.0, False)),
    ({"op": "affine", "factor": 2, "offset": 3}, (2.0, 3.0, False)),
    ({"op": "affine", "factor": 2}, (2.0, 0.0, False)),
])
def test_numeric_inline_recognises_plain_arithmetic(transform, expected):
    assert codegen._numeric_inline(transform) == expected


@pytest.mark.parametrize("transform", ["iso8601", "to_int", {"op": "enum", "map": {}}, {"op": "round", "digits": 1},
                                       {"op": "split"}])
def test_numeric_inline_leaves_other_transforms_to_their_closures(transform):
    assert codegen._numeric_inline(transform) is None


def test_check_vin_accepts_and_caches_a_valid_vin():
    C._VIN_OK.discard(VIN)
    codegen._check_vin(VIN)
    assert VIN in C._VIN_OK
    codegen._check_vin(VIN)


def test_check_vin_clears_a_full_cache(monkeypatch):
    monkeypatch.setattr(C, "_VIN_OK", {"OLD"})
    monkeypatch.setattr(C, "_VIN_CACHE_MAX", 1)
    codegen._check_vin(VIN)
    assert C._VIN_OK == {VIN}


@pytest.mark.parametrize("vin", ["1HGCM82633A004353", "", "short"])
def test_check_vin_rejects_invalid_vins(vin):
    with pytest.raises(NormalizeError) as info:
        codegen._check_vin(vin)
    assert (info.value.reason, info.value.field, info.value.detail) == (E.INVALID, "vin", C.R_VIN)


def test_check_dtc():
    codegen._check_dtc(["P0301", "U0100"])
    codegen._check_dtc([])
    for bad, detail in ((("P0301",), C.R_TYPE), ("P0301", C.R_TYPE), (["p0301"], C.R_DTC), ([5], C.R_DTC)):
        with pytest.raises(NormalizeError) as info:
            codegen._check_dtc(bad)
        assert (info.value.reason, info.value.field, info.value.detail) == (E.INVALID, "dtc", detail)
