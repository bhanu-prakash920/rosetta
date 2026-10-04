"""The baseline the model has to beat: map fields by how their names look.

This is what a careful engineer would script in an afternoon: compare each
source field name with the canonical names and a short alias list, accept the
best match above a threshold, and read the unit from a suffix in the name.
It never looks at the values.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .labels import IGNORE
from .profile import name_tokens

ALIASES = {
    "vin": ["vin", "vehicle id"],
    "ts": ["ts", "timestamp", "time"],
    "seq": ["seq", "sequence", "counter"],
    "lat": ["lat", "latitude"],
    "lon": ["lon", "lng", "longitude"],
    "heading": ["heading", "course", "bearing"],
    "speed": ["speed", "velocity"],
    "odo": ["odo", "odometer", "mileage"],
    "soc": ["soc", "battery", "state of charge"],
    "fuel": ["fuel", "fuel level"],
    "ambient": ["ambient", "temperature", "outside temp"],
    "ignition": ["ignition", "engine on"],
    "dtc": ["dtc", "trouble codes", "fault codes"],
    "evt": ["event", "evt"],
}
DEFAULT_UNIT = {"ts": "epoch_ms", "lat": "deg", "lon": "deg", "heading": "deg", "speed": "kmh",
                "odo": "km", "soc": "pct", "fuel": "pct", "ambient": "c"}
SUFFIX_UNIT = {
    "speed": {"mph": "mph", "kmh": "kmh", "kph": "kmh", "mps": "mps", "ms": "mps", "knots": "knots", "kn": "knots"},
    "odo": {"mi": "mi", "miles": "mi", "km": "km", "m": "m"},
    "ambient": {"f": "f", "c": "c", "k": "k"},
    "lat": {"e7": "e7"}, "lon": {"e7": "e7"},
    "soc": {"pm": "permille"}, "fuel": {"pm": "permille"},
}
THRESHOLD = 0.72


def score(path: str) -> dict[str, float]:
    """Best name similarity of `path` against each canonical field, 0..1."""
    toks = name_tokens(path)
    last = toks.split()[-2:] if toks else []
    words = toks.split()
    cands = {toks, " ".join(last), last[-1] if last else ""}
    if len(words) >= 2:                       # "speed mph" -> also try "speed"
        cands |= {" ".join(words[:-1]), words[-2]}
    out = {}
    for f, aliases in ALIASES.items():
        best = 0.0
        for a in aliases:
            for c in cands:
                if c:
                    best = max(best, SequenceMatcher(None, a, c).ratio())
        out[f] = best
    return out


def predict(path: str) -> str:
    s = score(path)
    f = max(s, key=s.get)
    if s[f] < THRESHOLD:
        return IGNORE
    if f not in DEFAULT_UNIT:
        return f
    unit = DEFAULT_UNIT[f]
    for tok in name_tokens(path).split():
        unit = SUFFIX_UNIT.get(f, {}).get(tok, unit)
    return f"{f}|{unit}"
