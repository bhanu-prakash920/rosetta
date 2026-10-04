"""rosetta.simulator.fleet: the vectorised fleet model."""
from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from rosetta.domain.canonical import EVENT_TYPES, validate
from rosetta.domain.dtc import is_valid_dtc
from rosetta.domain.vin import is_valid_vin
from rosetta.simulator import fleet as F
from rosetta.simulator.fleet import BEV, CHARGING, DRIVING, ICE, OEMS, PARKED, PHEV, Fleet

T0 = 1_790_000_000_000
STATE_ARRAYS = ("oem", "powertrain", "city", "lat", "lon", "heading", "state", "speed", "cruise", "odo", "soc",
                "fuel", "temp", "seq", "evt", "heartbeat", "fleet_id")


def run(fleet: Fleet, steps: int, dt: float = 1.0, **kw) -> None:
    for k in range(steps):
        fleet.step(dt, T0 + int((k + 1) * dt * 1000), **kw)


def same_state(a: Fleet, b: Fleet) -> bool:
    return all(np.array_equal(getattr(a, name), getattr(b, name), equal_nan=True) for name in STATE_ARRAYS) \
        and a.vin == b.vin and a.device_id == b.device_id and a.dtc == b.dtc


# ---------------------------------------------------------------- construction
def test_same_seed_gives_the_same_fleet():
    assert same_state(Fleet(500, seed=3), Fleet(500, seed=3))


def test_same_seed_gives_the_same_history():
    a, b = Fleet(500, seed=3), Fleet(500, seed=3)
    run(a, 40)
    run(b, 40)
    assert same_state(a, b)
    idx = np.arange(500)
    assert a.truth(idx) == b.truth(idx)


def test_different_seeds_give_different_fleets():
    a, b = Fleet(500, seed=3), Fleet(500, seed=4)
    assert not np.array_equal(a.lat, b.lat)
    assert not np.array_equal(a.oem, b.oem)


def test_every_vin_is_unique_and_valid():
    f = Fleet(3000, seed=7)
    assert len(f.vin) == 3000
    assert len(set(f.vin)) == 3000
    assert all(is_valid_vin(v) for v in f.vin)


def test_vin_prefix_matches_the_maker():
    f = Fleet(1000, seed=7)
    for i in range(1000):
        assert f.vin[i].startswith(OEMS[f.oem[i]].wmi)
        assert f.vin[i][3:8] == ("EV1A2" if f.powertrain[i] == BEV else "GT4B7")


def test_device_ids_are_unique_and_carry_the_maker_prefix():
    f = Fleet(2000, seed=7)
    assert len(set(f.device_id)) == 2000
    for i in range(2000):
        assert f.device_id[i].startswith(OEMS[f.oem[i]].key[:2] + "-")
        assert len(f.device_id[i]) == 10


def test_arrays_have_one_entry_per_vehicle():
    f = Fleet(321, seed=1)
    assert f.n == 321
    assert all(len(getattr(f, name)) == 321 for name in STATE_ARRAYS)


def test_fleet_shares_follow_the_maker_profiles():
    f = Fleet(3000, seed=7)
    counts = Counter(f.oem.tolist())
    for i, profile in enumerate(OEMS):
        assert counts[i] / 3000 == pytest.approx(profile.share, abs=0.04), profile.key


def test_powertrain_mix_follows_the_maker_profiles():
    f = Fleet(3000, seed=7)
    voltaic = f.oem == F.OEM_INDEX["voltaic"]
    assert (f.powertrain[voltaic] == BEV).all(), "voltaic builds only battery vehicles"
    nordvik = f.oem == F.OEM_INDEX["nordvik"]
    assert (f.powertrain[nordvik] == BEV).mean() == pytest.approx(0.30, abs=0.06)
    assert {ICE, BEV, PHEV} == set(f.powertrain.tolist())


def test_energy_gauges_match_the_powertrain():
    f = Fleet(2000, seed=7)
    assert np.isnan(f.soc[f.powertrain == ICE]).all()
    assert np.isnan(f.fuel[f.powertrain == BEV]).all()
    assert not np.isnan(f.soc[f.powertrain != ICE]).any()
    assert not np.isnan(f.fuel[f.powertrain != BEV]).any()
    phev = f.powertrain == PHEV
    assert not np.isnan(f.soc[phev]).any() and not np.isnan(f.fuel[phev]).any()


def test_initial_state_is_plausible():
    f = Fleet(2000, seed=7)
    assert set(f.state.tolist()) <= {PARKED, DRIVING}
    assert (f.speed[f.state == PARKED] == 0).all()
    assert (f.speed[f.state == DRIVING] >= 15).all()
    assert ((f.lat > 5) & (f.lat < 35)).all() and ((f.lon > 65) & (f.lon < 95)).all(), "Indian cities"
    assert (f.seq >= 1).all()
    assert (f.evt == 0).all()
    assert f.now_ms == 0


