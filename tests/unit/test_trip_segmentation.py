"""rosetta.algorithms.trip_segmentation: Viterbi decoding of STOP and MOVE on a noisy track."""
from __future__ import annotations

import numpy as np
import pytest

from rosetta.algorithms import trip_segmentation as TS
from rosetta.algorithms.trip_segmentation import MOVE, STOP

T0 = 1_790_000_000_000


def make_track(seed: int = 1, light_s: int = 20):
    """Parked 5 min, a 10 min trip with one red light, parked 5 min. One point per second.

    While parked the GPS speed jitters and often exceeds 3 km/h.
    """
    rng = np.random.default_rng(seed)
    speed = np.concatenate([
        np.abs(rng.normal(0, 2.0, 300)),             # points 0..299: parked
        45 + rng.normal(0, 4, 280),                  # points 300..899: the trip
        np.zeros(light_s),
        50 + rng.normal(0, 4, 320 - light_s),
        np.abs(rng.normal(0, 2.0, 300)),             # points 900..1199: parked
    ])
    n = len(speed)
    ts = T0 + np.arange(n) * 1000
    lat = 12.97 + np.cumsum(speed / 3600.0) / 110.574
    lon = np.full(n, 77.59)
    return ts, lat, lon, speed


def kinds(segments):
    return [s.kind for s in segments]


# -------------------------------------------------------------------- helpers
def test_haversine_known_distances():
    assert TS.haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.195, abs=0.01)
    assert TS.haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.195, abs=0.01)
    assert TS.haversine_km(12.9716, 77.5946, 12.9716, 77.5946) == 0.0
    # Chennai to Bengaluru, about 290 km
    assert TS.haversine_km(13.0827, 80.2707, 12.9716, 77.5946) == pytest.approx(290.2, abs=1.0)


def test_haversine_is_symmetric_and_vectorised():
    a = np.array([10.0, 20.0, -33.0])
    b = np.array([70.0, 80.0, 151.0])
    d1 = TS.haversine_km(a, b, a + 1, b + 1)
    d2 = TS.haversine_km(a + 1, b + 1, a, b)
    assert d1.shape == (3,)
    assert np.allclose(d1, d2)
    assert (d1 > 0).all()


def test_haversine_antipodes_is_half_the_circumference():
    assert TS.haversine_km(0.0, 0.0, 0.0, 180.0) == pytest.approx(np.pi * TS.EARTH_KM)


def test_emission_costs_prefer_stop_at_standstill_and_move_at_speed():
    costs = TS.emission_costs(np.array([0.0, 1.0, 40.0, 120.0]))
    assert costs.shape == (4, 2)
    assert costs[0, STOP] < costs[0, MOVE]
    assert costs[1, STOP] < costs[1, MOVE]
    assert costs[2, MOVE] < costs[2, STOP]
    assert costs[3, MOVE] < costs[3, STOP]


def test_emission_costs_treat_negative_speed_as_zero():
    costs = TS.emission_costs(np.array([-5.0, 0.0]))
    assert np.array_equal(costs[0], costs[1])


def test_threshold_baseline():
    assert TS.threshold_baseline(np.array([0.0, 3.0, 3.01, 50.0])).tolist() == [0, 0, 1, 1]
    assert TS.threshold_baseline(np.array([4.0, 6.0]), threshold=5.0).tolist() == [0, 1]


# -------------------------------------------------------------------- viterbi
def test_viterbi_empty_input():
    assert TS.viterbi(np.zeros((0, 2)), 1.0).size == 0


