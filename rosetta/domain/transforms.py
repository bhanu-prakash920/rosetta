"""Whitelisted value transforms used by mapping specs.

A mapping spec is data, never code. Each transform is a named operation with
validated parameters, compiled here into a plain Python callable. The AI agent
can only choose from this list, which is the core guardrail of the system: a
proposed mapping cannot execute anything that is not defined in this file.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .dtc import extract_dtcs

Transform = Callable[[Any], Any]


class TransformError(ValueError):
    """The spec asked for an operation or parameter that is not allowed."""


# unit -> (factor, offset) so that canonical = raw * factor + offset
UNITS: dict[str, dict[str, tuple[float, float]]] = {
    "speed": {  # canonical km/h
        "kmh": (1.0, 0.0),
        "mph": (1.609344, 0.0),
        "mps": (3.6, 0.0),
        "knots": (1.852, 0.0),
        "cm_s": (0.036, 0.0),
        "centi_kmh": (0.01, 0.0),
    },
    "distance": {  # canonical km
        "km": (1.0, 0.0),
        "mi": (1.609344, 0.0),
        "m": (0.001, 0.0),
        "hm": (0.1, 0.0),
    },
    "temperature": {  # canonical Celsius
        "c": (1.0, 0.0),
        "f": (5.0 / 9.0, -160.0 / 9.0),
        "k": (1.0, -273.15),
        "deci_c": (0.1, 0.0),
    },
    "percent": {  # canonical 0..100
        "pct": (1.0, 0.0),
        "fraction": (100.0, 0.0),
        "permille": (0.1, 0.0),
        "byte": (100.0 / 255.0, 0.0),
    },
    "angle": {  # canonical degrees
        "deg": (1.0, 0.0),
        "microdeg": (1e-6, 0.0),
        "e7": (1e-7, 0.0),
        "rad": (180.0 / math.pi, 0.0),
    },
    "time": {  # canonical epoch milliseconds
        "epoch_ms": (1.0, 0.0),
        "epoch_s": (1000.0, 0.0),
        "epoch_us": (0.001, 0.0),
    },
}

CANONICAL_QUANTITY = {
    "speed_kmh": "speed",
    "odo_km": "distance",
    "ambient_c": "temperature",
    "soc_pct": "percent",
    "fuel_pct": "percent",
    "lat": "angle",
    "lon": "angle",
    "heading_deg": "angle",
    "ts": "time",
}

_TRUTHY = frozenset(("1", "true", "t", "yes", "y", "on", "run", "running"))
_FALSY = frozenset(("0", "false", "f", "no", "n", "off", "stop", "stopped", ""))


def _num(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("bool is not a number")
    if isinstance(v, (int, float)):
        return v
    return float(str(v).strip())


def _iso_to_ms(v: Any) -> int:
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(round(dt.timestamp() * 1000))


def _finite(name: str, x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise TransformError(f"{name} must be a finite number")
    return float(x)


def compile_transform(spec: Any) -> Transform:
    """Turn one transform spec (a string or a dict with "op") into a callable."""
    if isinstance(spec, str):
        spec = {"op": spec}
    if not isinstance(spec, dict) or "op" not in spec:
        raise TransformError(f"transform must be a name or an object with 'op': {spec!r}")
    if not isinstance(spec["op"], str):
        raise TransformError(f"unknown transform op {spec['op']!r}")
    op = spec["op"]
    extra = set(spec) - {"op"} - _ALLOWED_PARAMS.get(op, set())
    if op not in _ALLOWED_PARAMS:
        raise TransformError(f"unknown transform op {op!r}")
    if extra:
        raise TransformError(f"transform {op!r} does not accept {sorted(extra)}")

    if op == "unit":
        quantity, src = spec.get("quantity"), spec.get("from")
        if not isinstance(quantity, str) or not isinstance(src, str) or quantity not in UNITS or src not in UNITS[quantity]:
            raise TransformError(f"unknown unit {quantity!r}/{src!r}")
        factor, offset = UNITS[quantity][src]
        if quantity == "time":
            return lambda v: int(round(_num(v) * factor))
        if factor == 1.0 and offset == 0.0:
            return lambda v: float(_num(v))
        return lambda v: _num(v) * factor + offset
    if op == "scale":
        factor = _finite("factor", spec.get("factor"))
        return lambda v: _num(v) * factor
    if op == "affine":
        factor = _finite("factor", spec.get("factor"))
        offset = _finite("offset", spec.get("offset", 0.0))
        return lambda v: _num(v) * factor + offset
    if op == "iso8601":
        return _iso_to_ms
    if op == "to_int":
        return lambda v: int(round(_num(v)))
    if op == "to_float":
        return lambda v: float(_num(v))
    if op == "to_str":
        return lambda v: str(v)
    if op == "to_bool":
        def to_bool(v: Any) -> bool:
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in _TRUTHY:
                return True
            if s in _FALSY:
                return False
            return bool(_num(v))
        return to_bool
    if op == "upper":
        return lambda v: str(v).upper()
    if op == "strip":
        return lambda v: str(v).strip()
    if op == "split":
        sep = spec.get("sep", ",")
        if not isinstance(sep, str) or not 1 <= len(sep) <= 4:
            raise TransformError("split.sep must be a 1-4 char string")
        return lambda v: [p for p in str(v).split(sep)] if v not in (None, "") else []
    if op == "index":
        i = spec.get("i")
        if isinstance(i, bool) or not isinstance(i, int) or not -64 <= i <= 64:
            raise TransformError("index.i must be a small integer")
        return lambda v: v[i]
    if op == "dtc_extract":
        return extract_dtcs
    if op == "enum":
        mapping = spec.get("map")
        if not isinstance(mapping, dict) or len(mapping) > 256:
            raise TransformError("enum.map must be an object with at most 256 entries")
        table = {str(k): val for k, val in mapping.items()}
        default = spec.get("default")
        return lambda v: table.get(str(v), default)
    if op == "round":
        nd = spec.get("digits", 0)
        if isinstance(nd, bool) or not isinstance(nd, int) or not 0 <= nd <= 9:
            raise TransformError("round.digits must be 0..9")
        return lambda v: round(_num(v), nd)
    raise TransformError(f"unknown transform op {op!r}")  # pragma: no cover


_ALLOWED_PARAMS: dict[str, set[str]] = {
    "unit": {"quantity", "from"},
    "scale": {"factor"},
    "affine": {"factor", "offset"},
    "iso8601": set(),
    "to_int": set(),
    "to_float": set(),
    "to_str": set(),
    "to_bool": set(),
    "upper": set(),
    "strip": set(),
    "split": {"sep"},
    "index": {"i"},
    "dtc_extract": set(),
    "enum": {"map", "default"},
    "round": {"digits"},
}

ALLOWED_OPS = tuple(sorted(_ALLOWED_PARAMS))
