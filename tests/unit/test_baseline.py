"""rosetta.ml.baseline: mapping fields by the look of their names."""
from __future__ import annotations

import pytest

from rosetta.ml import baseline as B
from rosetta.ml.labels import IGNORE, LABEL_INDEX


def test_score_covers_every_alias_group():
    s = B.score("speed")
    assert set(s) == set(B.ALIASES)
    assert all(0.0 <= v <= 1.0 for v in s.values())


def test_exact_alias_scores_one():
    for field, aliases in B.ALIASES.items():
        for alias in aliases:
            assert B.score(alias)[field] == 1.0, alias


def test_score_of_an_empty_name_is_zero_everywhere():
    assert set(B.score("").values()) == {0.0}
    assert B.predict("") == IGNORE


def test_score_uses_the_last_part_of_a_nested_path():
    assert B.score("position.latitude")["lat"] == 1.0
    assert B.score("a.b.c.longitude")["lon"] == 1.0
    assert B.score("vehicle.vin")["vin"] == 1.0


def test_score_ignores_case_and_naming_style():
    assert B.score("Latitude") == B.score("latitude") == B.score("LATITUDE")
    assert B.score("fuelLevel")["fuel"] == B.score("fuel_level")["fuel"] == 1.0


@pytest.mark.parametrize("path,label", [
    ("vin", "vin"), ("vehicle.vin", "vin"),
    ("timestamp", "ts|epoch_ms"), ("ts", "ts|epoch_ms"),
    ("sequence", "seq"), ("counter", "seq"),
    ("latitude", "lat|deg"), ("position.lat", "lat|deg"),
    ("lng", "lon|deg"), ("position.longitude", "lon|deg"),
    ("heading", "heading|deg"), ("gnss_course", "heading|deg"),
    ("speed", "speed|kmh"), ("velocity", "speed|kmh"),
    ("odometer", "odo|km"), ("mileage", "odo|km"),
    ("battery", "soc|pct"), ("soc", "soc|pct"),
    ("fuel_level", "fuel|pct"), ("fuel", "fuel|pct"),
    ("temperature", "ambient|c"), ("ambient", "ambient|c"),
    ("ignition", "ignition"), ("engine_on", "ignition"),
    ("dtc", "dtc"), ("trouble_codes", "dtc"), ("diagnostics.troubleCodes", "dtc"),
    ("event", "evt"), ("evt", "evt"),
])
def test_predict_maps_plain_names(path, label):
    assert B.predict(path) == label


@pytest.mark.parametrize("path,label", [
    ("odometer_mi", "odo|mi"), ("odometer_km", "odo|km"), ("odo_m", "odo|m"), ("mileage_mi", "odo|mi"),
    ("velocity_mph", "speed|mph"), ("velocity_mps", "speed|mps"), ("speed_kn", "speed|knots"),
    ("speed_ms", "speed|mps"), ("fuel_pm", "fuel|permille"), ("ambient_f", "ambient|f"), ("ambient_k", "ambient|k"),
])
def test_predict_reads_the_unit_from_the_suffix(path, label):
    assert B.predict(path) == label


@pytest.mark.parametrize("path", ["rssi", "firmware", "hdop", "satellites", "kopf.fin", "fahrt.strecke", "c7", "x",
                                  "schema", "door_open"])
def test_predict_ignores_names_it_does_not_recognise(path):
    assert B.predict(path) == IGNORE


def test_predict_never_looks_at_values():
    # Same name, so the same answer, whatever the field contains: that is the baseline's weakness.
    assert B.predict("speed") == "speed|kmh"
    assert B.predict("motion.speed") == "speed|kmh"


def test_every_prediction_is_a_known_label():
    names = [a for aliases in B.ALIASES.values() for a in aliases]
    names += [f"{a}_{u}" for f, units in B.SUFFIX_UNIT.items() for u in units for a in B.ALIASES[f]]
    names += ["rssi", "x.y.z", "", "lat_e7", "soc_pm"]
    for name in names:
        assert B.predict(name) in LABEL_INDEX, name


def test_default_units_and_suffix_units_name_real_labels():
    for field, unit in B.DEFAULT_UNIT.items():
        assert f"{field}|{unit}" in LABEL_INDEX
    for field, units in B.SUFFIX_UNIT.items():
        for unit in units.values():
            assert f"{field}|{unit}" in LABEL_INDEX


def test_threshold_is_applied():
    assert B.score("bat")["soc"] == pytest.approx(0.6)                 # "bat" against "battery"
    assert max(B.score("bat").values()) < B.THRESHOLD
    assert B.predict("bat") == IGNORE
    assert B.score("batt")["soc"] == pytest.approx(8 / 11)             # just above 0.72
    assert B.predict("batt") == "soc|pct"


@pytest.mark.parametrize("path,label", [
    ("speed_mph", "speed|mph"), ("speed_kmh", "speed|kmh"), ("speed_mps", "speed|mps"),
    ("speed_knots", "speed|knots"), ("lat_e7", "lat|e7"), ("lon_e7", "lon|e7"), ("soc_pm", "soc|permille"),
    ("vehicle_speed_mph", "speed|mph"), ("mileage_miles", "odo|mi"),
])
def test_predict_reads_the_unit_from_a_longer_suffix(path, label):
    assert B.predict(path) == label
