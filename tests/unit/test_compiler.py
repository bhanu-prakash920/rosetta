"""rosetta.engine.compiler: a mapping spec (data) becomes an Adapter (callable).

Every behaviour is checked on both execution paths: the interpreter and the
specialised function from codegen. They must be indistinguishable.
"""
from __future__ import annotations

import copy
from typing import Any

import orjson
import pytest

from rosetta.domain import errors as E
from rosetta.domain.errors import NormalizeError, SpecError
from rosetta.engine import compiler
from rosetta.engine.compiler import MAX_TRANSFORMS, Adapter, compile_spec
from rosetta.engine.decoders import MAX_PAYLOAD_BYTES

VIN = "1HGCM82633A004352"

SPEC: dict[str, Any] = {
    "oem": "acme",
    "decoder": {"type": "json"},
    "fields": {
        "vin": {"path": "vin"},
        "ts": {"path": "t", "transforms": [{"op": "unit", "quantity": "time", "from": "epoch_s"}]},
        "seq": {"path": "n"},
        "lat": {"path": "pos.lat"},
        "lon": {"path": "pos.lon"},
        "speed_kmh": {"path": "mph", "transforms": [{"op": "unit", "quantity": "speed", "from": "mph"}]},
        "odo_km": {"path": "odo"},
        "soc_pct": {"path": "soc"},
        "ignition": {"path": "ign", "transforms": ["to_bool"]},
        "dtc": {"path": "codes"},
        "evt": {"path": "e", "transforms": [{"op": "enum", "map": {"HB": "HARSH_BRAKE"}}]},
    },
}


def message(**overrides: Any) -> dict[str, Any]:
    msg = {"vin": VIN, "t": 1_790_000_000, "n": 7, "pos": {"lat": 12.5, "lon": 77.5}, "mph": 40, "odo": 1000.5,
           "soc": 80, "ign": "ON", "codes": ["P0420"], "e": "HB"}
    msg.update(overrides)
    return msg


def payload(**overrides: Any) -> bytes:
    return orjson.dumps(message(**overrides))


def without(*keys: str) -> bytes:
    msg = message()
    for k in keys:
        del msg[k]
    return orjson.dumps(msg)


def edited(fn) -> dict[str, Any]:
    spec = copy.deepcopy(SPEC)
    fn(spec)
    return spec


@pytest.fixture(params=[True, False], ids=["specialised", "interpreted"])
def adapter(request) -> Adapter:
    a = compile_spec("acme", 3, SPEC, specialise=request.param)
    assert a.specialised is request.param
    return a


def failure(adapter: Adapter, raw: bytes) -> NormalizeError:
    with pytest.raises(NormalizeError) as info:
        adapter.normalize(raw)
    return info.value


# ------------------------------------------------------------------ happy path
def test_normalize_produces_a_canonical_event(adapter):
    ev = adapter.normalize(payload())
    assert ev == {
        "vin": VIN, "ts": 1_790_000_000_000, "seq": 7, "lat": 12.5, "lon": 77.5,
        "speed_kmh": pytest.approx(64.37376), "odo_km": 1000.5, "soc_pct": 80.0, "ignition": True,
        "dtc": ["P0420"], "evt": "HARSH_BRAKE", "oem": "acme", "map_v": 3,
    }


def test_values_are_coerced_to_the_canonical_types(adapter):
    ev = adapter.normalize(payload(n="12", odo=1000, soc="80.5", pos={"lat": "12.5", "lon": 77}))
    assert ev["seq"] == 12 and type(ev["seq"]) is int
    assert ev["odo_km"] == 1000.0 and type(ev["odo_km"]) is float
    assert ev["soc_pct"] == 80.5 and type(ev["soc_pct"]) is float
    assert (ev["lat"], ev["lon"]) == (12.5, 77.0) and type(ev["lon"]) is float


def test_fractional_integer_field_is_rounded(adapter):
    assert adapter.normalize(payload(n=7.6))["seq"] == 8


def test_scalar_is_wrapped_for_a_list_field(adapter):
    assert adapter.normalize(payload(codes="P0301"))["dtc"] == ["P0301"]


