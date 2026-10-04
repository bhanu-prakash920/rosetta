"""Geohash: turns (lat, lon) into a short string where nearby points share a prefix.

Used to index vehicles for map queries and to mask locations for privacy:
cutting a geohash to fewer characters makes the cell bigger and the position
less precise (5 chars is about 4.9 km, 4 chars about 39 km).

encode: O(precision). encode_many: vectorised with numpy for whole batches.
"""
from __future__ import annotations

import numpy as np

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(BASE32)}
_B32 = np.frombuffer(BASE32.encode(), dtype="S1")

# approximate cell size (km) by precision, at the equator: (width, height)
CELL_KM = {1: (5009.4, 4992.6), 2: (1252.3, 624.1), 3: (156.5, 156.0), 4: (39.1, 19.5),
           5: (4.9, 4.9), 6: (1.2, 0.61), 7: (0.153, 0.153), 8: (0.038, 0.019)}


def encode(lat: float, lon: float, precision: int = 7) -> str:
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ValueError("lat/lon out of range")
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out = []
    bit, ch, even = 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def decode_bbox(gh: str) -> tuple[float, float, float, float]:
    """Return (lat_lo, lat_hi, lon_lo, lon_hi) of the cell."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for c in gh:
        v = _DECODE[c]
        for shift in (4, 3, 2, 1, 0):
            b = (v >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                if b:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if b:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def decode(gh: str) -> tuple[float, float]:
    """Centre of the cell."""
    a, b, c, d = decode_bbox(gh)
    return (a + b) / 2, (c + d) / 2


def mask(lat: float, lon: float, precision: int = 5) -> tuple[float, float, str]:
    """Privacy masking: snap a position to the centre of its geohash cell."""
    gh = encode(lat, lon, precision)
    la, lo = decode(gh)
    return la, lo, gh


def encode_many(lat: np.ndarray, lon: np.ndarray, precision: int = 5) -> np.ndarray:
    """Vectorised encode. Returns an array of fixed-width byte strings."""
    nbits = precision * 5
    lon_bits = (nbits + 1) // 2
    lat_bits = nbits // 2
    lo = np.clip(((np.asarray(lon, dtype=np.float64) + 180.0) / 360.0 * (1 << lon_bits)).astype(np.int64),
                 0, (1 << lon_bits) - 1)
    la = np.clip(((np.asarray(lat, dtype=np.float64) + 90.0) / 180.0 * (1 << lat_bits)).astype(np.int64),
                 0, (1 << lat_bits) - 1)
    code = np.zeros(lo.shape, dtype=np.int64)
    for i in range(nbits):  # interleave, longitude first
        if i % 2 == 0:
            src, pos = lo, lon_bits - 1 - i // 2
        else:
            src, pos = la, lat_bits - 1 - i // 2
        code = (code << 1) | ((src >> pos) & 1)
    chars = np.empty(lo.shape + (precision,), dtype="S1")
    for j in range(precision):
        chars[..., j] = _B32[(code >> (5 * (precision - 1 - j))) & 31]
    return chars.view(f"S{precision}").reshape(lo.shape)


def neighbors(gh: str) -> list[str]:
    """The 8 cells around a cell (fewer at the poles). Used for radius queries."""
    lat_lo, lat_hi, lon_lo, lon_hi = decode_bbox(gh)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    clat, clon = (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2
    out = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            la = clat + dy * dlat
            if not -90.0 <= la <= 90.0:
                continue
            lo = (clon + dx * dlon + 180.0) % 360.0 - 180.0
            out.append(encode(la, lo, len(gh)))
    return sorted(set(out) - {gh})