def test_start_time_and_custom_cities():
    berlin = (("Berlin", 52.52, 13.40, 1.0, 11.0),)
    f = Fleet(200, seed=1, start_ms=T0, cities=berlin)
    assert f.now_ms == T0
    assert np.abs(f.lat - 52.52).max() < 1.0 and np.abs(f.lon - 13.40).max() < 1.0
    assert (f.city == 0).all()


def on_land(f: Fleet) -> np.ndarray:
    ok = np.ones(f.n, dtype=bool)
    for c, city in enumerate(F.CITIES):
        sel = f.city == c
        if city[0] in F.LAND:
            ok[sel] = F.on_land(F.LAND[city[0]], f.lat[sel], f.lon[sel])
    return ok


def test_land_outlines_contain_their_city_and_not_the_sea():
    for name, lat, lon, *_ in F.CITIES:
        if name in F.LAND:
            assert F.on_land(F.LAND[name], np.array([lat]), np.array([lon]))[0], name
    sea = {"Chennai": (13.08, 80.40), "Mumbai": (18.95, 72.90), "Kochi": (9.93, 76.20), "Surat": (21.10, 72.55)}
    for name, (lat, lon) in sea.items():
        assert not F.on_land(F.LAND[name], np.array([lat]), np.array([lon]))[0], name


def test_coastal_vehicles_start_on_land():
    f = Fleet(20_000, seed=7)
    assert on_land(f).all()
    assert F.on_land(F.LAND["Chennai"], f.home_lat[f.city == 0], f.home_lon[f.city == 0]).all()


def test_vehicles_turn_back_at_the_coast():
    f = Fleet(5_000, seed=7, start_ms=T0)
    chennai = np.flatnonzero(f.city == 0)
    f.state[chennai] = DRIVING
    f.speed[chennai] = 90.0
    f.cruise[chennai] = 95.0
    f.heading[chennai] = 90.0  # due east, into the Bay of Bengal
    odo = f.odo.copy()
    run(f, 600, dt=1.0, start_boost=0.0)
    assert on_land(f).all()
    assert (f.odo >= odo).all()


def test_fleet_ids_are_bounded():
    f = Fleet(2000, seed=7, fleets=12)
    assert f.fleet_id.min() >= 0 and f.fleet_id.max() < 12


def test_initial_trouble_codes_are_valid():
    f = Fleet(3000, seed=7)
    assert 0 < len(f.dtc) < 300, "about four percent of the fleet"
    for i, codes in f.dtc.items():
        assert 0 <= i < 3000
        assert 1 <= len(codes) <= 2 and len(set(codes)) == len(codes)
        assert all(is_valid_dtc(c) for c in codes)
    assert all(is_valid_dtc(c) for c in F.COMMON_DTCS)


def test_constant_tables():
    assert F.OEM_KEYS == ("nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix")
    assert sum(o.share for o in OEMS) == pytest.approx(1.0)
    assert sum(c[3] for c in F.CITIES) == pytest.approx(1.0)
    assert len({o.wmi for o in OEMS}) == len(OEMS) and all(len(o.wmi) == 3 for o in OEMS)
    assert [F.EVT_NAME[F.EVT_CODE[e]] for e in EVENT_TYPES] == list(EVENT_TYPES)
    assert F.EVT_NONE not in F.EVT_NAME


# ------------------------------------------------------------------------ step
def test_gauges_stay_in_range_after_many_steps():
    f = Fleet(1500, seed=5, fault_rate_per_s=1e-3)
    run(f, 300, dt=5.0)
    soc, fuel = f.soc[~np.isnan(f.soc)], f.fuel[~np.isnan(f.fuel)]
    assert soc.size and fuel.size
    assert soc.min() >= 0.0 and soc.max() <= 100.0
    assert fuel.min() >= 0.0 and fuel.max() <= 100.0
    assert f.speed.min() >= 0.0 and f.speed.max() <= 165.0
    assert f.heading.min() >= 0.0 and f.heading.max() < 360.0
    assert np.abs(f.lat).max() <= 89.9 and np.abs(f.lon).max() <= 180.0
    assert np.isnan(f.soc[f.powertrain == ICE]).all(), "a combustion car never grows a battery"
    assert set(f.state.tolist()) <= {PARKED, DRIVING, CHARGING}
    assert all(len(c) <= 4 and len(set(c)) == len(c) for c in f.dtc.values())


def test_battery_drains_to_empty_but_not_below():
    f = Fleet(300, seed=5)
    ev = f.powertrain == BEV
    f.soc[ev] = 0.05
    f.state[:] = DRIVING
    f.speed[:] = 80.0
    run(f, 50)
    assert f.soc[ev].min() >= 0.0