def test_optional_fields_may_be_missing(adapter):
    ev = adapter.normalize(without("soc", "ign", "codes", "e"))
    assert set(ev) == {"vin", "ts", "seq", "lat", "lon", "speed_kmh", "odo_km", "oem", "map_v"}


@pytest.mark.parametrize("empty", [None, ""])
def test_optional_field_that_is_null_or_empty_is_left_out(adapter, empty):
    ev = adapter.normalize(payload(soc=empty, e=empty))
    assert "soc_pct" not in ev and "evt" not in ev


def test_transform_that_yields_none_leaves_the_field_out(adapter):
    assert "evt" not in adapter.normalize(payload(e="UNMAPPED"))


def test_each_call_returns_a_new_event(adapter):
    a, b = adapter.normalize(payload()), adapter.normalize(payload())
    assert a == b and a is not b


def test_adapter_exposes_oem_version_and_spec(adapter):
    assert (adapter.oem, adapter.version) == ("acme", 3)
    assert adapter.spec is SPEC


def test_specialised_adapter_keeps_its_source_and_the_interpreter_has_none():
    assert "def normalize(payload)" in compile_spec("acme", 1, SPEC, specialise=True).source
    assert compile_spec("acme", 1, SPEC, specialise=False).source == ""


def test_specialise_is_the_default():
    assert compile_spec("acme", 1, SPEC).specialised is True


def test_decode_and_map_decoded_are_the_two_halves_of_normalize(adapter):
    obj = adapter.decode(payload())
    assert obj["pos"] == {"lat": 12.5, "lon": 77.5}
    mapped = adapter.map_decoded(obj)
    full = adapter.normalize(payload())
    assert mapped == {k: v for k, v in full.items() if k not in ("oem", "map_v")}


def test_map_decoded_does_not_validate(adapter):
    mapped = adapter.map_decoded(message(vin="NOT-A-VIN", pos={"lat": 500, "lon": 0}))
    assert mapped["vin"] == "NOT-A-VIN" and mapped["lat"] == 500.0


def test_interpreter_stays_available_on_a_specialised_adapter():
    a = compile_spec("acme", 3, SPEC)
    assert a.normalize_interpreted(payload()) == a.normalize(payload())


# ---------------------------------------------------------------- reason codes
def test_oversize(adapter):
    err = failure(adapter, b" " * (MAX_PAYLOAD_BYTES + 1))
    assert (err.reason, err.field, err.detail) == (E.OVERSIZE, "", f"{MAX_PAYLOAD_BYTES + 1} bytes")


def test_payload_of_exactly_the_limit_is_not_oversize(adapter):
    raw = payload()
    padded = raw + b" " * (MAX_PAYLOAD_BYTES - len(raw))
    assert len(padded) == MAX_PAYLOAD_BYTES
    assert adapter.normalize(padded)["vin"] == VIN


@pytest.mark.parametrize("raw", [b"", b"{", b"not json", b"\xff\xfe"])
def test_decode_error(adapter, raw):
    err = failure(adapter, raw)
    assert err.reason == E.DECODE_ERROR and err.field == ""


@pytest.mark.parametrize("source,field", [("vin", "vin"), ("t", "ts"), ("n", "seq"), ("pos", "lat"),
                                          ("mph", "speed_kmh"), ("odo", "odo_km")])
def test_schema_mismatch_names_the_missing_field(adapter, source, field):
    err = failure(adapter, without(source))
    assert (err.reason, err.field) == (E.SCHEMA_MISMATCH, field)
    assert err.detail == f"source field {SPEC['fields'][field]['path']!r} is missing"


@pytest.mark.parametrize("empty", [None, ""])
def test_required_field_that_is_null_or_empty_is_a_schema_mismatch(adapter, empty):
    err = failure(adapter, payload(odo=empty))
    assert (err.reason, err.field) == (E.SCHEMA_MISMATCH, "odo_km")


@pytest.mark.parametrize("raw", [b"{}", b"null", b"[]", b"12", b'"text"'])
def test_json_that_is_not_the_expected_object_is_a_schema_mismatch(adapter, raw):
    err = failure(adapter, raw)
    assert (err.reason, err.field) == (E.SCHEMA_MISMATCH, "vin")


