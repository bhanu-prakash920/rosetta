"""rosetta.ml.profile: features of a source field, from its name, its values and its physics."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from rosetta.domain.vin import make_vin
from rosetta.ml import profile as P

T0_S = 1_790_000_000


def drive(devices: int = 6, ticks: int = 30, speed_unit: float = 1.609344, time_scale: int = 1,
          coord_scale: float = 1.0, lat0: float = 12.0) -> tuple[list[dict[str, Any]], list[str]]:
    """Vehicles on straight lines at constant speed. Speed and odometer are sent in miles."""
    msgs, devs = [], []
    for k in range(devices):
        v = 30.0 + 10.0 * k                                   # km/h
        h = (20.0 + 65.0 * k) % 360.0
        lat, lon, odo = lat0 + 0.1 * k, 77.0 + 0.1 * k, 1000.0 * (k + 1)
        for i in range(ticks):
            d = v / 3600.0
            lat += d * math.cos(math.radians(h)) / 110.574
            lon += d * math.sin(math.radians(h)) / (111.320 * math.cos(math.radians(lat)))
            odo += d
            la, lo = round(lat, 6) * coord_scale, round(lon, 6) * coord_scale
            msgs.append({
                "hdr": {"vin": make_vin("7NV", k), "t": (T0_S + i) * time_scale, "n": 500 + i},
                "gps": {"la": la if coord_scale == 1.0 else int(round(la)),
                        "lo": lo if coord_scale == 1.0 else int(round(lo)), "hdg": h},
                "v_mph": round(v / speed_unit, 3), "odo_mi": round(odo / speed_unit, 4),
                "codes": "P0301,U0100", "on": True, "fw": "1.2.3",
            })
            devs.append(f"d{k}")
    return msgs, devs


def features(profile: P.FieldProfile) -> dict[str, float]:
    return dict(zip(P.FEATURE_NAMES, (float(x) for x in profile.features)))


@pytest.fixture(scope="module")
def profiles() -> dict[str, P.FieldProfile]:
    msgs, devs = drive()
    return P.build_profiles(P.make_samples(msgs, devs))


# ----------------------------------------------------------------------- names
@pytest.mark.parametrize("path,tokens", [
    ("motion.speedKmh", "motion speed kmh"),
    ("speed_mph", "speed mph"),
    ("signals[0].value", "signals value"),
    ("GNSS_LAT", "gnss lat"),
    ("kebab-name", "kebab name"),
    ("odometerKm", "odometer km"),
    ("lat_e7", "lat e7"),
    ("a.b[12].cD", "a b c d"),
    ("", ""),
    ("__", ""),
])
def test_name_tokens(path, tokens):
    assert P.name_tokens(path) == tokens


def test_name_vector_is_a_unit_vector_of_fixed_size():
    v = P.name_vector("motion.speedKmh")
    assert v.shape == (P.NAME_DIM,) and v.dtype == np.float32
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-5)
    assert (v >= 0).all()


def test_name_vector_ignores_case_and_separators():
    assert np.array_equal(P.name_vector("speed_kmh"), P.name_vector("speedKmh"))
    assert np.array_equal(P.name_vector("Speed.KMH"), P.name_vector("speed kmh"))


def test_similar_names_are_closer_than_unrelated_ones():
    speed = P.name_vector("vehicle_speed")
    assert float(speed @ P.name_vector("veh_speed")) > float(speed @ P.name_vector("fuel_level"))
    assert float(speed @ P.name_vector("odometer")) < 0.5


def test_name_vector_of_an_empty_name_is_zero():
    assert not P.name_vector("").any()


# ---------------------------------------------------------------------- values
@pytest.mark.parametrize("value,expected", [
    (5, 5.0), (2.5, 2.5), ("12", 12.0), (" 1e3 ", 1000.0), ("-0.5", -0.5),
    (True, None), (False, None), (None, None), ("", None), ("abc", None), ("nan", None), ("inf", None),
    (float("nan"), None), (float("inf"), None), ([1], None), ({"a": 1}, None), ("1" * 25, None),
])
def test_as_float(value, expected):
    assert P._as_float(value) == expected


def test_feature_names_are_unique_and_complete():
    assert len(set(P.FEATURE_NAMES)) == len(P.FEATURE_NAMES) == P.N_DENSE
    assert P.FEATURE_NAMES == P.VALUE_FEATURES + P.PHYSICS_FEATURES


def test_one_profile_per_flattened_path(profiles):
    assert list(profiles) == ["hdr.vin", "hdr.t", "hdr.n", "gps.la", "gps.lo", "gps.hdg", "v_mph", "odo_mi",
                              "codes", "on", "fw"]
    for path, prof in profiles.items():
        assert prof.path == path
        assert len(prof.values) == len(prof.numeric) == 180
        assert prof.features.shape == (P.N_DENSE,) and prof.features.dtype == np.float32
        assert np.isfinite(prof.features).all()


def test_type_mix(profiles):
    assert features(profiles["hdr.vin"])["frac_str"] == 1.0
    assert features(profiles["hdr.t"])["frac_num"] == 1.0
    assert features(profiles["on"])["frac_bool"] == 1.0
    assert features(profiles["on"])["frac_num"] == 0.0, "a bool is not a number"
    assert np.isnan(profiles["on"].numeric).all()


def test_vin_and_trouble_codes_are_recognised(profiles):
    assert features(profiles["hdr.vin"])["frac_vin"] == 1.0
    assert features(profiles["codes"])["frac_dtc"] == 1.0
    assert features(profiles["fw"])["frac_vin"] == 0.0 and features(profiles["fw"])["frac_dtc"] == 0.0


def test_clock_features(profiles):
    f = features(profiles["hdr.t"])
    assert f["epoch_s_like"] == 1.0 and f["epoch_ms_like"] == 0.0 and f["epoch_us_like"] == 0.0
    assert f["mono_up"] == 1.0 and f["mono_strict"] == 1.0 and f["step_const"] == 1.0


@pytest.mark.parametrize("scale,feature", [(1, "epoch_s_like"), (1000, "epoch_ms_like"), (10 ** 6, "epoch_us_like")])
def test_epoch_resolution_is_recognised(scale, feature):
    msgs, devs = drive(devices=3, ticks=10, time_scale=scale)
    f = features(P.build_profiles(P.make_samples(msgs, devs))["hdr.t"])
    assert f[feature] == 1.0
    assert sum(f[k] for k in ("epoch_s_like", "epoch_ms_like", "epoch_us_like")) == 1.0


def test_range_features(profiles):
    lat, hdg, odo = features(profiles["gps.la"]), features(profiles["gps.hdg"]), features(profiles["odo_mi"])
    assert lat["in_m90_90"] == 1.0 and lat["in_0_1"] == 0.0 and lat["sign_min"] == 0.0
    assert hdg["in_0_360"] == 1.0 and hdg["in_0_100"] == pytest.approx(1 / 3)
    assert odo["in_0_1000"] == pytest.approx(1 / 6)
    assert hdg["frac_integer"] == 1.0 and lat["frac_integer"] == 0.0


def test_counter_features(profiles):
    f = features(profiles["hdr.n"])
    assert f["mono_strict"] == 1.0 and f["step_const"] == 1.0 and f["frac_integer"] == 1.0
    assert f["count_step_fit"] == 1.0
    assert features(profiles["v_mph"])["count_step_fit"] == 0.0


def test_constant_field(profiles):
    f = features(profiles["fw"])
    assert f["two_valued"] == 1.0 and f["distinct_ratio"] == pytest.approx(1 / 180)
    assert f["str_len"] == pytest.approx(5 / 40)


def test_examples_are_distinct_and_few(profiles):
    assert profiles["fw"].examples == ["1.2.3"]
    assert profiles["on"].examples == [True]
    assert len(profiles["gps.la"].examples) == 5
    assert len(set(profiles["hdr.vin"].examples)) == len(profiles["hdr.vin"].examples) <= 5


def test_value_features_of_mixed_and_missing_values():
    msgs = [{"x": 1}, {"x": "2"}, {"x": None}, {}, {"x": "abc"}, {"x": ""}, {"x": [1, 2]}, {"x": False}]
    prof = P.build_profiles(P.make_samples(msgs, [f"d{i}" for i in range(8)]))["x"]
    f = features(prof)
    assert f["frac_null"] == pytest.approx(2 / 8)
    assert f["frac_num"] == pytest.approx(1 / 6) and f["frac_str"] == pytest.approx(3 / 6)
    assert f["frac_numstr"] == pytest.approx(1 / 6) and f["frac_list"] == pytest.approx(1 / 6)
    assert f["frac_bool"] == pytest.approx(1 / 6) and f["frac_empty"] == pytest.approx(1 / 6)
    assert f["list_len"] == 2.0
    assert np.isnan(prof.numeric).sum() == 6
    assert prof.examples == [1, "2", "abc", [1, 2], False]


def test_field_that_is_always_absent_in_some_messages():
    msgs = [{"a": 1, "b": 2}, {"a": 2}, {"a": 3}, {"a": 4, "b": 5}]
    profs = P.build_profiles(P.make_samples(msgs, ["d"] * 4))
    assert features(profs["b"])["frac_null"] == 0.5
    assert profs["b"].values == [2, None, None, 5]


def test_values_of_all_nulls():
    f = P._value_features([None, None], np.array([np.nan, np.nan]), [])
    assert f["frac_null"] == 1.0
    assert all(v == 0.0 for k, v in f.items() if k != "frac_null")
    assert P._value_features([], np.zeros(0), [])["frac_null"] == 1.0


def test_string_shape_features():
    msgs = [{"s": "2026-09-25T10:00:00Z"}, {"s": "2026-09-25 10:00:01"}, {"s": "ON"}, {"s": "  "}]
    f = features(P.build_profiles(P.make_samples(msgs, ["d"] * 4))["s"])
    assert f["frac_iso"] == 0.5 and f["frac_upper"] == 0.5 and f["frac_empty"] == 0.25


def test_decimals_feature():
    def decimals(values):
        msgs = [{"x": v} for v in values]
        return features(P.build_profiles(P.make_samples(msgs, ["d"] * len(msgs)))["x"])["decimals"]

    assert decimals([1, 2, 3]) == 0.0
    assert decimals([1.5, 2.5, 3.1]) == pytest.approx(1 / 8)
    assert decimals([1.25, 2.75, 3.11]) == pytest.approx(2 / 8)
    assert decimals([12.971601, 12.971644, 12.971689]) == pytest.approx(6 / 8)


def test_build_profiles_of_nothing():
    assert P.build_profiles(P.make_samples([], [])) == {}


# --------------------------------------------------------------------- samples
def test_samples_group_messages_by_device_in_arrival_order():
    s = P.Samples([{}] * 6, ["a", "b", "a", "c", "b", "a"])
    assert [g.tolist() for g in s.groups()] == [[0, 2, 5], [1, 4]], "a device with one message has no sequence"


def test_make_samples_flattens_each_message():
    s = P.make_samples([{"a": {"b": 1}}, {"a": {"c": 2}}], ["d1", "d2"])
    assert s.flat == [{"a.b": 1}, {"a.c": 2}] and s.device == ["d1", "d2"]


# --------------------------------------------------------------------- physics
def test_physics_finds_clock_position_and_heading():
    msgs, devs = drive()
    samples = P.make_samples(msgs, devs)
    info = P.add_physics(P.build_profiles(samples), samples)
    assert info["devices"] == 6
    assert info["clock"] == "hdr.t"
    assert (info["lat"], info["lat_unit"], info["lon"], info["lon_unit"]) == ("gps.la", "deg", "gps.lo", "deg")
    assert info["heading"] == "gps.hdg" and info["heading_fit"] == pytest.approx(1.0, abs=0.01)


def test_physics_marks_latitude_longitude_and_heading(profiles):
    assert features(profiles["gps.la"])["coord_lat_score"] == pytest.approx(1.0, abs=0.01)
    assert features(profiles["gps.lo"])["coord_lon_score"] == pytest.approx(1.0, abs=0.01)
    assert features(profiles["gps.la"])["coord_lon_score"] == 0.0
    assert features(profiles["gps.hdg"])["heading_fit"] == pytest.approx(1.0, abs=0.01)
    assert features(profiles["hdr.t"])["clock_fit"] == 1.0
    others = [p for p in profiles if p not in ("gps.la", "gps.lo")]
    assert all(features(profiles[p])["coord_lat_score"] == 0.0 for p in others)


@pytest.mark.parametrize("unit,per_kmh", [(1.0, 1.0), (1.609344, 1 / 1.609344), (1.852, 1 / 1.852), (3.6, 1 / 3.6)],
                         ids=["kmh", "mph", "knots", "mps"])
def test_physics_reads_the_speed_unit_from_the_movement(unit, per_kmh):
    msgs, devs = drive(speed_unit=unit)
    f = features(P.build_profiles(P.make_samples(msgs, devs))["v_mph"])
    assert f["speed_ratio_log"] * 4.0 == pytest.approx(math.log10(per_kmh), abs=0.01)
    assert f["speed_ratio_fit"] > 0.97


def test_physics_reads_the_distance_unit_from_the_movement(profiles):
    f = features(profiles["odo_mi"])
    assert f["dist_ratio_log"] * 4.0 == pytest.approx(math.log10(1 / 1.609344), abs=0.01)
    assert f["dist_ratio_fit"] > 0.97
    assert features(profiles["v_mph"])["dist_ratio_fit"] == 0.0, "a constant speed does not grow with distance"


@pytest.mark.parametrize("scale,unit,lat0", [(1e6, "microdeg", 12.0), (1e6, "microdeg", 52.0), (1e7, "e7", 28.0)])
def test_physics_recognises_scaled_integer_coordinates(scale, unit, lat0):
    msgs, devs = drive(coord_scale=scale, lat0=lat0)
    samples = P.make_samples(msgs, devs)
    info = P.add_physics(P.build_profiles(samples), samples)
    assert (info["lat"], info["lat_unit"], info["lon"], info["lon_unit"]) == ("gps.la", unit, "gps.lo", unit)


def test_physics_recognises_e7_coordinates_near_the_equator():
    msgs, devs = drive(coord_scale=1e7, lat0=12.0)
    samples = P.make_samples(msgs, devs)
    info = P.add_physics(P.build_profiles(samples), samples)
    assert (info["lat"], info["lat_unit"], info["lon"], info["lon_unit"]) == ("gps.la", "e7", "gps.lo", "e7")


def test_physics_needs_three_messages_per_device():
    msgs, devs = drive(devices=4, ticks=2)
    samples = P.make_samples(msgs, devs)
    profs = P.build_profiles(samples)
    assert P.add_physics(profs, samples) == {"devices": 0}
    off = len(P.VALUE_FEATURES)
    assert all(not p.features[off:].any() for p in profs.values())


def test_physics_without_a_clock_stops_early():
    msgs, devs = drive(devices=3, ticks=10)
    for m in msgs:
        del m["hdr"]["t"]
    samples = P.make_samples(msgs, devs)
    info = P.add_physics(P.build_profiles(samples), samples)
    assert info["clock"] is None and "lat" not in info
    assert ("gps.la", "deg") in info["coordinate_candidates"]


def test_physics_without_coordinates_stops_early():
    msgs, devs = drive(devices=3, ticks=10)
    for m in msgs:
        del m["gps"]
    samples = P.make_samples(msgs, devs)
    info = P.add_physics(P.build_profiles(samples), samples)
    assert info["clock"] == "hdr.t" and "lat" not in info


def test_physics_reads_an_iso_clock():
    from rosetta.simulator.dialects import iso

    msgs, devs = drive(devices=4, ticks=12)
    for m in msgs:
        m["hdr"]["t"] = iso(m["hdr"]["t"] * 1000)
    samples = P.make_samples(msgs, devs)
    profs = P.build_profiles(samples)
    info = P.add_physics(profs, samples)
    assert info["clock"] == "hdr.t" and info["lat"] == "gps.la"
    assert features(profs["v_mph"])["speed_ratio_fit"] > 0.97


# ------------------------------------------------------------ summary, matrix
def test_summary(profiles):
    s = profiles["v_mph"].summary()
    assert s["path"] == "v_mph" and s["present"] == 1.0
    assert s["min"] == pytest.approx(30 / 1.609344, abs=1e-3) and s["max"] == pytest.approx(80 / 1.609344, abs=1e-3)
    assert s["min"] <= s["median"] <= s["max"]
    assert set(s["physics"]) == {"speed_ratio_log", "speed_ratio_fit"}
    assert len(s["examples"]) <= 4


def test_summary_of_a_text_field_has_no_numbers(profiles):
    s = profiles["fw"].summary()
    assert "min" not in s and s["physics"] == {} and s["examples"] == ["1.2.3"]


def test_feature_matrix(profiles):
    paths, X = P.feature_matrix(profiles)
    assert paths == list(profiles)
    assert X.shape == (11, P.N_DENSE + P.NAME_DIM) and X.dtype == np.float32
    assert np.array_equal(X[0, : P.N_DENSE], profiles["hdr.vin"].features)
    assert np.array_equal(X[0, P.N_DENSE:], P.name_vector("hdr.vin"))


def test_feature_matrix_without_names(profiles):
    paths, X = P.feature_matrix(profiles, with_names=False)
    assert X.shape == (11, P.N_DENSE)


def test_feature_matrix_of_nothing():
    paths, X = P.feature_matrix({})
    assert paths == [] and X.shape == (0, P.N_DENSE + P.NAME_DIM)
    assert P.feature_matrix({}, with_names=False)[1].shape == (0, P.N_DENSE)
