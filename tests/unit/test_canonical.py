"""rosetta.domain.canonical: the canonical event and its validation rules."""
from __future__ import annotations

import math

import jsonschema
import pytest

from rosetta.domain import canonical as C

RANGED = [(f.name, f.lo, f.hi) for f in C.CANONICAL_FIELDS if f.lo is not None]
INTEGER_FIELDS = ("ts", "seq")


def _inside(name: str, value: float):
    """A value of the right Python type for the field."""
    return int(value) if name in INTEGER_FIELDS else float(value)


def test_valid_event_passes(valid_event):
    assert C.validate(valid_event()) is None


def test_event_with_only_required_fields_passes(valid_event):
    ev = {k: v for k, v in valid_event().items() if k in C.REQUIRED}
    assert C.validate(ev) is None


def test_validation_does_not_change_the_event(valid_event):
    ev = valid_event()
    before = dict(ev)
    C.validate(ev)
    assert ev == before


def test_unknown_extra_keys_are_allowed(valid_event):
    assert C.validate(valid_event(oem="nordvik", map_v=3, anything="else")) is None


@pytest.mark.parametrize("name", C.REQUIRED)
def test_missing_required_field(valid_event, name):
    ev = valid_event()
    del ev[name]
    assert C.validate(ev) == (C.R_MISSING, name)


@pytest.mark.parametrize("name", C.REQUIRED)
def test_required_field_set_to_none_counts_as_missing(valid_event, name):
    assert C.validate(valid_event(**{name: None})) == (C.R_MISSING, name)


def test_first_missing_field_is_reported_in_declaration_order():
    assert C.validate({}) == (C.R_MISSING, "vin")


@pytest.mark.parametrize("name", C.OPTIONAL)
def test_optional_field_may_be_absent_or_none(valid_event, name):
    ev = valid_event()
    del ev[name]
    assert C.validate(ev) is None
    assert C.validate(valid_event(**{name: None})) is None


@pytest.mark.parametrize("vin", ["1HGCM82633A004353", "1HGCM82633A00435", "IHGCM82633A004352", "", 0, 17])
def test_invalid_vin(valid_event, vin):
    assert C.validate(valid_event(vin=vin)) == (C.R_VIN, "vin")


def test_valid_vin_is_remembered_and_invalid_is_not(valid_event):
    C._VIN_OK.discard("1HGCM82633A004352")
    assert C.validate(valid_event()) is None
    assert "1HGCM82633A004352" in C._VIN_OK
    C.validate(valid_event(vin="1HGCM82633A004353"))
    assert "1HGCM82633A004353" not in C._VIN_OK


def test_vin_cache_is_cleared_when_full(valid_event, monkeypatch):
    monkeypatch.setattr(C, "_VIN_OK", {"OLD"})
    monkeypatch.setattr(C, "_VIN_CACHE_MAX", 1)
    assert C.validate(valid_event()) is None
    assert C._VIN_OK == {"1HGCM82633A004352"}


@pytest.mark.parametrize("vin", [["1HGCM82633A004352"], {"v": "1HGCM82633A004352"}])
def test_unhashable_vin_is_reported_not_raised(valid_event, vin):
    assert C.validate(valid_event(vin=vin)) == (C.R_VIN, "vin")


@pytest.mark.parametrize("name,lo,hi", RANGED)
def test_range_bounds_are_inclusive(valid_event, name, lo, hi):
    assert C.validate(valid_event(**{name: _inside(name, lo)})) is None
    assert C.validate(valid_event(**{name: _inside(name, hi)})) is None


@pytest.mark.parametrize("name,lo,hi", RANGED)
def test_value_below_the_range(valid_event, name, lo, hi):
    below = lo - 1 if name in INTEGER_FIELDS else lo - 0.001
    assert C.validate(valid_event(**{name: below})) == (C.R_RANGE, name)


@pytest.mark.parametrize("name,lo,hi", RANGED)
def test_value_above_the_range(valid_event, name, lo, hi):
    above = hi + 1 if name in INTEGER_FIELDS else hi + 0.001
    assert C.validate(valid_event(**{name: above})) == (C.R_RANGE, name)


@pytest.mark.parametrize("name", [n for n, _, _ in RANGED])
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
def test_nan_and_infinity_are_out_of_range(valid_event, name, bad):
    assert C.validate(valid_event(**{name: bad})) == (C.R_RANGE, name)


@pytest.mark.parametrize("name", [n for n, _, _ in RANGED])
@pytest.mark.parametrize("flag", [True, False])
def test_bool_is_not_accepted_as_a_number(valid_event, name, flag):
    assert C.validate(valid_event(**{name: flag})) == (C.R_TYPE, name)


@pytest.mark.parametrize("name", [n for n, _, _ in RANGED])
@pytest.mark.parametrize("bad", ["12", [12], {"v": 12}, b"12"], ids=["str", "list", "dict", "bytes"])
def test_non_numeric_value_is_a_type_error(valid_event, name, bad):
    assert C.validate(valid_event(**{name: bad})) == (C.R_TYPE, name)


def test_integer_is_accepted_for_float_fields(valid_event):
    assert C.validate(valid_event(lat=12, lon=77, speed_kmh=0, odo_km=10)) is None