@pytest.mark.parametrize("overrides,field", [
    ({"t": "yesterday"}, "ts"),
    ({"mph": "fast"}, "speed_kmh"),
    ({"mph": True}, "speed_kmh"),
    ({"n": "seven"}, "seq"),
    ({"odo": {"km": 5}}, "odo_km"),
    ({"soc": "full"}, "soc_pct"),
    ({"ign": "maybe"}, "ignition"),
    ({"pos": {"lat": [1], "lon": 2}}, "lat"),
])
def test_transform_error_names_the_field_and_the_source_path(adapter, overrides, field):
    err = failure(adapter, payload(**overrides))
    assert (err.reason, err.field) == (E.TRANSFORM_ERROR, field)
    assert err.detail.startswith(SPEC["fields"][field]["path"] + ": ")


def test_transform_error_detail_is_bounded(adapter):
    err = failure(adapter, payload(mph="x" * 5000))
    assert err.reason == E.TRANSFORM_ERROR
    assert len(err.detail) <= len("mph: ") + 100


@pytest.mark.parametrize("overrides,field,detail", [
    ({"vin": "1HGCM82633A004353"}, "vin", "INVALID_VIN"),
    ({"vin": 12345}, "vin", "INVALID_VIN"),
    ({"pos": {"lat": 91, "lon": 0}}, "lat", "OUT_OF_RANGE"),
    ({"pos": {"lat": 0, "lon": -181}}, "lon", "OUT_OF_RANGE"),
    ({"pos": {"lat": "nan", "lon": 0}}, "lat", "OUT_OF_RANGE"),
    ({"mph": 500}, "speed_kmh", "OUT_OF_RANGE"),
    ({"mph": -1}, "speed_kmh", "OUT_OF_RANGE"),
    ({"t": 1}, "ts", "OUT_OF_RANGE"),
    ({"n": -1}, "seq", "OUT_OF_RANGE"),
    ({"soc": 101}, "soc_pct", "OUT_OF_RANGE"),
    ({"codes": ["P0420", "bogus"]}, "dtc", "INVALID_DTC"),
    ({"codes": "bogus"}, "dtc", "INVALID_DTC"),
])
def test_invalid_event_carries_the_validation_reason(adapter, overrides, field, detail):
    err = failure(adapter, payload(**overrides))
    assert (err.reason, err.field, err.detail) == (E.INVALID, field, detail)


def test_unknown_event_type_is_invalid():
    spec = edited(lambda s: s["fields"]["evt"].pop("transforms"))
    for specialise in (True, False):
        err = failure(compile_spec("acme", 1, spec, specialise=specialise), payload(e="WHATEVER"))
        assert (err.reason, err.field, err.detail) == (E.INVALID, "evt", "UNKNOWN_EVENT_TYPE")


def test_ignition_that_is_not_a_bool_is_invalid():
    spec = edited(lambda s: s["fields"]["ignition"].pop("transforms"))
    for specialise in (True, False):
        err = failure(compile_spec("acme", 1, spec, specialise=specialise), payload(ign=1))
        assert (err.reason, err.field, err.detail) == (E.INVALID, "ignition", "BAD_TYPE")


def test_none_from_a_transform_cannot_be_coerced_to_a_number():
    # Only fields without a type coercion (evt, ignition) can be dropped by a transform that
    # returns None. For a numeric field the coercion that follows fails on None.
    spec = edited(lambda s: s["fields"]["odo_km"].update(transforms=[{"op": "enum", "map": {}}]))
    errors = [failure(compile_spec("acme", 1, spec, specialise=flag), payload()) for flag in (True, False)]
    assert [(e.reason, e.field) for e in errors] == [(E.TRANSFORM_ERROR, "odo_km")] * 2
    assert errors[0].detail == errors[1].detail


def test_a_missing_required_field_is_reported_before_a_bad_optional_one(adapter):
    raw = orjson.dumps({k: v for k, v in message(soc="broken").items() if k != "odo"})
    err = failure(adapter, raw)
    assert (err.reason, err.field) == (E.SCHEMA_MISMATCH, "odo_km")