def test_step_sets_the_clock():
    f = Fleet(50, seed=1)
    f.step(1.0, T0 + 5000)
    assert f.now_ms == T0 + 5000


def test_odometer_never_goes_back_and_grows_with_driving():
    f = Fleet(1000, seed=2)
    before = f.odo.copy()
    run(f, 60)
    assert (f.odo >= before).all()
    assert (f.odo - before).sum() > 0
    assert (f.odo - before).max() <= 165.0 / 60.0 + 1e-9, "at most one minute at top speed"


def test_parked_vehicles_do_not_move():
    f = Fleet(500, seed=2)
    f.state[:] = PARKED
    f.speed[:] = 0.0
    lat, lon, odo = f.lat.copy(), f.lon.copy(), f.odo.copy()
    f.step(1.0, T0, start_boost=0.0)                      # nobody starts
    assert np.array_equal(f.lat, lat) and np.array_equal(f.lon, lon) and np.array_equal(f.odo, odo)
    assert (f.speed == 0).all()


def test_position_changes_match_speed():
    f = Fleet(500, seed=2)
    lat, lon = f.lat.copy(), f.lon.copy()
    f.step(1.0, T0)
    from rosetta.algorithms.trip_segmentation import haversine_km

    moved = haversine_km(lat, lon, f.lat, f.lon)
    assert np.allclose(moved, f.speed / 3600.0, rtol=0.02, atol=1e-6)


def test_start_boost_models_a_shift_start():
    calm, rush = Fleet(3000, seed=9), Fleet(3000, seed=9)
    for f in (calm, rush):
        f.state[:] = PARKED
        f.speed[:] = 0.0
    calm.step(1.0, T0, start_boost=1.0)
    rush.step(1.0, T0, start_boost=50.0)
    assert (rush.state == DRIVING).sum() > 10 * max(1, (calm.state == DRIVING).sum())
    started = rush.state == DRIVING
    assert (rush.evt[started] == F.EVT_CODE["IGNITION_ON"]).all()


def test_events_are_cleared_every_step_and_use_known_codes():
    f = Fleet(3000, seed=4)
    seen = Counter()
    for k in range(120):
        f.step(1.0, T0 + k * 1000)
        seen.update(f.evt[f.evt != 0].tolist())
        assert set(f.evt.tolist()) <= {0, *F.EVT_NAME}
    assert {F.EVT_CODE["IGNITION_ON"], F.EVT_CODE["IGNITION_OFF"]} <= set(seen)
    assert (f.evt != 0).mean() < 0.05, "events are rare, and yesterday's event is not repeated"


def test_low_battery_vehicle_goes_charging_and_back():
    f = Fleet(400, seed=6)
    ev = np.flatnonzero(f.powertrain == BEV)
    f.state[ev] = PARKED
    f.speed[ev] = 0.0
    f.soc[ev] = 10.0
    f.step(1.0, T0, start_boost=0.0)
    assert (f.state[ev] == CHARGING).all()
    assert (f.evt[ev] == F.EVT_CODE["CHARGE_START"]).all()
    assert (f.soc[ev] > 10.0).all()

    f.soc[ev] = 95.0
    f.step(1.0, T0 + 1000, start_boost=0.0)
    assert (f.state[ev] == PARKED).all()
    assert (f.evt[ev] == F.EVT_CODE["CHARGE_STOP"]).all()


def test_empty_tank_is_refilled_when_parked():
    f = Fleet(400, seed=6)
    ice = np.flatnonzero(f.powertrain == ICE)
    f.state[ice] = PARKED
    f.speed[ice] = 0.0
    f.fuel[ice] = 2.0
    f.step(1.0, T0, start_boost=0.0)
    assert (f.fuel[ice] == 95.0).all()


def test_faults_appear_at_the_configured_rate():
    quiet, faulty = Fleet(2000, seed=8, fault_rate_per_s=0.0), Fleet(2000, seed=8, fault_rate_per_s=5e-3)
    n0 = sum(len(c) for c in quiet.dtc.values())
    run(quiet, 30)
    run(faulty, 30)
    assert sum(len(c) for c in quiet.dtc.values()) == n0
    assert sum(len(c) for c in faulty.dtc.values()) > n0 + 100


# -------------------------------------------------------------------- emitting
def test_mode_all_returns_every_vehicle():
    f = Fleet(700, seed=1)
    assert f.emitting("all").tolist() == list(range(700))


def test_emitting_advances_the_sequence_of_those_who_report():
    f = Fleet(700, seed=1)
    before = f.seq.copy()
    idx = f.emitting("realistic")
    silent = np.setdiff1d(np.arange(700), idx)
    assert (f.seq[idx] == before[idx] + 1).all()
    assert (f.seq[silent] == before[silent]).all()
    assert (f.heartbeat[idx] == 0).all()


