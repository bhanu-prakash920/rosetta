"""Synthetic dialect generator: training and test data for the field mapper.

There is no public corpus of "OEM telemetry formats with their correct
mappings", so we generate one. Each synthetic dialect picks, at random, a
name for every canonical field, a unit, an encoding, a nesting style and a
handful of distractor fields that must be ignored. Values come from the fleet
simulator, so the physics in the data is consistent.

Leakage control, stated plainly because it decides whether the evaluation
means anything:
  * test dialects draw names only from TEST_NAMES, which never appear in training
  * every name token used by the two demo cases (the unknown OEM "helix" and
    the drifted "pacifica_v2") is removed from the training vocabulary
  * training and test fleets use different random seeds and different regions
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..domain.canonical import EVENT_TYPES
from ..simulator.dialects import SPEC_HELIX, SPEC_PACIFICA, SPEC_PACIFICA_V2, iso
from ..simulator.fleet import DRIVING, Fleet
from .labels import IGNORE
from .profile import name_tokens

TRAIN_NAMES: dict[str, list[str]] = {
    "vin": ["vin", "vehicle_id", "vehicleIdentificationNumber", "chassis_no", "veh_vin", "vinNumber",
            "chassis", "vehicleId", "vin_code", "car_vin"],
    "ts": ["ts", "timestamp", "time", "recorded_at", "event_time", "datetime", "sent", "captured",
           "utc", "epoch", "time_utc", "sampledAt", "created"],
    "seq": ["seq", "sequence", "counter", "msg_no", "ctr", "message_id", "index", "serial", "msgId",
            "frame", "count"],
    "lat": ["lat", "latitude", "gps_lat", "position_lat", "gnss_lat", "latDeg", "lat_e7", "loc_lat",
            "latitud", "y_coord"],
    "lon": ["lon", "lng", "longitude", "gps_lon", "position_lon", "gnss_lon", "long", "lon_e7",
            "loc_lng", "longitud", "x_coord"],
    "heading": ["heading", "hdg", "course", "bearing", "direction", "cog", "azimuth", "gnss_course",
                "rumbo", "cap"],
    "speed": ["spd", "velocity", "speed_kmh", "speed_mph", "gps_speed", "vehicle_speed", "veh_speed",
              "velocidad", "vitesse", "kph", "mph", "groundspeed"],
    "odo": ["odo", "mileage", "total_distance", "odo_km", "odometer_mi", "odometerKm", "veh_odo",
            "distance_total", "kilometraje", "odo_m", "milage"],
    "soc": ["soc", "battery", "batt_pct", "state_of_charge", "battery_level", "charge", "bat_level",
            "hv_soc", "stateOfCharge", "bateria", "batterie", "soc_pm"],
    "fuel": ["fuel", "fuel_pct", "fuel_level", "gas", "fuelLevel", "petrol", "fuel_pm", "combustible",
             "carburant", "gasoline", "diesel_level"],
    "ambient": ["temp", "temperature", "ambient", "outside_temp", "oat", "ext_temp", "ambientTemp",
                "air_temp", "outsideTempC", "env_temp", "oat_f", "temperatura"],
    "ignition": ["ignition", "ign", "engine_on", "power", "running", "key_on", "engineState",
                 "pwr_state", "ignition_on", "encendido", "contact"],
    "dtc": ["dtc", "dtcs", "codes", "fault_codes", "trouble_codes", "diagnostics", "errors", "faults",
            "troubleCodes", "diag_codes", "codigos", "obd_codes"],
    "evt": ["event", "evt", "event_type", "evt_code", "alert", "trigger", "eventName", "drv_event",
            "ev", "evento", "evenement", "behaviour"],
}
TEST_NAMES: dict[str, list[str]] = {
    "vin": ["vehicle_ident", "chassisNumber", "carId", "identnummer", "fahrgestell"],
    "ts": ["when", "tstamp", "loggedWhen", "sampleTime", "zeitpunkt", "moment"],
    "seq": ["seqno", "packet", "msgCount", "laufnummer", "ordinal"],
    "lat": ["geo_lat", "breitengrad", "northing", "lat_wgs"],
    "lon": ["geo_lng", "laengengrad", "easting", "lng_wgs"],
    "heading": ["track", "yaw", "richtung", "orientation"],
    "speed": ["geschwindigkeit", "vel", "tempo", "pace", "rapidity"],
    "odo": ["kilometrage", "odoReading", "totalKm", "laufleistung", "travelled"],
    "soc": ["batteryPercent", "energyLevel", "ladezustand", "cell_level", "hvLevel"],
    "fuel": ["kraftstoff", "benzin", "fuelRemaining", "petrolLevel"],
    "ambient": ["outsideAir", "surroundTemp", "aussentemperatur", "weatherTemp"],
    "ignition": ["motorOn", "isRunning", "zuendstatus", "engineActive"],
    "dtc": ["faultList", "malfunction", "fehlercodes", "obdFaults"],
    "evt": ["incident", "eventKind", "vorfall", "occurrence"],
}
GROUPS = {
    "vin": ["vehicle", "veh", "meta", "hdr", "id", "header", ""],
    "ts": ["meta", "hdr", "header", "time", ""],
    "seq": ["meta", "hdr", "header", ""],
    "lat": ["position", "pos", "gps", "gnss", "loc", "location", "geo", ""],
    "lon": ["position", "pos", "gps", "gnss", "loc", "location", "geo", ""],
    "heading": ["position", "pos", "gps", "gnss", "motion", ""],
    "speed": ["motion", "drive", "dyn", "kinematics", "veh", ""],
    "odo": ["motion", "drive", "veh", "usage", ""],
    "soc": ["energy", "battery", "hv", "power", ""],
    "fuel": ["energy", "engine", "fluids", ""],
    "ambient": ["environment", "env", "climate", "weather", ""],
    "ignition": ["state", "engine", "status", ""],
    "dtc": ["diagnostics", "diag", "health", "obd", ""],
    "evt": ["events", "behaviour", "driving", ""],
}
UNITS = {
    "ts": ["epoch_ms", "epoch_s", "epoch_us", "iso8601"],
    "lat": ["deg", "deg", "microdeg", "e7"],
    "lon": None,   # always the same unit as lat
    "heading": ["deg", "deg", "centideg"],
    "speed": ["kmh", "mph", "mps", "knots", "centi_kmh"],
    "odo": ["km", "mi", "m", "hm"],
    "soc": ["pct", "fraction", "permille"],
    "fuel": ["pct", "fraction", "permille"],
    "ambient": ["c", "f", "k", "deci_c"],
}
_FACTOR = {"kmh": 1.0, "mph": 1 / 1.609344, "mps": 1 / 3.6, "knots": 1 / 1.852, "centi_kmh": 100.0,
           "km": 1.0, "mi": 1 / 1.609344, "m": 1000.0, "hm": 10.0,
           "pct": 1.0, "fraction": 0.01, "permille": 10.0,
           "deg": 1.0, "microdeg": 1e6, "e7": 1e7, "centideg": 100.0}
_INTEGER_UNITS = {"centi_kmh", "m", "hm", "permille", "microdeg", "e7", "centideg", "deci_c"}

REGIONS = (
    (("Chennai", 13.08, 80.27, 1, 31), ("Delhi", 28.61, 77.21, 1, 27), ("Mumbai", 19.08, 72.88, 1, 29)),
    (("Berlin", 52.52, 13.40, 1, 11), ("Madrid", 40.42, -3.70, 1, 17), ("Rome", 41.90, 12.50, 1, 18)),
    (("Chicago", 41.88, -87.63, 1, 12), ("Austin", 30.27, -97.74, 1, 24), ("Denver", 39.74, -104.99, 1, 10)),
    (("Sao Paulo", -23.55, -46.63, 1, 22), ("Lima", -12.05, -77.04, 1, 20), ("Bogota", 4.71, -74.07, 1, 15)),
    (("Sydney", -33.87, 151.21, 1, 19), ("Auckland", -36.85, 174.76, 1, 16), ("Perth", -31.95, 115.86, 1, 21)),
    (("Tokyo", 35.68, 139.69, 1, 17), ("Seoul", 37.57, 126.98, 1, 13), ("Osaka", 34.69, 135.50, 1, 18)),
    (("Nairobi", -1.29, 36.82, 1, 20), ("Lagos", 6.52, 3.38, 1, 28), ("Cairo", 30.04, 31.24, 1, 25)),
    (("Oslo", 59.91, 10.75, 1, 6), ("Helsinki", 60.17, 24.94, 1, 5), ("Reykjavik", 64.15, -21.94, 1, -1)),
)


def demo_tokens() -> set[str]:
    """Name tokens of the two demo cases. Removed from the training vocabulary."""
    toks: set[str] = set()
    for f in SPEC_HELIX["fields"].values():
        toks.update(name_tokens(f["path"]).split())
    v1 = {f["path"] for f in SPEC_PACIFICA["fields"].values()}
    for f in SPEC_PACIFICA_V2["fields"].values():
        if f["path"] not in v1:
            toks.update(name_tokens(f["path"]).split())
    return toks


def _clean(names: dict[str, list[str]], banned: set[str]) -> dict[str, list[str]]:
    out = {}
    for k, lst in names.items():
        keep = [n for n in lst if not (set(name_tokens(n).split()) & banned)]
        out[k] = keep or lst[:1]
    return out


@dataclass
class Recording:
    """Truth for `devices` vehicles over `ticks` seconds: the raw material for one dialect."""
    truth: list[dict[str, list]]      # one dict per tick
    devices: int


def record(seed: int, region: int, vehicles: int = 260, ticks: int = 36, keep: int = 40) -> Recording:
    f = Fleet(vehicles, seed=seed, fleets=4, fault_rate_per_s=2e-4, cities=REGIONS[region % len(REGIONS)])
    for k in range(20):
        f.step(1.0, 1_780_000_000_000 + k * 1000)
    rng = np.random.default_rng(seed)
    moving = np.flatnonzero(f.state == DRIVING)
    parked = np.flatnonzero(f.state != DRIVING)
    pick = np.concatenate([rng.choice(moving, size=min(len(moving), int(keep * 0.8)), replace=False),
                           rng.choice(parked, size=min(len(parked), keep - int(keep * 0.8)), replace=False)])
    out = []
    t0 = 1_780_000_020_000 + int(rng.integers(0, 10**9)) // 1000 * 1000
    for k in range(ticks):
        f.step(1.0, t0 + k * 1000 + int(rng.integers(0, 40)))
        f.seq[pick] += 1
        out.append(f.truth(pick))
    return Recording(out, len(pick))


def _style(name: str, style: str) -> str:
    parts = [p for p in name_tokens(name).split() if p]
    if not parts:
        return name
    if style == "camel":
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    if style == "upper":
        return "_".join(parts).upper()
    if style == "kebab":
        return "-".join(parts)
    if style == "pascal":
        return "".join(p.capitalize() for p in parts)
    return "_".join(parts)


def _distractors(rng: np.random.Generator, truth: dict[str, list], tick: int) -> dict[str, Any]:
    n = len(truth["vin"])
    spd = np.asarray(truth["speed_kmh"])
    return {
        "firmware": ["FW-" + str(3 + i % 4) + ".1" for i in truth["i"]],
        "schema": ["v2"] * n,
        "rssi": rng.integers(-112, -55, n).tolist(),
        "hdop": np.round(rng.uniform(0.6, 2.8, n), 2).tolist(),
        "satellites": rng.integers(5, 15, n).tolist(),
        "aux_voltage": np.round(rng.normal(12.9, 0.4, n), 2).tolist(),
        "rpm": np.round(np.where(spd > 1, 800 + spd * 28 + rng.normal(0, 120, n), 0)).astype(int).tolist(),
        "cabin_temp": np.round(rng.normal(22, 1.5, n), 1).tolist(),
        "tyre_fl": np.round(rng.normal(33, 1.0, n), 1).tolist(),
        "altitude": np.round(np.abs(np.asarray(truth["lat"]) * 7.0) + rng.normal(0, 1.5, n), 1).tolist(),
        "gear": [("D" if s > 1 else "P") for s in spd],
        "door_open": (rng.random(n) < 0.02).tolist(),
        "accel_x": np.round(rng.normal(0, 0.6, n), 3).tolist(),
        "engine_hours": np.round(np.asarray(truth["odo_km"]) / 38.0 + tick / 3600.0, 3).tolist(),
        "network": [("LTE", "5G", "4G")[i % 3] for i in truth["i"]],
        "msg_type": ["TLM"] * n,
        "uptime_s": [4000 + tick + (i % 900) for i in truth["i"]],
    }


DISTRACTOR_ALIASES = {
    "firmware": ["firmware", "sw_version", "fwVersion", "build"], "schema": ["schema", "format", "spec"],
    "rssi": ["rssi", "signal", "signal_dbm"], "hdop": ["hdop", "gps_accuracy", "dop"],
    "satellites": ["satellites", "sats", "sat_count"], "aux_voltage": ["aux_voltage", "v12", "lv_batt_v"],
    "rpm": ["rpm", "engine_rpm", "revs"], "cabin_temp": ["cabin_temp", "interior_temp", "inside_c"],
    "tyre_fl": ["tyre_fl", "tire_pressure_fl", "tp_front_left"], "altitude": ["altitude", "alt", "elevation"],
    "gear": ["gear", "prnd", "shift"], "door_open": ["door_open", "doors", "door_ajar"],
    "accel_x": ["accel_x", "ax", "long_accel"], "engine_hours": ["engine_hours", "run_hours", "hours"],
    "network": ["network", "rat", "bearer"], "msg_type": ["msg_type", "type", "kind"],
    "uptime_s": ["uptime_s", "uptime", "since_boot"],
}


@dataclass
class SynthDialect:
    messages: list[dict[str, Any]]     # decoded objects
    devices: list[str]
    labels: dict[str, str]             # flattened path -> label
    evt_codes: dict[str, str]          # source code -> canonical event


def make_dialect(rec: Recording, rng: np.random.Generator, names: dict[str, list[str]],
                 anonymous: bool = False) -> SynthDialect:
    style = str(rng.choice(["snake", "camel", "upper", "pascal", "kebab"]))
    nested = rng.random() < 0.6 and not anonymous
    numeric_strings = rng.random() < 0.15 or anonymous
    fields = ["vin", "ts", "seq", "lat", "lon", "heading", "speed", "odo", "soc", "fuel", "ambient",
              "ignition", "dtc", "evt"]
    drop = [f for f in ("heading", "fuel", "ambient", "evt", "dtc", "ignition", "soc") if rng.random() < 0.12]
    fields = [f for f in fields if f not in drop]
    unit: dict[str, str] = {}
    for f in fields:
        opts = UNITS.get(f)
        if opts:
            unit[f] = str(rng.choice(opts))
    if "lon" in fields:
        unit["lon"] = unit["lat"]
    ign_enc = str(rng.choice(["bool", "int", "onoff", "runstop", "str"]))
    dtc_enc = str(rng.choice(["list", "comma", "space", "semi"]))
    evt_enc = str(rng.choice(["name", "lower", "short", "num", "word"]))
    codes: dict[str, str] = {}
    num_step = int(rng.integers(1, 4))       # one step per dialect, so numeric codes never collide
    for i, e in enumerate(EVENT_TYPES):
        if evt_enc == "name":
            codes[e] = e
        elif evt_enc == "lower":
            codes[e] = e.lower()
        elif evt_enc == "short":
            codes[e] = "".join(w[0] for w in e.split("_")) + str(i)
        elif evt_enc == "num":
            codes[e] = str(10 + i * num_step)
        else:
            codes[e] = "E" + "".join(rng.choice(list("ABCDEFGHJKLMNPQRSTUVWXYZ"), size=5))

    path: dict[str, str] = {}
    used: set[str] = set()
    k = 0
    order = list(fields)
    n_dis = int(rng.integers(3, 9))
    dis = [str(d) for d in rng.choice(list(DISTRACTOR_ALIASES), size=n_dis, replace=False)]
    everything = order + dis
    rng.shuffle(everything)
    for f in everything:
        if anonymous:
            p = f"{('c', 'f', 'col', 'field')[k % 4 if False else 0]}{k}"
            p = f"c{k}"
        else:
            base = str(rng.choice(names[f] if f in names else DISTRACTOR_ALIASES[f]))
            p = _style(base, style)
            if nested:
                g = str(rng.choice(GROUPS.get(f, ["extra", "misc", "aux", ""])))
                if g:
                    p = f"{_style(g, style)}.{p}"
        while p in used:
            p += "2"
        used.add(p)
        path[f] = p
        k += 1

    labels = {path[f]: (f if f in ("vin", "seq", "ignition", "dtc", "evt") else f"{f}|{unit[f]}") for f in fields}
    for d in dis:
        labels[path[d]] = IGNORE

    def enc_num(v: Any, u: str) -> Any:
        if v is None:
            return None
        if u == "f":
            x = v * 1.8 + 32.0
        elif u == "k":
            x = v + 273.15
        elif u == "c":
            x = v
        elif u == "deci_c":
            x = v * 10.0
        else:
            x = v * _FACTOR[u]
        x = int(round(x)) if u in _INTEGER_UNITS else round(x, 4 if u in ("fraction", "deg") else 3)
        if u == "deg":
            x = round(v, 6)
        return str(x) if numeric_strings else x

    msgs: list[dict[str, Any]] = []
    devs: list[str] = []
    for tick, t in enumerate(rec.truth):
        dv = _distractors(rng, t, tick)
        for j in range(len(t["vin"])):
            if rng.random() < 0.03:
                continue                       # a lost message
            flat: dict[str, Any] = {}
            for f in fields:
                if f == "vin":
                    v: Any = t["vin"][j]
                elif f == "seq":
                    v = str(t["seq"][j]) if numeric_strings else t["seq"][j]
                elif f == "ts":
                    ms = t["ts"][j]
                    u = unit["ts"]
                    v = ms if u == "epoch_ms" else ms / 1000.0 if u == "epoch_s" else ms * 1000 if u == "epoch_us" else iso(ms)
                    if numeric_strings and u != "iso8601":
                        v = str(v)
                elif f == "ignition":
                    b = t["ignition"][j]
                    v = b if ign_enc == "bool" else int(b) if ign_enc == "int" else \
                        ("ON" if b else "OFF") if ign_enc == "onoff" else \
                        ("RUN" if b else "STOP") if ign_enc == "runstop" else ("true" if b else "false")
                elif f == "dtc":
                    c = t["dtc"][j]
                    v = list(c) if dtc_enc == "list" else {"comma": ",", "space": " ", "semi": ";"}[dtc_enc].join(c)
                elif f == "evt":
                    e = t["evt"][j]
                    v = None if e is None else codes[e]
                else:
                    key = {"heading": "heading_deg", "speed": "speed_kmh", "odo": "odo_km", "soc": "soc_pct",
                           "fuel": "fuel_pct", "ambient": "ambient_c"}.get(f, f)
                    v = enc_num(t[key][j], unit[f])
                if v is not None:
                    flat[path[f]] = v
            for d in dis:
                flat[path[d]] = dv[d][j]
            obj: dict[str, Any] = {}
            for p, v in flat.items():
                cur = obj
                parts = p.split(".")
                for part in parts[:-1]:
                    cur = cur.setdefault(part, {})
                cur[parts[-1]] = v
            msgs.append(obj)
            devs.append(t["device_id"][j])
    return SynthDialect(msgs, devs, labels, {v: k for k, v in codes.items()})


def make_corpus(n_dialects: int, seed: int, test: bool, log: Any | None = None) -> list[SynthDialect]:
    rng = np.random.default_rng(seed)
    banned = demo_tokens()
    names = TEST_NAMES if test else _clean(TRAIN_NAMES, banned)
    if test:
        seen = {name_tokens(n) for lst in TRAIN_NAMES.values() for n in lst}
        clash = sorted(n for lst in TEST_NAMES.values() for n in lst if name_tokens(n) in seen)
        if clash:
            raise ValueError(f"test names also used in training: {clash}")
    recs = [record(seed * 100 + r, r + (3 if test else 0)) for r in range(len(REGIONS))]
    out = []
    for i in range(n_dialects):
        rec = recs[int(rng.integers(len(recs)))]
        out.append(make_dialect(rec, rng, names, anonymous=rng.random() < 0.12))
        if log and (i + 1) % 100 == 0:
            log(f"  generated {i + 1}/{n_dialects} dialects")
    return out
