"""Vectorised fleet simulator: the state of 100,000+ vehicles in numpy arrays.

One simulated vehicle is a row across a set of arrays, not an object or a
thread, so a step of the whole fleet is a handful of array operations.
step(): O(n) time, O(n) memory (about 120 bytes per vehicle).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..domain.canonical import EVENT_TYPES
from ..domain.vin import make_vin

PARKED, DRIVING, CHARGING = 0, 1, 2
ICE, BEV, PHEV = 0, 1, 2

EVT_NONE = 0
EVT_CODE = {name: i + 1 for i, name in enumerate(EVENT_TYPES)}
EVT_NAME = {i + 1: name for i, name in enumerate(EVENT_TYPES)}

# (name, lat, lon, share of fleet, typical temperature C)
CITIES = (
    ("Chennai", 13.0827, 80.2707, 0.14, 31.0),
    ("Bengaluru", 12.9716, 77.5946, 0.15, 25.0),
    ("Mumbai", 19.0760, 72.8777, 0.14, 29.0),
    ("Delhi", 28.6139, 77.2090, 0.13, 27.0),
    ("Hyderabad", 17.3850, 78.4867, 0.10, 28.0),
    ("Pune", 18.5204, 73.8567, 0.08, 26.0),
    ("Surat", 21.1702, 72.8311, 0.05, 30.0),
    ("Kolkata", 22.5726, 88.3639, 0.07, 29.0),
    ("Ahmedabad", 23.0225, 72.5714, 0.05, 31.0),
    ("Jaipur", 26.9124, 75.7873, 0.04, 28.0),
    ("Kochi", 9.9975, 76.2995, 0.03, 28.0),
    ("Coimbatore", 11.0168, 76.9558, 0.02, 27.0),
)

# Land around the coastal cities, as (lat, lon) outlines traced a few hundred
# metres inside the Natural Earth 1:10m coastline. Vehicles of these cities are
# kept inside their outline; inland cities need none. Harbours, creeks and
# backwaters (Mumbai harbour, Thane creek, Pulicat lake, Vembanad) are cut out.
LAND = {
    "Chennai": np.array([
        (12.40, 79.60), (12.40, 80.110), (12.45, 80.134), (12.50, 80.160), (12.55, 80.176),
        (12.60, 80.186), (12.65, 80.208), (12.70, 80.226), (12.75, 80.240), (12.80, 80.250),
        (12.85, 80.256), (12.90, 80.260), (12.95, 80.262), (13.00, 80.270), (13.05, 80.278),
        (13.075, 80.284), (13.09, 80.296), (13.10, 80.302), (13.15, 80.302), (13.20, 80.326), (13.215, 80.308), (13.245, 80.310), (13.255, 80.328), (13.30, 80.334),
        (13.35, 80.340), (13.40, 80.320), (13.425, 80.272), (13.45, 80.250), (13.50, 80.114), (13.55, 80.076),
        (13.60, 80.046), (13.63, 80.046), (13.65, 80.068), (13.70, 80.060), (13.70, 79.60)]),
    "Mumbai": np.array([
        (18.915, 72.816), (18.94, 72.816), (18.96, 72.812), (18.98, 72.802), (19.00, 72.812),
        (19.02, 72.814), (19.04, 72.830), (19.06, 72.832), (19.08, 72.824), (19.10, 72.832),
        (19.12, 72.830), (19.14, 72.820), (19.16, 72.832), (19.24, 72.832), (19.256, 72.836), (19.262, 72.784),
        (19.28, 72.782), (19.29, 72.786), (19.29, 72.874), (19.275, 72.976), (19.255, 72.990),
        (19.235, 72.998), (19.215, 73.006), (19.20, 73.03), (19.20, 73.60),
        (18.44, 73.60), (18.44, 73.06), (19.02, 73.06), (19.03, 73.000), (19.06, 72.998),
        (19.08, 72.982), (19.10, 72.978), (19.12, 72.980), (19.14, 72.982), (19.16, 72.984),
        (19.178, 72.972), (19.16, 72.958), (19.14, 72.948), (19.12, 72.934), (19.10, 72.928),
        (19.08, 72.924), (19.065, 72.922), (19.06, 72.926), (19.04, 72.930), (19.03, 72.878), (19.00, 72.856),
        (18.98, 72.842), (18.96, 72.832), (18.94, 72.828), (18.92, 72.822)]),
    "Kochi": np.array([
        (9.30, 76.90), (9.30, 76.384), (9.40, 76.350), (9.45, 76.334), (9.49, 76.40), (9.49, 76.52),
        (9.54, 76.52), (9.56, 76.50), (9.57, 76.47), (9.58, 76.424), (9.60, 76.428), (9.65, 76.424), (9.70, 76.420), (9.75, 76.402),
        (9.80, 76.392), (9.85, 76.400), (9.90, 76.372), (9.92, 76.364), (9.94, 76.344),
        (9.96, 76.330), (9.968, 76.324), (9.972, 76.294), (9.98, 76.286), (10.00, 76.282), (10.04, 76.276),
        (10.05, 76.264), (10.10, 76.254), (10.18, 76.252), (10.235, 76.254), (10.26, 76.226),
        (10.266, 76.218), (10.27, 76.150), (10.30, 76.138), (10.35, 76.128), (10.40, 76.104), (10.45, 76.074), (10.50, 76.064),
        (10.55, 76.032), (10.60, 76.010), (10.60, 76.90)]),
    "Surat": np.array([
        (20.55, 73.40), (20.55, 72.898), (20.60, 72.896), (20.65, 72.914), (20.70, 72.918),
        (20.74, 72.920), (20.755, 72.956), (20.775, 72.956), (20.80, 72.932),
        (20.82, 72.930), (20.85, 72.900), (20.87, 72.902), (20.90, 72.878), (20.95, 72.876),
        (20.958, 72.896), (20.982, 72.898), (20.988, 72.840), (21.05, 72.860), (21.12, 72.836),
        (21.13, 72.822), (21.135, 72.736), (21.15, 72.736), (21.20, 72.742),
        (21.25, 72.664), (21.27, 72.654), (21.30, 72.612), (21.34, 72.598), (21.355, 72.654), (21.41, 72.650), (21.42, 72.668),
        (21.43, 72.684), (21.44, 72.698), (21.45, 72.714), (21.452, 72.762), (21.48, 72.762),
        (21.50, 72.646), (21.55, 72.722), (21.56, 72.752), (21.57, 72.770),
        (21.58, 72.784), (21.59, 72.794), (21.60, 72.802), (21.60, 73.40)]),
}


def on_land(outline: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """True where (lat, lon) lies inside the outline (even-odd ray casting)."""
    y, x = outline[:, 0], outline[:, 1]
    y2, x2 = np.roll(y, -1), np.roll(x, -1)
    lat, lon = lat[:, None], lon[:, None]
    crosses = (y > lat) != (y2 > lat)
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = x + (lat - y) * (x2 - x) / (y2 - y)
    return (np.sum(crosses & (lon < xc), axis=1) % 2).astype(bool)

COMMON_DTCS = ("P0301", "P0420", "P0171", "P0128", "P0455", "P0442", "C0035", "B1342",
               "U0100", "U0121", "P0A80", "P0AA6", "P1E00", "C1234", "P0700", "P0562")


@dataclass(frozen=True)
class OemProfile:
    key: str            # source id used on the wire and in the registry
    name: str           # display name (fictional brands)
    wmi: str            # 3-char VIN prefix
    share: float        # fraction of the fleet
    bev_share: float
    phev_share: float


OEMS = (
    OemProfile("nordvik", "Nordvik Motors", "7NV", 0.24, 0.30, 0.10),
    OemProfile("pacifica", "Pacifica Auto", "5PA", 0.22, 0.15, 0.10),
    OemProfile("stellaris", "Stellaris Group", "3ST", 0.18, 0.20, 0.10),
    OemProfile("kaizen", "Kaizen Motor", "JKZ", 0.16, 0.25, 0.15),
    OemProfile("voltaic", "Voltaic EV", "8VT", 0.10, 1.00, 0.00),
    OemProfile("helix", "Helix Mobility", "WHX", 0.10, 0.40, 0.10),
)
OEM_KEYS = tuple(o.key for o in OEMS)
OEM_INDEX = {o.key: i for i, o in enumerate(OEMS)}


class Fleet:
    """State of n vehicles. `step` advances physics, `emitting` picks who reports."""

    def __init__(self, n: int = 100_000, seed: int = 7, fleets: int = 250,
                 fault_rate_per_s: float = 2e-6, start_ms: int = 0,
                 cities: tuple = CITIES) -> None:
        self.n = n
        self.rng = rng = np.random.default_rng(seed)
        shares = np.array([o.share for o in OEMS])
        self.oem = rng.choice(len(OEMS), size=n, p=shares / shares.sum()).astype(np.int8)
        self.fleet_id = (rng.zipf(1.6, size=n) % fleets).astype(np.int32)

        u = rng.random(n)
        bev = np.array([o.bev_share for o in OEMS])[self.oem]
        phev = np.array([o.phev_share for o in OEMS])[self.oem]
        self.powertrain = np.where(u < bev, BEV, np.where(u < bev + phev, PHEV, ICE)).astype(np.int8)

        cs = np.array([c[3] for c in cities])
        self.city = rng.choice(len(cities), size=n, p=cs / cs.sum()).astype(np.int8)
        clat = np.array([c[1] for c in cities])[self.city]
        clon = np.array([c[2] for c in cities])[self.city]
        self.home_lat = clat + rng.normal(0, 0.07, n)
        self.home_lon = clon + rng.normal(0, 0.07, n)
        self.lat = self.home_lat + rng.normal(0, 0.02, n)
        self.lon = self.home_lon + rng.normal(0, 0.02, n)
        self._outline = [LAND.get(c[0]) for c in cities]
        self._place_on_land(np.array([c[1] for c in cities]), np.array([c[2] for c in cities]), seed)
        self.heading = rng.uniform(0, 360, n)
        self.state = np.where(rng.random(n) < 0.45, DRIVING, PARKED).astype(np.int8)
        self.speed = np.where(self.state == DRIVING, rng.uniform(15, 70, n), 0.0)
        self.cruise = rng.choice([25.0, 40.0, 60.0, 85.0], size=n, p=[0.3, 0.4, 0.2, 0.1])
        self.odo = rng.uniform(800, 190_000, n)
        self.soc = np.where(self.powertrain != ICE, rng.uniform(25, 100, n), np.nan)
        self.fuel = np.where(self.powertrain != BEV, rng.uniform(15, 100, n), np.nan)
        self.temp = np.array([c[4] for c in cities])[self.city] + rng.normal(0, 2.5, n)
        self.seq = rng.integers(1, 60_000, n).astype(np.int64)
        self.kwh_per_km = rng.uniform(0.14, 0.24, n)
        self.battery_kwh = rng.uniform(38, 95, n)
        self.evt = np.zeros(n, dtype=np.int8)
        self.heartbeat = rng.integers(0, 60, n).astype(np.int32)
        self.fault_rate = fault_rate_per_s
        self.now_ms = start_ms

        self.dtc: dict[int, list[str]] = {}
        for i in np.flatnonzero(rng.random(n) < 0.04):
            k = 1 + int(rng.random() < 0.25)
            self.dtc[int(i)] = list(rng.choice(COMMON_DTCS, size=k, replace=False))

        serial = np.zeros(len(OEMS), dtype=np.int64)
        vins, devs = [], []
        for i in range(n):
            o = int(self.oem[i])
            prof = OEMS[o]
            vins.append(make_vin(prof.wmi, int(serial[o]),
                                 descriptor="EV1A2" if self.powertrain[i] == BEV else "GT4B7"))
            devs.append(f"{prof.key[:2]}-{int(serial[o]):07d}")
            serial[o] += 1
        self.vin = vins
        self.device_id = devs

    def _on_land(self, idx: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Land check for vehicles idx at (lat, lon); always true for inland cities."""
        ok = np.ones(len(idx), dtype=bool)
        city = self.city[idx]
        for c, outline in enumerate(self._outline):
            if outline is None:
                continue
            sel = np.flatnonzero(city == c)
            if len(sel):
                ok[sel] = on_land(outline, lat[sel], lon[sel])
        return ok

    def _place_on_land(self, clat: np.ndarray, clon: np.ndarray, seed: int) -> None:
        # A separate generator, so the rest of the fleet comes out as before.
        rng = np.random.default_rng([seed, 1])
        everyone = np.arange(self.n)
        for _ in range(40):
            wet = everyone[~self._on_land(everyone, self.home_lat, self.home_lon)]
            if not len(wet):
                break
            c = self.city[wet]
            self.home_lat[wet] = clat[c] + rng.normal(0, 0.07, len(wet))
            self.home_lon[wet] = clon[c] + rng.normal(0, 0.07, len(wet))
        else:
            wet = everyone[~self._on_land(everyone, self.home_lat, self.home_lon)]
            self.home_lat[wet], self.home_lon[wet] = clat[self.city[wet]], clon[self.city[wet]]
        wet = ~self._on_land(everyone, self.lat, self.lon)
        self.lat[wet], self.lon[wet] = self.home_lat[wet], self.home_lon[wet]

    # ------------------------------------------------------------------ physics
    def step(self, dt: float, now_ms: int, start_boost: float = 1.0) -> None:
        """Advance every vehicle by dt seconds. `start_boost` > 1 models a shift start."""
        rng, n = self.rng, self.n
        self.now_ms = now_ms
        self.evt[:] = EVT_NONE
        driving = self.state == DRIVING

        # state changes
        r = rng.random(n)
        start = (self.state == PARKED) & (r < 1.0 / 2400.0 * dt * start_boost)
        stop = driving & (r < 1.0 / 1500.0 * dt)
        low = (self.powertrain == BEV) & (self.soc < 15) & (self.state == PARKED)
        full = (self.state == CHARGING) & (self.soc >= 92)
        self.state[start] = DRIVING
        self.evt[start] = EVT_CODE["IGNITION_ON"]
        self.state[stop] = PARKED
        self.evt[stop] = EVT_CODE["IGNITION_OFF"]
        self.state[low] = CHARGING
        self.evt[low] = EVT_CODE["CHARGE_START"]
        self.state[full] = PARKED
        self.evt[full] = EVT_CODE["CHARGE_STOP"]
        driving = self.state == DRIVING

        # speed follows a cruise target that changes now and then
        change = driving & (rng.random(n) < 0.04 * dt)
        k = int(change.sum())
        if k:
            self.cruise[change] = rng.choice([0.0, 20.0, 35.0, 50.0, 70.0, 95.0], size=k,
                                             p=[0.12, 0.2, 0.28, 0.2, 0.14, 0.06])
        accel = np.clip((self.cruise - self.speed) * 0.35, -9.0, 6.0) + rng.normal(0, 1.2, n)
        harsh_b = driving & (self.speed > 35) & (rng.random(n) < 4e-4 * dt)
        harsh_a = driving & (self.speed < 40) & (rng.random(n) < 2e-4 * dt)
        accel[harsh_b] = -rng.uniform(26, 38, int(harsh_b.sum()))
        accel[harsh_a] = rng.uniform(16, 22, int(harsh_a.sum()))
        self.evt[harsh_b] = EVT_CODE["HARSH_BRAKE"]
        self.evt[harsh_a] = EVT_CODE["HARSH_ACCEL"]
        self.speed = np.where(driving, np.clip(self.speed + accel * dt, 0.0, 165.0), 0.0)
        speeding = driving & (self.speed > 120) & (self.evt == EVT_NONE) & (rng.random(n) < 0.02 * dt)
        self.evt[speeding] = EVT_CODE["SPEEDING"]

        # heading: random walk, steered home when the vehicle strays too far
        self.heading = (self.heading + np.where(driving, rng.normal(0, 5.0, n) * dt, 0.0)) % 360.0
        dlat, dlon = self.home_lat - self.lat, self.home_lon - self.lon
        far = driving & ((dlat * dlat + dlon * dlon) > 0.2 ** 2)
        if far.any():
            self.heading[far] = (np.degrees(np.arctan2(dlon[far], dlat[far])) + rng.normal(0, 12, int(far.sum()))) % 360.0

        dist = self.speed * dt / 3600.0  # km
        hr = np.radians(self.heading)
        lat = np.clip(self.lat + dist * np.cos(hr) / 110.574, -89.9, 89.9)
        lon = self.lon + dist * np.sin(hr) / (111.320 * np.cos(np.radians(lat)))
        # a vehicle that would drive into the sea stays put and turns around
        moving = np.flatnonzero(driving & (dist > 0))
        wet = moving[~self._on_land(moving, lat[moving], lon[moving])]
        if len(wet):
            lat[wet], lon[wet] = self.lat[wet], self.lon[wet]
            self.heading[wet] = (self.heading[wet] + 180.0) % 360.0
            dist[wet] = 0.0
        self.lat, self.lon = lat, lon
        self.odo += dist

        ev_like = self.powertrain != ICE
        self.soc = np.where(ev_like, np.clip(self.soc - dist * self.kwh_per_km / self.battery_kwh * 100.0, 0.0, 100.0), np.nan)
        charging = self.state == CHARGING
        self.soc = np.where(charging, np.clip(self.soc + 0.02 * dt, 0.0, 100.0), self.soc)
        self.fuel = np.where(self.powertrain != BEV, np.clip(self.fuel - dist * 0.14, 0.0, 100.0), np.nan)
        refuel = (self.powertrain != BEV) & (self.fuel < 6) & ~driving
        self.fuel[refuel] = 95.0
        low_soc = ev_like & driving & (self.soc < 12) & (self.evt == EVT_NONE) & (rng.random(n) < 0.01 * dt)
        self.evt[low_soc] = EVT_CODE["LOW_SOC"]
        self.temp += rng.normal(0, 0.01, n) * dt

        # faults appear rarely and clear even more rarely
        new_fault = np.flatnonzero(rng.random(n) < self.fault_rate * dt)
        for i in new_fault:
            codes = self.dtc.setdefault(int(i), [])
            c = str(rng.choice(COMMON_DTCS))
            if c not in codes and len(codes) < 4:
                codes.append(c)
        self.heartbeat += int(round(dt))

    def emitting(self, mode: str = "realistic", heartbeat_s: int = 30) -> np.ndarray:
        """Indices of vehicles that report on this tick.

        realistic: moving vehicles every tick, others on a heartbeat or when an event fires.
        all:       every vehicle every tick (the 100K events/sec load profile).
        """
        if mode == "all":
            idx = np.arange(self.n)
        else:
            due = self.heartbeat >= heartbeat_s
            idx = np.flatnonzero((self.state == DRIVING) | due | (self.evt != EVT_NONE))
        self.heartbeat[idx] = 0
        self.seq[idx] += 1
        return idx

    # --------------------------------------------------------------- snapshots
    def truth(self, idx: np.ndarray, gps_noise: bool = True) -> dict[str, list]:
        """The true values for the chosen vehicles as python lists, ready to encode.

        Sensor noise is applied here, so encoders and the golden set see the same
        numbers and a mapping can be checked exactly.
        """
        rng = self.rng
        m = len(idx)
        lat, lon, speed = self.lat[idx], self.lon[idx], self.speed[idx]
        if gps_noise:
            lat = lat + rng.normal(0, 2.5e-5, m)
            lon = lon + rng.normal(0, 2.5e-5, m)
            parked = self.state[idx] != DRIVING
            speed = np.where(parked, np.abs(rng.normal(0, 0.6, m)) * (rng.random(m) < 0.3), speed)
        dtc = self.dtc
        return {
            "i": idx.tolist(),
            "vin": [self.vin[i] for i in idx],
            "device_id": [self.device_id[i] for i in idx],
            "ts": [self.now_ms] * m,
            "seq": self.seq[idx].tolist(),
            "lat": np.round(lat, 6).tolist(),
            "lon": np.round(lon, 6).tolist(),
            "heading_deg": (np.round(self.heading[idx], 1) % 360.0).tolist(),
            "speed_kmh": np.round(speed, 2).tolist(),
            "odo_km": np.round(self.odo[idx], 3).tolist(),
            "soc_pct": [None if v != v else round(v, 1) for v in self.soc[idx].tolist()],
            "fuel_pct": [None if v != v else round(v, 1) for v in self.fuel[idx].tolist()],
            "ambient_c": np.round(self.temp[idx], 1).tolist(),
            "ignition": (self.state[idx] == DRIVING).tolist(),
            "dtc": [dtc.get(i, []) for i in idx.tolist()],
            "evt": [EVT_NAME.get(e) for e in self.evt[idx].tolist()],
        }
