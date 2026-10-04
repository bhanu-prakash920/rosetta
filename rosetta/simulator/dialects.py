"""Six OEM dialects. Each one encodes the same vehicle state in its own way.

| source     | wire format                 | units and quirks                                   |
|------------|-----------------------------|----------------------------------------------------|
| nordvik    | nested JSON                 | metric, ISO-8601 time, camelCase names             |
| pacifica   | flat JSON                   | mph, miles, Fahrenheit, epoch seconds, DTC string  |
| stellaris  | pipe-delimited text         | m/s, metres, micro-degrees, per-mille, deci-C      |
| kaizen     | Protobuf (binary)           | 1e-7 degrees, centi-km/h, hectometres, micro-secs  |
| voltaic    | JSON name/value signal list | m/s, fraction of charge, Kelvin                    |
| helix      | nested JSON, German names   | m/s, metres, fraction, Kelvin (the "unknown" OEM)  |

`pacifica_v2` is the same brand after an over-the-air update that renamed two
fields and silently switched them to metric. It drives the format-drift demo.

Every dialect also ships its ground-truth mapping spec. Five of them seed the
registry. The helix and pacifica_v2 specs are never loaded by the running
system: they exist only so tests can score what the agent proposes.
"""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import orjson

from ..domain.canonical import EVENT_TYPES

Encoder = Callable[[dict[str, list]], list[bytes]]

_dumps = orjson.dumps


def _u(quantity: str, src: str) -> dict[str, str]:
    return {"op": "unit", "quantity": quantity, "from": src}


_ISO_CACHE: dict[int, str] = {}


def iso(ts_ms: int) -> str:
    s = _ISO_CACHE.get(ts_ms)
    if s is None:
        if len(_ISO_CACHE) > 4096:
            _ISO_CACHE.clear()
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        s = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}Z"
        _ISO_CACHE[ts_ms] = s
    return s


