"""rosetta.ml.labels: the label space of the field mapper."""
from __future__ import annotations

import pytest

from rosetta.domain.canonical import FIELD_BY_NAME, REQUIRED
from rosetta.domain.transforms import UNITS, compile_transform
from rosetta.ml import labels as L


def test_ignore_is_the_first_label():
    assert L.LABEL_NAMES[0] == L.IGNORE == "ignore"
    assert L.LABEL_INDEX[L.IGNORE] == 0


def test_label_names_are_unique_and_indexed():
    assert len(set(L.LABEL_NAMES)) == len(L.LABEL_NAMES) == len(L.LABELS) + 1
    assert all(L.LABEL_NAMES[i] == name for name, i in L.LABEL_INDEX.items())


@pytest.mark.parametrize("label", list(L.LABELS))
def test_every_label_maps_to_a_canonical_field(label):
    assert L.field_of(label) in FIELD_BY_NAME
    assert L.CANONICAL_OF[label] == L.LABELS[label][0]


@pytest.mark.parametrize("label", list(L.LABELS))
def test_every_label_carries_transforms_from_the_whitelist(label):
    for t in L.transforms_of(label):
        assert callable(compile_transform(t))


def test_every_canonical_field_can_be_a_target():
    assert set(L.CANONICAL_TARGETS) == set(FIELD_BY_NAME)
    assert set(REQUIRED) <= set(L.CANONICAL_TARGETS)
    assert len(L.CANONICAL_TARGETS) == len(set(L.CANONICAL_TARGETS))


@pytest.mark.parametrize("label,field,unit", [
    ("vin", "vin", ""), ("seq", "seq", ""), ("ts|iso8601", "ts", "iso8601"), ("lat|e7", "lat", "e7"),
    ("heading|centideg", "heading_deg", "centideg"), ("speed|mph", "speed_kmh", "mph"), ("odo|hm", "odo_km", "hm"),
    ("soc|fraction", "soc_pct", "fraction"), ("fuel|permille", "fuel_pct", "permille"),
    ("ambient|deci_c", "ambient_c", "deci_c"), ("dtc", "dtc", ""), ("evt", "evt", ""),
])
def test_field_and_unit_of_a_label(label, field, unit):
    assert L.field_of(label) == field
    assert L.unit_of(label) == unit


@pytest.mark.parametrize("label", ["ignore", "nonsense", "", "speed|warp"])
def test_field_of_an_unknown_label_is_ignore(label):
    assert L.field_of(label) == L.IGNORE


def test_transforms_of_an_unknown_label():
    with pytest.raises(KeyError):
        L.transforms_of("ignore")


def test_transforms_of_returns_copies():
    first = L.transforms_of("speed|mph")
    first[0]["from"] = "kmh"
    first.append("round")
    assert L.transforms_of("speed|mph") == [{"op": "unit", "quantity": "speed", "from": "mph"}]


@pytest.mark.parametrize("label,raw,expected", [
    ("speed|mph", 40, 64.37376), ("speed|mps", 10, 36.0), ("speed|knots", 10, 18.52), ("speed|centi_kmh", 5423, 54.23),
    ("odo|mi", 100, 160.9344), ("odo|m", 18234500, 18234.5), ("odo|hm", 182345, 18234.5),
    ("soc|fraction", 0.765, 76.5), ("fuel|permille", 400, 40.0),
    ("ambient|f", 98.6, 37.0), ("ambient|k", 273.15, 0.0), ("ambient|deci_c", -35, -3.5),
    ("lat|microdeg", 12971600, 12.9716), ("lon|e7", -775946000, -77.5946), ("heading|centideg", 18150, 181.5),
    ("ts|epoch_s", 1790000000, 1790000000000), ("ts|epoch_us", 1790000000123456, 1790000000123),
    ("ts|iso8601", "2026-09-21T14:13:20Z", 1790000000000),
    ("vin", " 1hgcm82633a004352 ", "1HGCM82633A004352"), ("seq", "41", 41), ("ignition", "ON", True),
    ("dtc", "p0301;u0100", ["P0301", "U0100"]), ("lat|deg", "12.5", 12.5), ("evt", "IDLE", "IDLE"),
])
def test_label_transforms_produce_canonical_values(label, raw, expected):
    value = raw
    for t in L.transforms_of(label):
        value = compile_transform(t)(value)
    assert value == pytest.approx(expected) if isinstance(expected, float) else value == expected


def test_unit_labels_use_units_the_engine_knows():
    for label, (_field, transforms) in L.LABELS.items():
        for t in transforms:
            if isinstance(t, dict) and t["op"] == "unit":
                assert t["from"] in UNITS[t["quantity"]], label
                assert L.unit_of(label) == t["from"]
