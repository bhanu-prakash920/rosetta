"""Trip segmentation with dynamic programming (a two-state Viterbi decoder).

Problem: a vehicle's GPS track is noisy. Speed jitters above zero while parked
and drops to zero at traffic lights. A simple "speed > 3 km/h" threshold
therefore cuts one trip into many pieces and invents short fake trips.

Approach: label every point STOP or MOVE so that the total cost is minimal:

    cost = sum of emission(point, state)  +  penalty * (number of state changes)

emission is the negative log-likelihood of the observed speed under each state
(stopped speeds follow an exponential around GPS noise, moving speeds a wide
normal). The switching penalty is what removes flicker.

    dp[i][s] = emission(i, s) + min(dp[i-1][s], dp[i-1][1-s] + penalty)

Time O(n * k^2) with k = 2 states, so O(n). Space O(n) for the back-pointers.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

STOP, MOVE = 0, 1
EARTH_KM = 6371.0088


@dataclass(frozen=True)
class Segment:
    state: int          # STOP or MOVE
    start: int          # index of the first point
    end: int            # index of the last point (inclusive)
    start_ts: int       # epoch ms
    end_ts: int
    distance_km: float
    max_speed_kmh: float

    @property
    def duration_s(self) -> float:
        return (self.end_ts - self.start_ts) / 1000.0

    @property
    def kind(self) -> str:
        return "trip" if self.state == MOVE else "stop"


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance. Works on floats and on numpy arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def emission_costs(speed_kmh: np.ndarray, noise_kmh: float = 1.5,
                   cruise_kmh: float = 40.0, cruise_sd: float = 28.0) -> np.ndarray:
    """Negative log-likelihood of each speed under STOP and MOVE. Shape (n, 2)."""
    v = np.maximum(np.asarray(speed_kmh, dtype=np.float64), 0.0)
    stop = v / noise_kmh + np.log(noise_kmh)
    move = 0.5 * ((v - cruise_kmh) / cruise_sd) ** 2 + np.log(cruise_sd * np.sqrt(2 * np.pi))
    return np.stack([stop, move], axis=1)


def viterbi(costs: np.ndarray, penalty: float) -> np.ndarray:
    """Minimum-cost state sequence for an (n, k) emission cost matrix."""
    n, k = costs.shape
    if n == 0:
        return np.zeros(0, dtype=np.int8)
    back = np.zeros((n, k), dtype=np.int8)
    prev = costs[0].copy()
    switch = penalty * (1.0 - np.eye(k))
    for i in range(1, n):
        cand = prev[:, None] + switch      # cand[from, to]
        best_from = np.argmin(cand, axis=0)
        back[i] = best_from
        prev = cand[best_from, np.arange(k)] + costs[i]
    states = np.zeros(n, dtype=np.int8)
    states[-1] = int(np.argmin(prev))
    for i in range(n - 1, 0, -1):
        states[i - 1] = back[i, states[i]]
    return states


def threshold_baseline(speed_kmh: np.ndarray, threshold: float = 3.0) -> np.ndarray:
    """The naive method we compare against: moving when speed is above a threshold."""
    return (np.asarray(speed_kmh) > threshold).astype(np.int8)


def to_segments(states: np.ndarray, ts_ms: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                speed_kmh: np.ndarray) -> list[Segment]:
    n = len(states)
    if n == 0:
        return []
    step_km = np.zeros(n)
    if n > 1:
        step_km[1:] = haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:])
    cuts = np.flatnonzero(np.diff(states)) + 1
    bounds = np.concatenate([[0], cuts, [n]])
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        out.append(Segment(
            state=int(states[a]), start=int(a), end=int(b - 1),
            start_ts=int(ts_ms[a]), end_ts=int(ts_ms[b - 1]),
            distance_km=float(step_km[a + 1:b].sum()) if states[a] == MOVE else 0.0,
            max_speed_kmh=float(speed_kmh[a:b].max()),
        ))
    return out


def segment_trips(ts_ms, lat, lon, speed_kmh, min_stop_s: float = 90.0) -> list[Segment]:
    """Sort by event time, decode states, return alternating stop and trip segments.

    Emission costs are weighted by the time each point covers, so the result does
    not depend on the reporting rate. `min_stop_s` is roughly the shortest pause
    that counts as a real stop: a 20 s traffic light stays inside the trip.
    """
    ts_ms = np.asarray(ts_ms, dtype=np.int64)
    order = np.argsort(ts_ms, kind="stable")
    ts_ms = ts_ms[order]
    lat = np.asarray(lat, dtype=np.float64)[order]
    lon = np.asarray(lon, dtype=np.float64)[order]
    speed = np.asarray(speed_kmh, dtype=np.float64)[order]
    n = len(ts_ms)
    if n == 0:
        return []
    dt = np.ones(n)
    if n > 1:
        d = np.diff(ts_ms) / 1000.0
        dt[1:] = np.clip(d, 0.0, 300.0)
        dt[0] = float(np.median(dt[1:]))
    costs = emission_costs(speed) * dt[:, None]
    # At standstill MOVE costs about 4.6 more per second than STOP. Leaving and
    # re-entering MOVE costs two penalties, so the break-even pause is min_stop_s.
    penalty = 0.5 * min_stop_s * float(emission_costs(np.zeros(1))[0, MOVE] - emission_costs(np.zeros(1))[0, STOP])
    states = viterbi(costs, penalty)
    return to_segments(states, ts_ms, lat, lon, speed)
