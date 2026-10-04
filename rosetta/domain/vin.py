"""VIN validation (ISO 3779 shape + North American check digit).

A VIN is 17 characters, never contains I, O or Q, and position 9 is a check
digit computed from the other 16. Time O(17) = O(1), space O(1).
"""
from __future__ import annotations

import re

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

_TRANSLIT = {c: i for i, c in enumerate("0123456789")}
_TRANSLIT.update(
    dict(zip("ABCDEFGH", (1, 2, 3, 4, 5, 6, 7, 8)))
)
_TRANSLIT.update(dict(zip("JKLMN", (1, 2, 3, 4, 5))))
_TRANSLIT.update({"P": 7, "R": 9})
_TRANSLIT.update(dict(zip("STUVWXYZ", (2, 3, 4, 5, 6, 7, 8, 9))))

_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
VIN_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def check_digit(vin17: str) -> str:
    """Return the expected check digit for a 17-char VIN (position 9 is ignored)."""
    total = 0
    for ch, w in zip(vin17, _WEIGHTS):
        total += _TRANSLIT[ch] * w
    r = total % 11
    return "X" if r == 10 else str(r)


def is_valid_vin(vin: object) -> bool:
    """True when the VIN has the right shape and its check digit matches."""
    if not isinstance(vin, str) or len(vin) != 17 or not VIN_RE.match(vin):
        return False
    return check_digit(vin) == vin[8]


def with_check_digit(vin17: str) -> str:
    """Return the VIN with position 9 replaced by the correct check digit."""
    return vin17[:8] + check_digit(vin17) + vin17[9:]


def make_vin(wmi: str, serial: int, descriptor: str = "EV1A2", year: str = "T", plant: str = "A") -> str:
    """Build a syntactically valid VIN for the simulator.

    wmi: 3-char world manufacturer id, serial: 0..999999.
    """
    if len(wmi) != 3 or len(descriptor) != 5:
        raise ValueError("wmi must be 3 chars and descriptor 5 chars")
    body = f"{wmi}{descriptor}0{year}{plant}{serial % 1_000_000:06d}"
    return with_check_digit(body)
