"""The canonical telemetry event: the one format every OEM dialect is mapped to.

Events travel through the hot path as plain dicts (not pydantic models) because
the pipeline handles 100K+ events per second; validation is a hand-written
function that returns a reason code instead of raising.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dtc import DTC_RE
from .vin import is_valid_vin

SCHEMA_VERSION = 1

EVENT_TYPES = (
    "HARSH_BRAKE",
    "HARSH_ACCEL",
    "SPEEDING",
    "IDLE",
    "IGNITION_ON",
    "IGNITION_OFF",
    "LOW_SOC",
    "CHARGE_START",
    "CHARGE_STOP",
)
_EVENT_SET = frozenset(EVENT_TYPES)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str  # "str" | "int" | "float" | "bool" | "list[str]" | "enum"
    required: bool
    unit: str = ""
    lo: float | None = None
    hi: float | None = None
    description: str = ""
    pii: bool = False


CANONICAL_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("vin", "str", True, description="17-char vehicle identification number", pii=True),
    FieldSpec("ts", "int", True, "epoch_ms", 946684800000, 4102444800000, "event time, UTC milliseconds"),
    FieldSpec("seq", "int", True, "", 0, 2**53, "per-vehicle sequence number"),
    FieldSpec("lat", "float", True, "deg", -90.0, 90.0, "latitude", pii=True),
    FieldSpec("lon", "float", True, "deg", -180.0, 180.0, "longitude", pii=True),
    FieldSpec("speed_kmh", "float", True, "kmh", 0.0, 320.0, "ground speed"),
    FieldSpec("odo_km", "float", True, "km", 0.0, 3_000_000.0, "odometer"),
    FieldSpec("heading_deg", "float", False, "deg", 0.0, 360.0, "compass heading"),
    FieldSpec("soc_pct", "float", False, "pct", 0.0, 100.0, "battery state of charge"),
    FieldSpec("fuel_pct", "float", False, "pct", 0.0, 100.0, "fuel level"),
    FieldSpec("ambient_c", "float", False, "c", -70.0, 80.0, "outside temperature"),
    FieldSpec("ignition", "bool", False, description="engine / drivetrain on"),
    FieldSpec("dtc", "list[str]", False, description="diagnostic trouble codes"),
    FieldSpec("evt", "enum", False, description="driving event, one of EVENT_TYPES"),
)

FIELD_BY_NAME = {f.name: f for f in CANONICAL_FIELDS}
REQUIRED = tuple(f.name for f in CANONICAL_FIELDS if f.required)
OPTIONAL = tuple(f.name for f in CANONICAL_FIELDS if not f.required)
_RANGED = tuple((f.name, f.lo, f.hi, f.required) for f in CANONICAL_FIELDS if f.lo is not None)

# Reason codes. They are stable strings: dashboards, the DLQ and tests key on them.
# VINs that already passed the check digit. A fleet repeats the same VINs all day,
# so the 17-step checksum runs once per vehicle instead of once per event.
_VIN_OK: set[str] = set()
_VIN_CACHE_MAX = 2_000_000

R_MISSING = "MISSING_REQUIRED"
R_TYPE = "BAD_TYPE"
R_RANGE = "OUT_OF_RANGE"
R_VIN = "INVALID_VIN"
R_DTC = "INVALID_DTC"
R_EVT = "UNKNOWN_EVENT_TYPE"


def validate(ev: dict[str, Any]) -> tuple[str, str] | None:
    """Return None when `ev` is a valid canonical event, else (reason, field).

    Time O(f + d) for f fields and d trouble codes; no allocation on success.
    """
    for name in REQUIRED:
        if ev.get(name) is None:
            return (R_MISSING, name)
    vin = ev["vin"]
    if not isinstance(vin, str):
        return (R_VIN, "vin")
    if vin not in _VIN_OK:
        if not is_valid_vin(vin):
            return (R_VIN, "vin")
        if len(_VIN_OK) >= _VIN_CACHE_MAX:
            _VIN_OK.clear()
        _VIN_OK.add(vin)
    for name, lo, hi, _req in _RANGED:
        v = ev.get(name)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return (R_TYPE, name)
        if v != v or v < lo or v > hi:  # v != v catches NaN
            return (R_RANGE, name)
    if not isinstance(ev["ts"], int) or not isinstance(ev["seq"], int):
        return (R_TYPE, "ts" if not isinstance(ev["ts"], int) else "seq")
    ign = ev.get("ignition")
    if ign is not None and not isinstance(ign, bool):
        return (R_TYPE, "ignition")
    dtc = ev.get("dtc")
    if dtc:
        if not isinstance(dtc, list):
            return (R_TYPE, "dtc")
        for code in dtc:
            if not isinstance(code, str) or not DTC_RE.match(code):
                return (R_DTC, "dtc")
    evt = ev.get("evt")
    if evt is not None and evt not in _EVENT_SET:
        return (R_EVT, "evt")
    return None


def json_schema() -> dict[str, Any]:
    """JSON Schema for the canonical event, served by the schema registry."""
    kinds = {
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "list[str]": {"type": "array", "items": {"type": "string", "pattern": DTC_RE.pattern}},
        "enum": {"type": "string", "enum": list(EVENT_TYPES)},
    }
    props: dict[str, Any] = {}
    for f in CANONICAL_FIELDS:
        p = dict(kinds[f.kind])
        if f.lo is not None:
            p["minimum"], p["maximum"] = f.lo, f.hi
        if f.unit:
            p["x-unit"] = f.unit
        if f.pii:
            p["x-pii"] = True
        p["description"] = f.description
        props[f.name] = p
    props["vin"]["pattern"] = "^[A-HJ-NPR-Z0-9]{17}$"
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"rosetta/canonical-telemetry/v{SCHEMA_VERSION}",
        "title": "CanonicalTelemetryEvent",
        "type": "object",
        "properties": props,
        "required": list(REQUIRED),
        "additionalProperties": True,
    }