# ------------------------------------------------------------- spec validation
@pytest.mark.parametrize("spec", [None, [], "spec", 5, [("fields", {})]])
def test_spec_must_be_an_object(spec):
    with pytest.raises(SpecError, match="spec must be an object"):
        compile_spec("acme", 1, spec)


@pytest.mark.parametrize("key", ["code", "python", "transforms", "Fields", "decoders"])
def test_unknown_top_level_keys_are_rejected(key):
    with pytest.raises(SpecError, match="unexpected keys") as info:
        compile_spec("acme", 1, {**SPEC, key: "x"})
    assert key in str(info.value)


def test_documented_top_level_keys_are_accepted():
    spec = {**SPEC, "notes": "reviewed by ops", "schema_version": 1}
    assert compile_spec("acme", 1, spec).normalize(payload())["vin"] == VIN


@pytest.mark.parametrize("fields", [None, {}, [], "vin", [{"vin": {"path": "vin"}}]])
def test_fields_must_be_a_non_empty_object(fields):
    with pytest.raises(SpecError, match="spec.fields"):
        compile_spec("acme", 1, {**SPEC, "fields": fields})


def test_missing_fields_key_is_rejected():
    with pytest.raises(SpecError, match="spec.fields"):
        compile_spec("acme", 1, {"decoder": {"type": "json"}})


@pytest.mark.parametrize("name", ["speed", "velocity", "VIN", "oem", "map_v", "rx_ts", "__class__"])
def test_unknown_canonical_fields_are_rejected(name):
    spec = edited(lambda s: s["fields"].update({name: {"path": "x"}}))
    with pytest.raises(SpecError, match="unknown canonical fields") as info:
        compile_spec("acme", 1, spec)
    assert name in str(info.value)


@pytest.mark.parametrize("name", ["vin", "ts", "seq", "lat", "lon", "speed_kmh", "odo_km"])
def test_every_required_field_must_be_mapped(name):
    spec = edited(lambda s: s["fields"].pop(name))
    with pytest.raises(SpecError, match="required canonical fields are not mapped") as info:
        compile_spec("acme", 1, spec)
    assert name in str(info.value)


def test_optional_fields_need_not_be_mapped():
    spec = edited(lambda s: [s["fields"].pop(n) for n in ("soc_pct", "ignition", "dtc", "evt")])
    ev = compile_spec("acme", 1, spec).normalize(payload())
    assert "soc_pct" not in ev and "dtc" not in ev


@pytest.mark.parametrize("mapping", [None, "vin", ["vin"], {}, {"transforms": ["upper"]}, 5])
def test_field_mapping_needs_a_path(mapping):
    spec = edited(lambda s: s["fields"].update(vin=mapping))
    with pytest.raises(SpecError, match="needs a 'path'"):
        compile_spec("acme", 1, spec)


@pytest.mark.parametrize("path", ["", None, 5, "a..b", "a[x]", "x" * 201])
def test_field_path_is_validated(path):
    spec = edited(lambda s: s["fields"]["vin"].update(path=path))
    with pytest.raises(SpecError):
        compile_spec("acme", 1, spec)


@pytest.mark.parametrize("transforms", ["upper", {"op": "upper"}, 5, ("upper",)])
def test_transforms_must_be_a_list(transforms):
    spec = edited(lambda s: s["fields"]["vin"].update(transforms=transforms))
    with pytest.raises(SpecError, match="transforms must be a list"):
        compile_spec("acme", 1, spec)


def test_too_many_transforms_are_rejected():
    spec = edited(lambda s: s["fields"]["vin"].update(transforms=["strip"] * (MAX_TRANSFORMS + 1)))
    with pytest.raises(SpecError, match=f"at most {MAX_TRANSFORMS}"):
        compile_spec("acme", 1, spec)


def test_the_maximum_number_of_transforms_is_accepted():
    spec = edited(lambda s: s["fields"]["vin"].update(transforms=["strip"] * MAX_TRANSFORMS))
    assert compile_spec("acme", 1, spec).normalize(payload(vin=f"  {VIN} "))["vin"] == VIN


