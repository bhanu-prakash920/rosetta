"""rosetta.domain.transforms: the whitelist of value transforms a mapping spec may use."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from rosetta.domain import transforms as T
from rosetta.domain.canonical import FIELD_BY_NAME
from rosetta.domain.transforms import TransformError, compile_transform


def unit(quantity: str, src: str):
    return compile_transform({"op": "unit", "quantity": quantity, "from": src})


# ------------------------------------------------------------------------ unit
@pytest.mark.parametrize("quantity,src,raw,expected", [
    ("speed", "kmh", 88.0, 88.0),
    ("speed", "mph", 40.0, 64.37376),
    ("speed", "mph", 60.0, 96.56064),
    ("speed", "mps", 10.0, 36.0),
    ("speed", "knots", 10.0, 18.52),
    ("speed", "cm_s", 1000.0, 36.0),
    ("speed", "centi_kmh", 5423, 54.23),
    ("distance", "km", 12.5, 12.5),
    ("distance", "mi", 100.0, 160.9344),
    ("distance", "m", 18234500, 18234.5),
    ("distance", "hm", 182345, 18234.5),
    ("temperature", "c", -12.5, -12.5),
    ("temperature", "f", 98.6, 37.0),
    ("temperature", "f", 32.0, 0.0),
    ("temperature", "f", -40.0, -40.0),
    ("temperature", "k", 273.15, 0.0),
    ("temperature", "k", 300.0, 26.85),
    ("temperature", "deci_c", 275, 27.5),
    ("percent", "pct", 76.5, 76.5),
    ("percent", "fraction", 0.765, 76.5),
    ("percent", "permille", 765, 76.5),
    ("percent", "byte", 255, 100.0),
    ("percent", "byte", 0, 0.0),
    ("angle", "deg", 12.9716, 12.9716),
    ("angle", "microdeg", 12971600, 12.9716),
    ("angle", "e7", 129716000, 12.9716),
    ("angle", "rad", math.pi, 180.0),
])
def test_unit_conversion_with_known_values(quantity, src, raw, expected):
    assert unit(quantity, src)(raw) == pytest.approx(expected, abs=1e-9)


def test_forty_mph_in_kmh_to_four_decimals():
    assert round(unit("speed", "mph")(40), 4) == 64.3738


@pytest.mark.parametrize("src,raw,expected", [
    ("epoch_ms", 1790000000000, 1790000000000),
    ("epoch_s", 1790000000, 1790000000000),
    ("epoch_s", 1790000000.123, 1790000000123),
    ("epoch_us", 1790000000123456, 1790000000123),
    ("epoch_s", "1790000000.5", 1790000000500),
])
def test_time_units_return_integer_milliseconds(src, raw, expected):
    got = unit("time", src)(raw)
    assert got == expected
    assert type(got) is int


def test_identity_unit_still_returns_a_float():
    got = unit("speed", "kmh")(5)
    assert got == 5.0 and type(got) is float


@pytest.mark.parametrize("raw,expected", [("40", 64.37376), (" 40.0 ", 64.37376), ("4e1", 64.37376)])
def test_unit_accepts_numeric_strings(raw, expected):
    assert unit("speed", "mph")(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["fast", "", "12 km", None, [1], {"v": 1}])
def test_unit_raises_value_error_family_on_garbage(raw):
    with pytest.raises((ValueError, TypeError)):
        unit("speed", "mph")(raw)


@pytest.mark.parametrize("flag", [True, False])
def test_unit_rejects_bool(flag):
    with pytest.raises(ValueError, match="bool"):
        unit("speed", "mph")(flag)
    with pytest.raises(ValueError, match="bool"):
        unit("speed", "kmh")(flag)
    with pytest.raises(ValueError, match="bool"):
        unit("time", "epoch_s")(flag)


@pytest.mark.parametrize("quantity,src", [("speed", "furlongs"), ("mass", "kg"), (None, None), ("speed", None),
                                          ("time", "iso8601"), ("SPEED", "mph")])
def test_unknown_unit_is_rejected_at_compile_time(quantity, src):
    with pytest.raises(TransformError, match="unknown unit"):
        compile_transform({"op": "unit", "quantity": quantity, "from": src})


def test_every_unit_in_the_table_compiles_and_maps_zero_offset_correctly():
    for quantity, table in T.UNITS.items():
        for src, (factor, offset) in table.items():
            got = unit(quantity, src)(1000)
            want = 1000 * factor + offset
            assert got == pytest.approx(int(round(want)) if quantity == "time" else want)


def test_canonical_quantity_points_at_real_fields_and_quantities():
    for field, quantity in T.CANONICAL_QUANTITY.items():
        assert field in FIELD_BY_NAME
        assert quantity in T.UNITS
        assert FIELD_BY_NAME[field].unit in T.UNITS[quantity], field


# -------------------------------------------------------------- scale / affine
def test_scale():
    assert compile_transform({"op": "scale", "factor": 0.01})(18100) == pytest.approx(181.0)
    assert compile_transform({"op": "scale", "factor": 2})("21") == pytest.approx(42.0)
    assert compile_transform({"op": "scale", "factor": -1})(3) == -3


def test_affine_with_and_without_offset():
    assert compile_transform({"op": "affine", "factor": 1.8, "offset": 32})(100) == pytest.approx(212.0)
    assert compile_transform({"op": "affine", "factor": 0.5})(10) == pytest.approx(5.0)


@pytest.mark.parametrize("spec", [
    {"op": "scale"}, {"op": "scale", "factor": None}, {"op": "scale", "factor": "2"},
    {"op": "scale", "factor": True}, {"op": "scale", "factor": math.nan}, {"op": "scale", "factor": math.inf},
    {"op": "scale", "factor": [2]},
    {"op": "affine"}, {"op": "affine", "factor": 1, "offset": "3"}, {"op": "affine", "factor": 1, "offset": math.nan},
    {"op": "affine", "factor": 1, "offset": False}, {"op": "affine", "factor": -math.inf},
])
def test_scale_and_affine_need_finite_numbers(spec):
    with pytest.raises(TransformError, match="finite"):
        compile_transform(spec)


def test_scale_rejects_bool_values_at_run_time():
    with pytest.raises(ValueError):
        compile_transform({"op": "scale", "factor": 2})(True)


# --------------------------------------------------------------------- iso8601
def _ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))


def test_iso8601_with_z():
    got = compile_transform("iso8601")("2026-09-25T10:00:00Z")
    assert got == _ms(datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC))
    assert type(got) is int


def test_iso8601_with_milliseconds():
    got = compile_transform("iso8601")("2026-09-25T10:00:00.123Z")
    assert got == _ms(datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)) + 123


@pytest.mark.parametrize("text,offset_min", [("2026-09-25T15:30:00+05:30", 330), ("2026-09-25T03:00:00-07:00", -420),
                                             ("2026-09-25T10:00:00+00:00", 0)])
def test_iso8601_with_offsets(text, offset_min):
    tz = timezone(timedelta(minutes=offset_min))
    naive = datetime.fromisoformat(text[:19])
    assert compile_transform("iso8601")(text) == _ms(naive.replace(tzinfo=tz))


def test_iso8601_offset_and_z_describe_the_same_instant():
    iso = compile_transform("iso8601")
    assert iso("2026-09-25T15:30:00+05:30") == iso("2026-09-25T10:00:00Z")


def test_iso8601_without_zone_is_read_as_utc():
    iso = compile_transform("iso8601")
    assert iso("2026-09-25T10:00:00") == iso("2026-09-25T10:00:00Z")


def test_iso8601_accepts_space_separator_and_surrounding_blanks():
    iso = compile_transform("iso8601")
    assert iso("  2026-09-25 10:00:00Z ") == iso("2026-09-25T10:00:00Z")


def test_iso8601_known_epoch():
    assert compile_transform("iso8601")("1970-01-01T00:00:01Z") == 1000
    assert compile_transform({"op": "iso8601"})("2000-01-01T00:00:00Z") == 946684800000


@pytest.mark.parametrize("bad", ["yesterday", "", "2026-13-45T00:00:00Z", 1790000000, None])
def test_iso8601_raises_on_garbage(bad):
    with pytest.raises(ValueError):
        compile_transform("iso8601")(bad)


# ------------------------------------------------------------ type conversions
@pytest.mark.parametrize("raw,expected", [(3, 3), (3.4, 3), (3.6, 4), ("17", 17), ("17.6", 18), (" 5 ", 5), (-2.6, -3),
                                          ("1e3", 1000)])
def test_to_int(raw, expected):
    got = compile_transform("to_int")(raw)
    assert got == expected and type(got) is int


@pytest.mark.parametrize("raw", ["x", "", None, True, "nan", "inf"])
def test_to_int_raises_on_values_without_an_integer(raw):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        compile_transform("to_int")(raw)


@pytest.mark.parametrize("raw,expected", [(3, 3.0), ("3.25", 3.25), (" -1e-3 ", -0.001), (2.5, 2.5)])
def test_to_float(raw, expected):
    got = compile_transform("to_float")(raw)
    assert got == expected and type(got) is float


def test_to_float_rejects_bool_and_text():
    with pytest.raises(ValueError):
        compile_transform("to_float")(False)
    with pytest.raises(ValueError):
        compile_transform("to_float")("fast")


@pytest.mark.parametrize("raw,expected", [(12, "12"), (1.5, "1.5"), ("x", "x"), (True, "True"), (None, "None")])
def test_to_str(raw, expected):
    assert compile_transform("to_str")(raw) == expected


@pytest.mark.parametrize("raw", [True, 1, 1.0, "1", "true", "TRUE", " True ", "t", "yes", "Y", "on", "ON", "run",
                                 "RUNNING", "2", 5, -1])
def test_to_bool_truthy(raw):
    assert compile_transform("to_bool")(raw) is True


@pytest.mark.parametrize("raw", [False, 0, 0.0, "0", "false", "F", "no", "n", "off", "OFF", "stop", "Stopped", "",
                                 "  ", "0.0"])
def test_to_bool_falsy(raw):
    assert compile_transform("to_bool")(raw) is False


@pytest.mark.parametrize("raw", ["maybe", "enabled", "ja", None])
def test_to_bool_raises_on_unknown_words(raw):
    with pytest.raises(ValueError):
        compile_transform("to_bool")(raw)


def test_upper_and_strip():
    assert compile_transform("upper")("harsh_brake") == "HARSH_BRAKE"
    assert compile_transform("upper")(12) == "12"
    assert compile_transform("strip")("  1HGCM82633A004352\n") == "1HGCM82633A004352"
    assert compile_transform("strip")(7) == "7"


# --------------------------------------------------------------- split / index
def test_split_defaults_to_comma():
    assert compile_transform("split")("a,b,,c") == ["a", "b", "", "c"]


def test_split_with_custom_separator():
    assert compile_transform({"op": "split", "sep": "::"})("P0301::U0100") == ["P0301", "U0100"]


@pytest.mark.parametrize("raw", [None, ""])
def test_split_of_nothing_is_an_empty_list(raw):
    assert compile_transform("split")(raw) == []


def test_split_stringifies_numbers():
    assert compile_transform({"op": "split", "sep": "."})(12.5) == ["12", "5"]


@pytest.mark.parametrize("sep", ["", "12345", 1, None, [","]])
def test_split_separator_must_be_one_to_four_chars(sep):
    with pytest.raises(TransformError, match="split.sep"):
        compile_transform({"op": "split", "sep": sep})


@pytest.mark.parametrize("i,expected", [(0, "a"), (2, "c"), (-1, "c"), (-3, "a")])
def test_index(i, expected):
    assert compile_transform({"op": "index", "i": i})(["a", "b", "c"]) == expected


def test_index_out_of_range_fails_at_run_time():
    with pytest.raises(IndexError):
        compile_transform({"op": "index", "i": 5})(["a"])


@pytest.mark.parametrize("i", [None, "1", 1.0, True, 65, -65, [0]])
def test_index_must_be_a_small_integer(i):
    with pytest.raises(TransformError, match="index.i"):
        compile_transform({"op": "index", "i": i})


@pytest.mark.parametrize("i", [64, -64])
def test_index_bounds_are_inclusive(i):
    assert callable(compile_transform({"op": "index", "i": i}))


def test_split_then_index_compose():
    split, first = compile_transform({"op": "split", "sep": "|"}), compile_transform({"op": "index", "i": 1})
    assert first(split("x|y|z")) == "y"


# ------------------------------------------------------------ dtc / enum / round
def test_dtc_extract_is_the_domain_function():
    assert compile_transform("dtc_extract")("p0301; U0100") == ["P0301", "U0100"]
    assert compile_transform("dtc_extract")([]) == []


def test_enum_maps_known_values_and_defaults_to_none():
    fn = compile_transform({"op": "enum", "map": {"HB": "HARSH_BRAKE", "1": "IDLE"}})
    assert fn("HB") == "HARSH_BRAKE"
    assert fn(1) == "IDLE", "the looked-up value is stringified first"
    assert fn("??") is None


def test_enum_with_default():
    fn = compile_transform({"op": "enum", "map": {"a": "IDLE"}, "default": "SPEEDING"})
    assert fn("zzz") == "SPEEDING"


def test_enum_is_case_sensitive():
    assert compile_transform({"op": "enum", "map": {"hb": "HARSH_BRAKE"}})("HB") is None


def test_enum_accepts_an_empty_map_and_256_entries():
    assert compile_transform({"op": "enum", "map": {}})("x") is None
    big = {str(i): i for i in range(256)}
    assert compile_transform({"op": "enum", "map": big})("255") == 255


@pytest.mark.parametrize("mapping", [None, [("a", "b")], "a=b", 5, {str(i): i for i in range(257)}])
def test_enum_map_must_be_a_bounded_object(mapping):
    with pytest.raises(TransformError, match="enum.map"):
        compile_transform({"op": "enum", "map": mapping})


@pytest.mark.parametrize("digits,raw,expected", [(0, 2.6, 3.0), (1, 27.46, 27.5), (3, "1.23456", 1.235), (9, 0.1, 0.1)])
def test_round(digits, raw, expected):
    assert compile_transform({"op": "round", "digits": digits})(raw) == pytest.approx(expected)


def test_round_defaults_to_zero_digits():
    assert compile_transform("round")(41.7) == 42


@pytest.mark.parametrize("digits", [-1, 10, 1.0, "2", True, None])
def test_round_digits_must_be_zero_to_nine(digits):
    with pytest.raises(TransformError, match="round.digits"):
        compile_transform({"op": "round", "digits": digits})


# ------------------------------------------------------------------- guardrail
@pytest.mark.parametrize("op", ["eval", "exec", "python", "lambda", "", "UNIT", "unit ", "__import__", None, 5])
def test_unknown_op_is_rejected(op):
    with pytest.raises(TransformError, match="unknown transform op"):
        compile_transform({"op": op})


def test_unknown_op_as_a_plain_name_is_rejected():
    with pytest.raises(TransformError, match="unknown transform op"):
        compile_transform("os.system")


@pytest.mark.parametrize("spec", [None, 5, 1.5, [], ["upper"], ("upper",), {}, {"name": "upper"}, b"upper"])
def test_spec_must_be_a_name_or_an_object_with_op(spec):
    with pytest.raises(TransformError, match="must be a name or an object"):
        compile_transform(spec)


@pytest.mark.parametrize("spec,param", [
    ({"op": "upper", "locale": "de"}, "locale"),
    ({"op": "iso8601", "format": "%Y"}, "format"),
    ({"op": "to_int", "base": 16}, "base"),
    ({"op": "unit", "quantity": "speed", "from": "mph", "to": "kmh"}, "to"),
    ({"op": "scale", "factor": 2, "offset": 1}, "offset"),
    ({"op": "split", "sep": ",", "maxsplit": 1}, "maxsplit"),
    ({"op": "index", "i": 0, "default": None}, "default"),
    ({"op": "enum", "map": {}, "code": "import os"}, "code"),
    ({"op": "round", "digits": 1, "mode": "up"}, "mode"),
    ({"op": "dtc_extract", "pattern": ".*"}, "pattern"),
])
def test_unexpected_parameters_are_rejected(spec, param):
    with pytest.raises(TransformError, match="does not accept") as info:
        compile_transform(spec)
    assert param in str(info.value)


@pytest.mark.parametrize("op", [["unit"], {"name": "unit"}])
def test_unhashable_op_is_rejected_with_transform_error(op):
    with pytest.raises(TransformError):
        compile_transform({"op": op})


@pytest.mark.parametrize("spec", [{"op": "unit", "quantity": ["speed"], "from": "mph"},
                                  {"op": "unit", "quantity": "speed", "from": ["mph"]}])
def test_unhashable_unit_parameters_are_rejected_with_transform_error(spec):
    with pytest.raises(TransformError):
        compile_transform(spec)


def test_transform_error_is_a_value_error():
    assert issubclass(TransformError, ValueError)


def test_allowed_ops_lists_every_op_once_sorted():
    assert list(T.ALLOWED_OPS) == sorted(set(T.ALLOWED_OPS))
    assert set(T.ALLOWED_OPS) == {"unit", "scale", "affine", "iso8601", "to_int", "to_float", "to_str", "to_bool",
                                  "upper", "strip", "split", "index", "dtc_extract", "enum", "round"}


@pytest.mark.parametrize("op", ["iso8601", "to_int", "to_float", "to_str", "to_bool", "upper", "strip", "split",
                                "dtc_extract", "round"])
def test_every_parameterless_op_compiles_from_its_name(op):
    assert callable(compile_transform(op))
    assert callable(compile_transform({"op": op}))