def test_viterbi_without_penalty_picks_the_cheapest_state_per_point():
    costs = np.array([[0.0, 1.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])
    assert TS.viterbi(costs, 0.0).tolist() == [0, 1, 0, 1]


def test_viterbi_with_a_high_penalty_never_switches():
    costs = np.array([[0.0, 1.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]])
    assert TS.viterbi(costs, 100.0).tolist() == [0] * 5


def test_viterbi_switches_when_the_evidence_outweighs_the_penalty():
    costs = np.array([[0.0, 5.0]] * 4 + [[5.0, 0.0]] * 4)
    assert TS.viterbi(costs, 3.0).tolist() == [0, 0, 0, 0, 1, 1, 1, 1]


def test_viterbi_finds_the_minimum_cost_path():
    rng = np.random.default_rng(7)
    costs = rng.uniform(0, 3, (9, 2))
    penalty = 1.2

    def total(path):
        switches = sum(1 for a, b in zip(path, path[1:]) if a != b)
        return sum(costs[i, s] for i, s in enumerate(path)) + penalty * switches

    best = min(total([(m >> i) & 1 for i in range(9)]) for m in range(2 ** 9))
    assert total(TS.viterbi(costs, penalty).tolist()) == pytest.approx(best)


# ------------------------------------------------------------------- segments
def test_to_segments_empty():
    empty = np.array([])
    assert TS.to_segments(np.zeros(0, dtype=np.int8), empty, empty, empty, empty) == []


def test_to_segments_cuts_at_state_changes():
    states = np.array([0, 0, 1, 1, 1, 0], dtype=np.int8)
    ts = T0 + np.arange(6) * 1000
    lat = np.array([0.0, 0.0, 0.0, 0.01, 0.02, 0.02])
    lon = np.zeros(6)
    speed = np.array([0.0, 1.0, 30.0, 45.0, 20.0, 0.0])
    segs = TS.to_segments(states, ts, lat, lon, speed)
    assert [(s.kind, s.start, s.end) for s in segs] == [("stop", 0, 1), ("trip", 2, 4), ("stop", 5, 5)]
    trip = segs[1]
    assert trip.max_speed_kmh == 45.0
    assert trip.distance_km == pytest.approx(TS.haversine_km(0.0, 0.0, 0.02, 0.0), rel=1e-6)
    assert trip.duration_s == 2.0
    assert (trip.start_ts, trip.end_ts) == (T0 + 2000, T0 + 4000)
    assert segs[0].distance_km == 0.0 and segs[2].distance_km == 0.0


def test_segment_kind_and_duration():
    seg = TS.Segment(state=MOVE, start=0, end=9, start_ts=T0, end_ts=T0 + 90_500, distance_km=1.0, max_speed_kmh=50.0)
    assert seg.kind == "trip" and seg.duration_s == 90.5
    assert TS.Segment(STOP, 0, 0, T0, T0, 0.0, 0.0).kind == "stop"


# -------------------------------------------------------------- segment_trips
def test_empty_input_gives_no_segments():
    assert TS.segment_trips([], [], [], []) == []


def test_single_stationary_point():
    segs = TS.segment_trips([T0], [12.97], [77.59], [0.0])
    assert len(segs) == 1
    assert (segs[0].kind, segs[0].start, segs[0].end, segs[0].duration_s) == ("stop", 0, 0, 0.0)


def test_single_moving_point():
    segs = TS.segment_trips([T0], [12.97], [77.59], [60.0])
    assert kinds(segs) == ["trip"]
    assert segs[0].distance_km == 0.0 and segs[0].max_speed_kmh == 60.0


def test_stop_trip_stop_is_found_despite_noise_and_a_traffic_light():
    ts, lat, lon, speed = make_track()
    segs = TS.segment_trips(ts, lat, lon, speed)
    assert kinds(segs) == ["stop", "trip", "stop"]
    trip = segs[1]
    assert abs(trip.start - 300) <= 3 and abs(trip.end - 899) <= 3
    assert trip.duration_s == pytest.approx(600, abs=6)
    assert trip.distance_km == pytest.approx(float(speed[300:900].sum() / 3600.0), rel=0.02)
    assert trip.max_speed_kmh == pytest.approx(float(speed.max()))


def test_threshold_baseline_shatters_the_same_track():
    ts, lat, lon, speed = make_track()
    naive = TS.to_segments(TS.threshold_baseline(speed), ts, lat, lon, speed)
    assert len(naive) > 30
    assert len(TS.segment_trips(ts, lat, lon, speed)) == 3


@pytest.mark.parametrize("seed", range(5))
def test_result_is_stable_across_noise_realisations(seed):
    ts, lat, lon, speed = make_track(seed=seed)
    assert kinds(TS.segment_trips(ts, lat, lon, speed)) == ["stop", "trip", "stop"]


def test_segments_cover_every_point_exactly_once():
    ts, lat, lon, speed = make_track()
    segs = TS.segment_trips(ts, lat, lon, speed)
    assert segs[0].start == 0 and segs[-1].end == len(ts) - 1
    for a, b in zip(segs, segs[1:]):
        assert b.start == a.end + 1
        assert a.state != b.state, "neighbouring segments alternate"


def test_a_long_pause_splits_the_trip():
    ts, lat, lon, speed = make_track(light_s=240)
    assert kinds(TS.segment_trips(ts, lat, lon, speed)) == ["stop", "trip", "stop", "trip", "stop"]


def test_min_stop_controls_what_counts_as_a_stop():
    ts, lat, lon, speed = make_track(light_s=60)
    assert kinds(TS.segment_trips(ts, lat, lon, speed, min_stop_s=90.0)) == ["stop", "trip", "stop"]
    assert kinds(TS.segment_trips(ts, lat, lon, speed, min_stop_s=20.0)) == ["stop", "trip", "stop", "trip", "stop"]


def test_input_order_does_not_matter():
    ts, lat, lon, speed = make_track()
    perm = np.random.default_rng(9).permutation(len(ts))
    assert TS.segment_trips(ts[perm], lat[perm], lon[perm], speed[perm]) == TS.segment_trips(ts, lat, lon, speed)


def test_result_does_not_depend_on_the_reporting_rate():
    ts, lat, lon, speed = make_track()
    every_5s = TS.segment_trips(ts[::5], lat[::5], lon[::5], speed[::5])
    assert kinds(every_5s) == ["stop", "trip", "stop"]
    assert every_5s[1].duration_s == pytest.approx(600, abs=15)


def test_all_parked_is_one_stop():
    rng = np.random.default_rng(10)
    n = 600
    speed = np.abs(rng.normal(0, 2.0, n))
    segs = TS.segment_trips(T0 + np.arange(n) * 1000, np.full(n, 12.97), np.full(n, 77.59), speed)
    assert kinds(segs) == ["stop"]
    assert segs[0].distance_km == 0.0


def test_all_driving_is_one_trip():
    n = 600
    speed = np.full(n, 60.0)
    lat = 12.97 + np.arange(n) * (60.0 / 3600.0) / 110.574
    segs = TS.segment_trips(T0 + np.arange(n) * 1000, lat, np.full(n, 77.59), speed)
    assert kinds(segs) == ["trip"]
    assert segs[0].distance_km == pytest.approx(10.0, rel=0.01)


def test_accepts_plain_python_lists():
    ts, lat, lon, speed = make_track()
    segs = TS.segment_trips(ts.tolist(), lat.tolist(), lon.tolist(), speed.tolist())
    assert kinds(segs) == ["stop", "trip", "stop"]
