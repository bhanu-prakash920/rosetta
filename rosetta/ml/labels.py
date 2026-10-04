"""Label space of the field mapper: a canonical field plus how the source encodes it."""
from __future__ import annotations

from typing import Any

IGNORE = "ignore"

# label -> (canonical field, transforms that turn the source value into the canonical one)
LABELS: dict[str, tuple[str, list[Any]]] = {
    "vin": ("vin", ["strip", "upper"]),
    "seq": ("seq", ["to_int"]),
    "ts|epoch_ms": ("ts", [{"op": "unit", "quantity": "time", "from": "epoch_ms"}]),
    "ts|epoch_s": ("ts", [{"op": "unit", "quantity": "time", "from": "epoch_s"}]),
    "ts|epoch_us": ("ts", [{"op": "unit", "quantity": "time", "from": "epoch_us"}]),
    "ts|iso8601": ("ts", ["iso8601"]),
    "lat|deg": ("lat", ["to_float"]),
    "lat|microdeg": ("lat", [{"op": "unit", "quantity": "angle", "from": "microdeg"}]),
    "lat|e7": ("lat", [{"op": "unit", "quantity": "angle", "from": "e7"}]),
    "lon|deg": ("lon", ["to_float"]),
    "lon|microdeg": ("lon", [{"op": "unit", "quantity": "angle", "from": "microdeg"}]),
    "lon|e7": ("lon", [{"op": "unit", "quantity": "angle", "from": "e7"}]),
    "heading|deg": ("heading_deg", ["to_float"]),
    "heading|centideg": ("heading_deg", [{"op": "scale", "factor": 0.01}]),
    "speed|kmh": ("speed_kmh", [{"op": "unit", "quantity": "speed", "from": "kmh"}]),
    "speed|mph": ("speed_kmh", [{"op": "unit", "quantity": "speed", "from": "mph"}]),
    "speed|mps": ("speed_kmh", [{"op": "unit", "quantity": "speed", "from": "mps"}]),
    "speed|knots": ("speed_kmh", [{"op": "unit", "quantity": "speed", "from": "knots"}]),
    "speed|centi_kmh": ("speed_kmh", [{"op": "unit", "quantity": "speed", "from": "centi_kmh"}]),
    "odo|km": ("odo_km", [{"op": "unit", "quantity": "distance", "from": "km"}]),
    "odo|mi": ("odo_km", [{"op": "unit", "quantity": "distance", "from": "mi"}]),
    "odo|m": ("odo_km", [{"op": "unit", "quantity": "distance", "from": "m"}]),
    "odo|hm": ("odo_km", [{"op": "unit", "quantity": "distance", "from": "hm"}]),
    "soc|pct": ("soc_pct", [{"op": "unit", "quantity": "percent", "from": "pct"}]),
    "soc|fraction": ("soc_pct", [{"op": "unit", "quantity": "percent", "from": "fraction"}]),
    "soc|permille": ("soc_pct", [{"op": "unit", "quantity": "percent", "from": "permille"}]),
    "fuel|pct": ("fuel_pct", [{"op": "unit", "quantity": "percent", "from": "pct"}]),
    "fuel|fraction": ("fuel_pct", [{"op": "unit", "quantity": "percent", "from": "fraction"}]),
    "fuel|permille": ("fuel_pct", [{"op": "unit", "quantity": "percent", "from": "permille"}]),
    "ambient|c": ("ambient_c", [{"op": "unit", "quantity": "temperature", "from": "c"}]),
    "ambient|f": ("ambient_c", [{"op": "unit", "quantity": "temperature", "from": "f"}]),
    "ambient|k": ("ambient_c", [{"op": "unit", "quantity": "temperature", "from": "k"}]),
    "ambient|deci_c": ("ambient_c", [{"op": "unit", "quantity": "temperature", "from": "deci_c"}]),
    "ignition": ("ignition", ["to_bool"]),
    "dtc": ("dtc", ["dtc_extract"]),
    "evt": ("evt", []),
}
LABEL_NAMES = [IGNORE] + list(LABELS)
LABEL_INDEX = {n: i for i, n in enumerate(LABEL_NAMES)}
CANONICAL_OF = {k: v[0] for k, v in LABELS.items()}
CANONICAL_TARGETS = list(dict.fromkeys(CANONICAL_OF.values()))


def field_of(label: str) -> str:
    return CANONICAL_OF.get(label, IGNORE)


def unit_of(label: str) -> str:
    return label.split("|", 1)[1] if "|" in label else ""


def transforms_of(label: str) -> list[Any]:
    return [dict(t) if isinstance(t, dict) else t for t in LABELS[label][1]]
