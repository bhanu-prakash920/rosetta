"""OBD-II Diagnostic Trouble Code parsing.

A DTC looks like P0301: a system letter (P powertrain, C chassis, B body,
U network), a digit 0-3, then three hex digits. OEM payloads carry them in
many shapes: lists, comma strings, free text, lower case. `extract_dtcs`
pulls every code out of any of those. Time O(n) in the input length.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

DTC_RE = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")
_DTC_SCAN = re.compile(r"(?<![A-Z0-9])([PCBU][0-3][0-9A-F]{3})(?![A-Z0-9])")

SYSTEMS = {"P": "powertrain", "C": "chassis", "B": "body", "U": "network"}


def is_valid_dtc(code: object) -> bool:
    return isinstance(code, str) and bool(DTC_RE.match(code))


def extract_dtcs(raw: Any) -> list[str]:
    """Return the unique DTCs found in `raw`, in first-seen order."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple, set)):
        parts: Iterable[Any] = raw
        text = " ".join(str(p) for p in parts)
    else:
        text = str(raw)
    seen: dict[str, None] = {}
    for m in _DTC_SCAN.finditer(text.upper()):
        seen.setdefault(m.group(1), None)
    return list(seen)


def describe(code: str) -> dict[str, str]:
    """Small decoder used by the UI: system and whether the code is generic."""
    if not is_valid_dtc(code):
        raise ValueError(f"not a DTC: {code!r}")
    return {
        "code": code,
        "system": SYSTEMS[code[0]],
        "scope": "generic" if code[1] in "02" else "manufacturer",
    }