@pytest.mark.parametrize("empty", [None, []])
def test_empty_transform_list_is_allowed(empty):
    spec = edited(lambda s: s["fields"]["vin"].update(transforms=empty))
    assert compile_spec("acme", 1, spec).normalize(payload())["vin"] == VIN


@pytest.mark.parametrize("transform", ["eval", {"op": "exec"}, {"op": "upper", "code": "x"},
                                       {"op": "unit", "quantity": "speed", "from": "warp"}, {"factor": 2}, 5,
                                       {"op": "scale", "factor": float("inf")}])
def test_a_transform_outside_the_whitelist_is_a_spec_error(transform):
    spec = edited(lambda s: s["fields"]["speed_kmh"].update(transforms=[transform]))
    with pytest.raises(SpecError, match="field 'speed_kmh'"):
        compile_spec("acme", 1, spec)


@pytest.mark.parametrize("decoder", [None, {}, {"type": "xml"}, "json", {"type": "delimited"},
                                     {"type": "json", "pivot": {}}])
def test_decoder_is_validated(decoder):
    with pytest.raises(SpecError):
        compile_spec("acme", 1, {**SPEC, "decoder": decoder})


def test_missing_decoder_is_rejected():
    with pytest.raises(SpecError, match="decoder.type"):
        compile_spec("acme", 1, {"fields": SPEC["fields"]})


@pytest.mark.parametrize("edit", [
    lambda s: s["fields"]["ts"].update(transforms=[{"op": ["unit"]}]),
    lambda s: s["fields"]["ts"].update(transforms=[{"op": "unit", "quantity": ["time"], "from": "epoch_s"}]),
    lambda s: s.update(decoder={"type": ["json"]}),
    lambda s: s.update(decoder={"type": "json", "pivot": "signals"}),
    lambda s: s["fields"]["vin"].update(path="a[--1]"),
], ids=["op-is-a-list", "unit-quantity-is-a-list", "decoder-type-is-a-list", "pivot-is-a-string", "index--1"])
def test_every_malformed_spec_is_a_spec_error(edit):
    with pytest.raises(SpecError):
        compile_spec("acme", 1, edited(edit))


# ----------------------------------------------------------------- internals
def test_plan_puts_required_fields_first():
    spec = edited(lambda s: s.update(fields=dict(reversed(list(s["fields"].items())))))
    plan = compiler._build_plan(spec)
    required = [p[3] for p in plan]
    assert required == sorted(required, reverse=True)
    # within a group the canonical order, whatever the spec order: the interpreter and the
    # specialised function must meet a faulty field in the same order and report the same error
    from rosetta.domain.canonical import CANONICAL_FIELDS

    order = [f.name for f in CANONICAL_FIELDS]
    names = [p[0] for p in plan]
    assert names == sorted(names, key=lambda n: (n not in [x for x in order if compiler.C.FIELD_BY_NAME[x].required],
                                                 order.index(n)))


@pytest.mark.parametrize("kind,raw,expected", [
    ("int", 5, 5), ("int", "5", 5), ("int", 5.5, 6), ("int", True, 1),
    ("float", 5, 5.0), ("float", "2.5", 2.5), ("float", 2.5, 2.5),
    ("str", 12, "12"), ("str", "x", "x"),
    ("list[str]", "P0420", ["P0420"]), ("list[str]", ["P0420"], ["P0420"]),
])
def test_coerce(kind, raw, expected):
    got = compiler._coerce(kind)(raw)
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize("kind", ["bool", "enum"])
def test_bool_and_enum_are_not_coerced(kind):
    assert compiler._coerce(kind) is None


def test_adapter_falls_back_to_the_interpreter_when_code_generation_fails(monkeypatch):
    from rosetta.engine import codegen

    def boom(*_args, **_kwargs):
        raise RuntimeError("cannot specialise")

    monkeypatch.setattr(codegen, "generate", boom)
    a = compile_spec("acme", 3, SPEC, specialise=True)
    assert a.specialised is False and a.source == ""
    assert a.normalize(payload())["map_v"] == 3