# ----------------------------------------------------------------- nordvik
def encode_nordvik(t: dict[str, list]) -> list[bytes]:
    return [
        _dumps({
            "vehicle": {"vin": vin, "deviceId": dev},
            "recordedAt": iso(ts),
            "sequence": seq,
            "position": {"latitude": lat, "longitude": lon, "heading": hdg},
            "motion": {"speedKmh": spd, "odometerKm": odo},
            "energy": {"stateOfCharge": soc, "fuelLevel": fuel},
            "environment": {"outsideTempC": tmp},
            "ignition": ign,
            "diagnostics": {"troubleCodes": dtc},
            "event": evt,
        })
        for vin, dev, ts, seq, lat, lon, hdg, spd, odo, soc, fuel, tmp, ign, dtc, evt in zip(
            t["vin"], t["device_id"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"],
            t["speed_kmh"], t["odo_km"], t["soc_pct"], t["fuel_pct"], t["ambient_c"],
            t["ignition"], t["dtc"], t["evt"])
    ]


SPEC_NORDVIK = {
    "oem": "nordvik",
    "decoder": {"type": "json"},
    "fields": {
        "vin": {"path": "vehicle.vin"},
        "ts": {"path": "recordedAt", "transforms": ["iso8601"]},
        "seq": {"path": "sequence"},
        "lat": {"path": "position.latitude"},
        "lon": {"path": "position.longitude"},
        "heading_deg": {"path": "position.heading"},
        "speed_kmh": {"path": "motion.speedKmh"},
        "odo_km": {"path": "motion.odometerKm"},
        "soc_pct": {"path": "energy.stateOfCharge"},
        "fuel_pct": {"path": "energy.fuelLevel"},
        "ambient_c": {"path": "environment.outsideTempC"},
        "ignition": {"path": "ignition", "transforms": ["to_bool"]},
        "dtc": {"path": "diagnostics.troubleCodes", "transforms": ["dtc_extract"]},
        "evt": {"path": "event"},
    },
}

# ---------------------------------------------------------------- pacifica
_PAC_EVT = {"HARSH_BRAKE": "HB", "HARSH_ACCEL": "HA", "SPEEDING": "SPD", "IDLE": "IDL",
            "IGNITION_ON": "IGN1", "IGNITION_OFF": "IGN0", "LOW_SOC": "LSOC",
            "CHARGE_START": "CHG1", "CHARGE_STOP": "CHG0"}
_PAC_EVT_INV = {v: k for k, v in _PAC_EVT.items()}


def encode_pacifica(t: dict[str, list]) -> list[bytes]:
    out = []
    for vin, ts, seq, lat, lon, hdg, spd, odo, soc, fuel, tmp, ign, dtc, evt in zip(
            t["vin"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"], t["speed_kmh"],
            t["odo_km"], t["soc_pct"], t["fuel_pct"], t["ambient_c"], t["ignition"], t["dtc"], t["evt"]):
        d = {"VIN": vin, "ts": ts / 1000.0, "msg_no": seq, "lat": lat, "lng": lon, "hdg": hdg,
             "speed_mph": round(spd / 1.609344, 3), "odometer_mi": round(odo / 1.609344, 4),
             "oat_f": round(tmp * 1.8 + 32.0, 2), "ign": "ON" if ign else "OFF",
             "dtcs": ",".join(dtc)}
        if soc is not None:
            d["batt_pct"] = soc
        if fuel is not None:
            d["fuel_pct"] = fuel
        if evt is not None:
            d["evt_code"] = _PAC_EVT[evt]
        out.append(_dumps(d))
    return out


SPEC_PACIFICA = {
    "oem": "pacifica",
    "decoder": {"type": "json"},
    "fields": {
        "vin": {"path": "VIN"},
        "ts": {"path": "ts", "transforms": [_u("time", "epoch_s")]},
        "seq": {"path": "msg_no"},
        "lat": {"path": "lat"},
        "lon": {"path": "lng"},
        "heading_deg": {"path": "hdg"},
        "speed_kmh": {"path": "speed_mph", "transforms": [_u("speed", "mph")]},
        "odo_km": {"path": "odometer_mi", "transforms": [_u("distance", "mi")]},
        "soc_pct": {"path": "batt_pct"},
        "fuel_pct": {"path": "fuel_pct"},
        "ambient_c": {"path": "oat_f", "transforms": [_u("temperature", "f")]},
        "ignition": {"path": "ign", "transforms": ["to_bool"]},
        "dtc": {"path": "dtcs", "transforms": ["dtc_extract"]},
        "evt": {"path": "evt_code", "transforms": [{"op": "enum", "map": _PAC_EVT_INV}]},
    },
}


def encode_pacifica_v2(t: dict[str, list]) -> list[bytes]:
    """After the OTA update: speed_mph -> speed (km/h), odometer_mi -> odometer (km)."""
    out = []
    for vin, ts, seq, lat, lon, hdg, spd, odo, soc, fuel, tmp, ign, dtc, evt in zip(
            t["vin"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"], t["speed_kmh"],
            t["odo_km"], t["soc_pct"], t["fuel_pct"], t["ambient_c"], t["ignition"], t["dtc"], t["evt"]):
        d = {"VIN": vin, "ts": ts / 1000.0, "msg_no": seq, "lat": lat, "lng": lon, "hdg": hdg,
             "speed": spd, "odometer": odo, "oat_f": round(tmp * 1.8 + 32.0, 2),
             "ign": "ON" if ign else "OFF", "dtcs": ",".join(dtc), "fw": "8.2.0"}
        if soc is not None:
            d["batt_pct"] = soc
        if fuel is not None:
            d["fuel_pct"] = fuel
        if evt is not None:
            d["evt_code"] = _PAC_EVT[evt]
        out.append(_dumps(d))
    return out


SPEC_PACIFICA_V2 = {
    "oem": "pacifica",
    "decoder": {"type": "json"},
    "fields": {**SPEC_PACIFICA["fields"],
               "speed_kmh": {"path": "speed"},
               "odo_km": {"path": "odometer"}},
}

# --------------------------------------------------------------- stellaris
_STL_COLS = ["tag", "ver", "vin", "t_ms", "ctr", "lat_ud", "lon_ud", "hdg", "v_mps", "odo_m",
             "soc_pm", "fuel_pm", "t_dc", "ign", "codes", "ev"]
_STL_EVT = {name: str(i + 1) for i, name in enumerate(EVENT_TYPES)}
_STL_EVT_INV = {v: k for k, v in _STL_EVT.items()}


def encode_stellaris(t: dict[str, list]) -> list[bytes]:
    out = []
    for vin, ts, seq, lat, lon, hdg, spd, odo, soc, fuel, tmp, ign, dtc, evt in zip(
            t["vin"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"], t["speed_kmh"],
            t["odo_km"], t["soc_pct"], t["fuel_pct"], t["ambient_c"], t["ignition"], t["dtc"], t["evt"]):
        out.append("|".join((
            "STL", "2", vin, str(ts), str(seq),
            str(int(round(lat * 1e6))), str(int(round(lon * 1e6))), f"{hdg:.1f}",
            f"{spd / 3.6:.3f}", str(int(round(odo * 1000))),
            "" if soc is None else str(int(round(soc * 10))),
            "" if fuel is None else str(int(round(fuel * 10))),
            str(int(round(tmp * 10))), "1" if ign else "0",
            " ".join(dtc), "0" if evt is None else _STL_EVT[evt],
        )).encode())
    return out


SPEC_STELLARIS = {
    "oem": "stellaris",
    "decoder": {"type": "delimited", "delimiter": "|", "columns": _STL_COLS},
    "fields": {
        "vin": {"path": "vin"},
        "ts": {"path": "t_ms", "transforms": ["to_int"]},
        "seq": {"path": "ctr", "transforms": ["to_int"]},
        "lat": {"path": "lat_ud", "transforms": [_u("angle", "microdeg")]},
        "lon": {"path": "lon_ud", "transforms": [_u("angle", "microdeg")]},
        "heading_deg": {"path": "hdg", "transforms": ["to_float"]},
        "speed_kmh": {"path": "v_mps", "transforms": [_u("speed", "mps")]},
        "odo_km": {"path": "odo_m", "transforms": [_u("distance", "m")]},
        "soc_pct": {"path": "soc_pm", "transforms": [_u("percent", "permille")]},
        "fuel_pct": {"path": "fuel_pm", "transforms": [_u("percent", "permille")]},
        "ambient_c": {"path": "t_dc", "transforms": [_u("temperature", "deci_c")]},
        "ignition": {"path": "ign", "transforms": ["to_bool"]},
        "dtc": {"path": "codes", "transforms": ["dtc_extract"]},
        "evt": {"path": "ev", "transforms": [{"op": "enum", "map": _STL_EVT_INV}]},
    },
}


# ------------------------------------------------------------------ kaizen
def _kaizen_descriptor() -> bytes:
    from google.protobuf import descriptor_pb2 as pb

    F = pb.FieldDescriptorProto
    fd = pb.FileDescriptorProto(name="kaizen/v1/telemetry.proto", package="kaizen.v1", syntax="proto3")

    pos = fd.message_type.add(name="Position")
    pos.field.add(name="lat_e7", number=1, type=F.TYPE_SINT32, label=F.LABEL_OPTIONAL)
    pos.field.add(name="lon_e7", number=2, type=F.TYPE_SINT32, label=F.LABEL_OPTIONAL)
    pos.field.add(name="heading_cdeg", number=3, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL)

    m = fd.message_type.add(name="Telemetry")
    m.field.add(name="vin", number=1, type=F.TYPE_STRING, label=F.LABEL_OPTIONAL)
    m.field.add(name="time_us", number=2, type=F.TYPE_INT64, label=F.LABEL_OPTIONAL)
    m.field.add(name="counter", number=3, type=F.TYPE_UINT64, label=F.LABEL_OPTIONAL)
    m.field.add(name="pos", number=4, type=F.TYPE_MESSAGE, label=F.LABEL_OPTIONAL, type_name=".kaizen.v1.Position")
    m.field.add(name="speed_centi_kmh", number=5, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL)
    m.field.add(name="odometer_hm", number=6, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL)
    for i, (name, num) in enumerate((("soc_permille", 7), ("fuel_permille", 8))):
        m.oneof_decl.add(name=f"_{name}")
        m.field.add(name=name, number=num, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL,
                    oneof_index=i, proto3_optional=True)
    m.field.add(name="ambient_deci_c", number=9, type=F.TYPE_SINT32, label=F.LABEL_OPTIONAL)
    m.field.add(name="ignition_on", number=10, type=F.TYPE_BOOL, label=F.LABEL_OPTIONAL)
    m.field.add(name="dtc", number=11, type=F.TYPE_STRING, label=F.LABEL_REPEATED)
    m.field.add(name="event", number=12, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL)
    return pb.FileDescriptorSet(file=[fd]).SerializeToString()


KAIZEN_DESCRIPTOR_B64 = base64.b64encode(_kaizen_descriptor()).decode()
_KZ_EVT = {name: i + 1 for i, name in enumerate(EVENT_TYPES)}
_kaizen_cls: Any = None


def encode_kaizen(t: dict[str, list]) -> list[bytes]:
    global _kaizen_cls
    if _kaizen_cls is None:
        from ..engine.decoders import build_message_class
        _kaizen_cls = build_message_class(KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Telemetry")
    cls = _kaizen_cls
    out = []
    for vin, ts, seq, lat, lon, hdg, spd, odo, soc, fuel, tmp, ign, dtc, evt in zip(
            t["vin"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"], t["speed_kmh"],
            t["odo_km"], t["soc_pct"], t["fuel_pct"], t["ambient_c"], t["ignition"], t["dtc"], t["evt"]):
        m = cls(vin=vin, time_us=ts * 1000, counter=seq,
                speed_centi_kmh=int(round(spd * 100)), odometer_hm=int(round(odo * 10)),
                ambient_deci_c=int(round(tmp * 10)), ignition_on=ign, dtc=dtc,
                event=0 if evt is None else _KZ_EVT[evt])
        m.pos.lat_e7 = int(round(lat * 1e7))
        m.pos.lon_e7 = int(round(lon * 1e7))
        m.pos.heading_cdeg = int(round(hdg * 100)) % 36000
        if soc is not None:
            m.soc_permille = int(round(soc * 10))
        if fuel is not None:
            m.fuel_permille = int(round(fuel * 10))
        out.append(m.SerializeToString())
    return out


SPEC_KAIZEN = {
    "oem": "kaizen",
    "decoder": {"type": "protobuf", "message": "kaizen.v1.Telemetry", "descriptor_b64": KAIZEN_DESCRIPTOR_B64},
    "fields": {
        "vin": {"path": "vin"},
        "ts": {"path": "time_us", "transforms": [_u("time", "epoch_us")]},
        "seq": {"path": "counter"},
        "lat": {"path": "pos.lat_e7", "transforms": [_u("angle", "e7")]},
        "lon": {"path": "pos.lon_e7", "transforms": [_u("angle", "e7")]},
        "heading_deg": {"path": "pos.heading_cdeg", "transforms": [{"op": "scale", "factor": 0.01}]},
        "speed_kmh": {"path": "speed_centi_kmh", "transforms": [_u("speed", "centi_kmh")]},
        "odo_km": {"path": "odometer_hm", "transforms": [_u("distance", "hm")]},
        "soc_pct": {"path": "soc_permille", "transforms": [_u("percent", "permille")]},
        "fuel_pct": {"path": "fuel_permille", "transforms": [_u("percent", "permille")]},
        "ambient_c": {"path": "ambient_deci_c", "transforms": [_u("temperature", "deci_c")]},
        "ignition": {"path": "ignition_on"},
        "dtc": {"path": "dtc", "transforms": ["dtc_extract"]},
        "evt": {"path": "event", "transforms": [{"op": "enum", "map": {str(v): k for k, v in _KZ_EVT.items()}}]},
    },
}


# ----------------------------------------------------------------- voltaic
def encode_voltaic(t: dict[str, list]) -> list[bytes]:
    out = []
    for vin, ts, seq, lat, lon, hdg, spd, odo, soc, tmp, ign, dtc, evt in zip(
            t["vin"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"], t["speed_kmh"],
            t["odo_km"], t["soc_pct"], t["ambient_c"], t["ignition"], t["dtc"], t["evt"]):
        sig = [
            {"k": "gnss_lat", "v": lat}, {"k": "gnss_lon", "v": lon}, {"k": "gnss_course", "v": hdg},
            {"k": "veh_speed", "v": round(spd / 3.6, 3)}, {"k": "veh_odo", "v": odo},
            {"k": "env_temp", "v": round(tmp + 273.15, 2)}, {"k": "pwr_state", "v": "RUN" if ign else "OFF"},
            {"k": "diag_codes", "v": dtc},
        ]
        if soc is not None:
            sig.append({"k": "bat_level", "v": round(soc / 100.0, 4)})
        if evt is not None:
            sig.append({"k": "drv_event", "v": evt.lower()})
        out.append(_dumps({"meta": {"vin": vin, "sent": ts, "ctr": seq, "schema": "vt.3"}, "signals": sig}))
    return out


SPEC_VOLTAIC = {
    "oem": "voltaic",
    "decoder": {"type": "json", "pivot": {"path": "signals", "key": "k", "value": "v", "into": "sig"}},
    "fields": {
        "vin": {"path": "meta.vin"},
        "ts": {"path": "meta.sent"},
        "seq": {"path": "meta.ctr"},
        "lat": {"path": "sig.gnss_lat"},
        "lon": {"path": "sig.gnss_lon"},
        "heading_deg": {"path": "sig.gnss_course"},
        "speed_kmh": {"path": "sig.veh_speed", "transforms": [_u("speed", "mps")]},
        "odo_km": {"path": "sig.veh_odo"},
        "soc_pct": {"path": "sig.bat_level", "transforms": [_u("percent", "fraction")]},
        "ambient_c": {"path": "sig.env_temp", "transforms": [_u("temperature", "k")]},
        "ignition": {"path": "sig.pwr_state", "transforms": ["to_bool"]},
        "dtc": {"path": "sig.diag_codes", "transforms": ["dtc_extract"]},
        "evt": {"path": "sig.drv_event", "transforms": ["upper"]},
    },
}

# ------------------------------------------------------------------- helix
_HX_EVT = {"HARSH_BRAKE": "VOLLBREMSUNG", "HARSH_ACCEL": "KICKDOWN", "SPEEDING": "TEMPO",
           "IDLE": "LEERLAUF", "IGNITION_ON": "START", "IGNITION_OFF": "STOPP",
           "LOW_SOC": "AKKU_LEER", "CHARGE_START": "LADEN_AN", "CHARGE_STOP": "LADEN_AUS"}


def encode_helix(t: dict[str, list]) -> list[bytes]:
    out = []
    for vin, ts, seq, lat, lon, hdg, spd, odo, soc, fuel, tmp, ign, dtc, evt in zip(
            t["vin"], t["ts"], t["seq"], t["lat"], t["lon"], t["heading_deg"], t["speed_kmh"],
            t["odo_km"], t["soc_pct"], t["fuel_pct"], t["ambient_c"], t["ignition"], t["dtc"], t["evt"]):
        d: dict[str, Any] = {
            "kopf": {"fin": vin, "zeit": ts, "lfd_nr": seq, "fw": "HX-4.1"},
            "ort": {"breite": lat, "laenge": lon, "kurs": hdg},
            "fahrt": {"v": round(spd / 3.6, 3), "strecke": int(round(odo * 1000))},
            "klima": {"aussen": round(tmp + 273.15, 2)},
            "zuendung": 1 if ign else 0,
            "fehler": ";".join(dtc),
        }
        if soc is not None:
            d["akku"] = {"ladung": round(soc / 100.0, 4)}
        if fuel is not None:
            d["tank"] = {"stand": round(fuel / 100.0, 4)}
        if evt is not None:
            d["ereignis"] = _HX_EVT[evt]
        out.append(_dumps(d))
    return out


SPEC_HELIX = {
    "oem": "helix",
    "decoder": {"type": "json"},
    "fields": {
        "vin": {"path": "kopf.fin"},
        "ts": {"path": "kopf.zeit"},
        "seq": {"path": "kopf.lfd_nr"},
        "lat": {"path": "ort.breite"},
        "lon": {"path": "ort.laenge"},
        "heading_deg": {"path": "ort.kurs"},
        "speed_kmh": {"path": "fahrt.v", "transforms": [_u("speed", "mps")]},
        "odo_km": {"path": "fahrt.strecke", "transforms": [_u("distance", "m")]},
        "soc_pct": {"path": "akku.ladung", "transforms": [_u("percent", "fraction")]},
        "fuel_pct": {"path": "tank.stand", "transforms": [_u("percent", "fraction")]},
        "ambient_c": {"path": "klima.aussen", "transforms": [_u("temperature", "k")]},
        "ignition": {"path": "zuendung", "transforms": ["to_bool"]},
        "dtc": {"path": "fehler", "transforms": ["dtc_extract"]},
        "evt": {"path": "ereignis", "transforms": [{"op": "enum", "map": {v: k for k, v in _HX_EVT.items()}}]},
    },
}


@dataclass(frozen=True)
class Dialect:
    key: str
    oem: str
    content_type: str
    encode: Encoder
    spec: dict[str, Any]
    seeded: bool  # True when the platform already knows this dialect at start-up


DIALECTS: dict[str, Dialect] = {
    "nordvik": Dialect("nordvik", "nordvik", "application/json", encode_nordvik, SPEC_NORDVIK, True),
    "pacifica": Dialect("pacifica", "pacifica", "application/json", encode_pacifica, SPEC_PACIFICA, True),
    "stellaris": Dialect("stellaris", "stellaris", "text/plain", encode_stellaris, SPEC_STELLARIS, True),
    "kaizen": Dialect("kaizen", "kaizen", "application/x-protobuf", encode_kaizen, SPEC_KAIZEN, True),
    "voltaic": Dialect("voltaic", "voltaic", "application/json", encode_voltaic, SPEC_VOLTAIC, True),
    "helix": Dialect("helix", "helix", "application/json", encode_helix, SPEC_HELIX, False),
    "pacifica_v2": Dialect("pacifica_v2", "pacifica", "application/json", encode_pacifica_v2, SPEC_PACIFICA_V2, False),
}

# Canonical fields a golden comparison checks, with the tolerance allowed after
# a round trip through the dialect's rounding.
TOLERANCE = {"lat": 2e-6, "lon": 2e-6, "heading_deg": 0.06, "speed_kmh": 0.02, "odo_km": 0.06,
             "soc_pct": 0.06, "fuel_pct": 0.06, "ambient_c": 0.06}


def expected_events(t: dict[str, list], oem: str) -> list[dict[str, Any]]:
    """The canonical events a perfect mapping must produce for `t` (the golden answers)."""
    out = []
    n = len(t["vin"])
    for j in range(n):
        ev = {"vin": t["vin"][j], "ts": t["ts"][j], "seq": t["seq"][j], "lat": t["lat"][j],
              "lon": t["lon"][j], "heading_deg": t["heading_deg"][j], "speed_kmh": t["speed_kmh"][j],
              "odo_km": t["odo_km"][j], "ambient_c": t["ambient_c"][j], "ignition": t["ignition"][j],
              "dtc": list(t["dtc"][j])}
        for k in ("soc_pct", "fuel_pct", "evt"):
            if t[k][j] is not None:
                ev[k] = t[k][j]
        if oem == "voltaic":
            ev.pop("fuel_pct", None)
        out.append(ev)
    return out