def test_realistic_mode_reports_moving_vehicles_every_tick():
    f = Fleet(1000, seed=1)
    f.heartbeat[:] = 0
    f.step(1.0, T0)
    idx = set(f.emitting("realistic").tolist())
    driving = set(np.flatnonzero(f.state == DRIVING).tolist())
    with_event = set(np.flatnonzero(f.evt != 0).tolist())
    assert idx == driving | with_event
    assert 0 < len(idx) < 1000


def test_parked_vehicles_report_on_a_heartbeat():
    f = Fleet(300, seed=1)
    f.state[:] = PARKED
    f.speed[:] = 0.0
    f.heartbeat[:] = 0
    reported = Counter()
    for k in range(90):
        f.step(1.0, T0 + k * 1000, start_boost=0.0)
        reported.update(f.emitting("realistic", heartbeat_s=30).tolist())
    assert set(reported) == set(range(300))
    assert set(reported.values()) == {3}, "once every 30 s"


def test_unknown_mode_behaves_like_realistic():
    a, b = Fleet(300, seed=1), Fleet(300, seed=1)
    assert a.emitting("realistic").tolist() == b.emitting("something-else").tolist()


# ----------------------------------------------------------------------- truth
def test_truth_has_one_value_per_chosen_vehicle():
    f = Fleet(400, seed=3, start_ms=T0)
    idx = np.array([5, 17, 399])
    t = f.truth(idx)
    assert set(t) == {"i", "vin", "device_id", "ts", "seq", "lat", "lon", "heading_deg", "speed_kmh", "odo_km",
                      "soc_pct", "fuel_pct", "ambient_c", "ignition", "dtc", "evt"}
    assert all(len(v) == 3 for v in t.values())
    assert t["i"] == [5, 17, 399]
    assert t["vin"] == [f.vin[5], f.vin[17], f.vin[399]]
    assert t["ts"] == [T0] * 3
    assert t["seq"] == f.seq[idx].tolist()


def test_truth_is_plain_python():
    f = Fleet(200, seed=3, start_ms=T0)
    t = f.truth(f.emitting("all"))
    for name, values in t.items():
        assert type(values) is list, name
        assert all(not isinstance(v, np.generic) for v in values), name
    assert all(type(v) is bool for v in t["ignition"])
    assert all(type(v) is int for v in t["seq"])


def test_truth_makes_valid_canonical_events():
    f = Fleet(600, seed=3, start_ms=T0)
    run(f, 20)
    t = f.truth(f.emitting("all"))
    for j in range(600):
        ev = {k: t[k][j] for k in ("vin", "ts", "seq", "lat", "lon", "heading_deg", "speed_kmh", "odo_km",
                                   "soc_pct", "fuel_pct", "ambient_c", "ignition", "dtc", "evt")}
        assert validate(ev) is None, ev


def test_truth_marks_missing_gauges_with_none():
    f = Fleet(600, seed=3)
    t = f.truth(np.arange(600))
    for j in range(600):
        assert (t["soc_pct"][j] is None) == (f.powertrain[j] == ICE)
        assert (t["fuel_pct"][j] is None) == (f.powertrain[j] == BEV)


def test_truth_rounding():
    f = Fleet(300, seed=3)
    t = f.truth(np.arange(300))
    assert all(round(v, 6) == v for v in t["lat"] + t["lon"])
    assert all(round(v, 2) == v for v in t["speed_kmh"])
    assert all(round(v, 3) == v for v in t["odo_km"])
    assert all(0.0 <= v < 360.0 for v in t["heading_deg"])


def test_truth_without_gps_noise_is_exact():
    f = Fleet(300, seed=3)
    idx = np.arange(300)
    t = f.truth(idx, gps_noise=False)
    assert t["lat"] == np.round(f.lat, 6).tolist()
    assert t["speed_kmh"] == np.round(f.speed, 2).tolist()
    assert f.truth(idx, gps_noise=False) == t, "and it does not consume randomness"


def test_gps_noise_is_small_and_parked_vehicles_jitter():
    f = Fleet(2000, seed=3)
    idx = np.arange(2000)
    t = f.truth(idx)
    assert np.abs(np.array(t["lat"]) - f.lat).max() < 3e-4
    parked = f.state != DRIVING
    jitter = np.array(t["speed_kmh"])[parked]
    assert (jitter > 0).any() and jitter.max() < 5.0
    assert (jitter == 0).mean() > 0.5


def test_truth_events_and_ignition():
    f = Fleet(3000, seed=4)
    run(f, 5)
    t = f.truth(np.arange(3000))
    assert t["ignition"] == (f.state == DRIVING).tolist()
    assert set(t["evt"]) <= {None, *EVENT_TYPES}
    assert [e for e in t["evt"] if e] != []
    assert all(t["dtc"][i] == f.dtc.get(i, []) for i in range(3000))