@pytest.mark.parametrize("name", INTEGER_FIELDS)
def test_float_is_not_accepted_for_integer_fields(valid_event, name):
    value = float(valid_event()[name])
    assert C.validate(valid_event(**{name: value})) == (C.R_TYPE, name)


def test_ts_is_reported_before_seq_when_both_are_floats(valid_event):
    assert C.validate(valid_event(ts=float(valid_event()["ts"]), seq=1.0)) == (C.R_TYPE, "ts")


@pytest.mark.parametrize("bad", [1, 0, "true", "ON", 1.0, [True]])
def test_ignition_must_be_a_bool(valid_event, bad):
    assert C.validate(valid_event(ignition=bad)) == (C.R_TYPE, "ignition")


@pytest.mark.parametrize("flag", [True, False])
def test_ignition_bool_is_accepted(valid_event, flag):
    assert C.validate(valid_event(ignition=flag)) is None


@pytest.mark.parametrize("bad", ["P0420", ("P0420",), {"P0420"}, {"code": "P0420"}])
def test_dtc_must_be_a_list(valid_event, bad):
    assert C.validate(valid_event(dtc=bad)) == (C.R_TYPE, "dtc")


@pytest.mark.parametrize("bad", [["p0420"], ["P0420", "nope"], ["P0420", 301], [None], ["P04201"], [""]])
def test_dtc_list_with_an_invalid_code(valid_event, bad):
    assert C.validate(valid_event(dtc=bad)) == (C.R_DTC, "dtc")


def test_empty_dtc_list_is_valid(valid_event):
    assert C.validate(valid_event(dtc=[])) is None


def test_several_valid_dtcs(valid_event):
    assert C.validate(valid_event(dtc=["P0301", "U0100", "C0035", "B1342"])) is None


@pytest.mark.parametrize("evt", C.EVENT_TYPES)
def test_every_known_event_type_is_accepted(valid_event, evt):
    assert C.validate(valid_event(evt=evt)) is None


@pytest.mark.parametrize("evt", ["harsh_brake", "UNKNOWN", "", 0, 1])
def test_unknown_event_type(valid_event, evt):
    assert C.validate(valid_event(evt=evt)) == (C.R_EVT, "evt")


def test_checks_run_in_a_fixed_order(valid_event):
    # vin before ranges, ranges before ignition, ignition before dtc, dtc before evt
    ev = valid_event(vin="BAD", lat=999.0, ignition=1, dtc=["bad"], evt="bad")
    assert C.validate(ev) == (C.R_VIN, "vin")
    ev["vin"] = "1HGCM82633A004352"
    assert C.validate(ev) == (C.R_RANGE, "lat")
    ev["lat"] = 1.0
    assert C.validate(ev) == (C.R_TYPE, "ignition")
    ev["ignition"] = True
    assert C.validate(ev) == (C.R_DTC, "dtc")
    ev["dtc"] = []
    assert C.validate(ev) == (C.R_EVT, "evt")


def test_field_tables_are_consistent():
    names = [f.name for f in C.CANONICAL_FIELDS]
    assert len(names) == len(set(names))
    assert set(C.REQUIRED) | set(C.OPTIONAL) == set(names)
    assert not set(C.REQUIRED) & set(C.OPTIONAL)
    assert C.REQUIRED == ("vin", "ts", "seq", "lat", "lon", "speed_kmh", "odo_km")
    assert set(C.FIELD_BY_NAME) == set(names)


def test_pii_fields_are_flagged():
    assert {f.name for f in C.CANONICAL_FIELDS if f.pii} == {"vin", "lat", "lon"}


# ------------------------------------------------------------------ JSON Schema
def test_json_schema_is_a_valid_schema():
    jsonschema.Draft202012Validator.check_schema(C.json_schema())


def test_json_schema_accepts_a_valid_event(valid_event):
    jsonschema.validate(valid_event(), C.json_schema())


def test_json_schema_lists_the_required_fields():
    schema = C.json_schema()
    assert schema["required"] == list(C.REQUIRED)
    assert set(schema["properties"]) == set(C.FIELD_BY_NAME)
    assert schema["$id"].endswith(f"v{C.SCHEMA_VERSION}")


def test_json_schema_carries_units_ranges_and_pii_markers():
    props = C.json_schema()["properties"]
    assert props["speed_kmh"]["x-unit"] == "kmh"
    assert (props["lat"]["minimum"], props["lat"]["maximum"]) == (-90.0, 90.0)
    assert props["vin"]["x-pii"] is True
    assert "x-pii" not in props["speed_kmh"]
    assert props["evt"]["enum"] == list(C.EVENT_TYPES)


@pytest.mark.parametrize("override", [{"lat": 91.0}, {"vin": "short"}, {"evt": "NOPE"}, {"dtc": ["bad"]},
                                      {"ignition": "yes"}, {"ts": "now"}, {"soc_pct": 101}])
def test_json_schema_and_validate_agree_on_invalid_events(valid_event, override):
    ev = valid_event(**override)
    assert C.validate(ev) is not None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ev, C.json_schema())


@pytest.mark.parametrize("name", C.REQUIRED)
def test_json_schema_requires_every_required_field(valid_event, name):
    ev = valid_event()
    del ev[name]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ev, C.json_schema())
