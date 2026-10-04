"""Which mapping version handles a message: canary routing, fallback and stickiness.

An OEM source can have several live versions at the same time. That is normal:
after an over-the-air update half the fleet speaks the new format while the
other half has not updated yet. For each message the router builds an ordered
list of adapters to try:

  1. the version that last worked for this device (sticky, so the common case
     is a single attempt),
  2. the canary version, when the device falls inside the canary percentage,
  3. every active version, newest first,
  4. the canary as a last resort, for messages nothing else could read.

The canary share is chosen by hashing the device id, so a vehicle is either in
the canary group or not. It never flips between versions from one message to
the next.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import xxhash

from .compiler import Adapter, compile_spec

ST_DRAFT, ST_VALIDATED, ST_CANARY, ST_ACTIVE = "draft", "validated", "canary", "active"
ST_RETIRED, ST_REJECTED = "retired", "rejected"
LIVE_STATES = (ST_CANARY, ST_ACTIVE)
ALL_STATES = (ST_DRAFT, ST_VALIDATED, ST_CANARY, ST_ACTIVE, ST_RETIRED, ST_REJECTED)


@dataclass(frozen=True)
class MappingRow:
    oem: str
    version: int
    state: str
    spec: dict[str, Any]
    canary_pct: int = 0


def canary_bucket(device_key: bytes) -> int:
    """Stable 0..99 bucket for a device."""
    return xxhash.xxh3_64_intdigest(device_key, seed=0x5EED) % 100


@dataclass
class OemRoutes:
    oem: str
    active: list[Adapter] = field(default_factory=list)   # newest first
    canary: Adapter | None = None
    canary_pct: int = 0
    by_version: dict[int, Adapter] = field(default_factory=dict)
    single: Adapter | None = None   # set when exactly one version is live: the fast path

    def candidates(self, device_key: bytes, sticky_version: int | None) -> list[Adapter]:
        out: list[Adapter] = []
        if sticky_version is not None:
            a = self.by_version.get(sticky_version)
            if a is not None:
                out.append(a)
        if self.canary is not None and self.canary not in out and canary_bucket(device_key) < self.canary_pct:
            out.insert(0, self.canary) if sticky_version is None else out.append(self.canary)
        for a in self.active:
            if a not in out:
                out.append(a)
        # Last resort: a message that no active version understands would be dead
        # anyway, so letting the canary try it cannot make things worse.
        if self.canary is not None and self.canary not in out:
            out.append(self.canary)
        return out


class RoutingTable:
    """Immutable snapshot of all live adapters. Swapped atomically on registry change."""

    def __init__(self, rows: Iterable[MappingRow], epoch: int = 0) -> None:
        self.epoch = epoch
        self.routes: dict[str, OemRoutes] = {}
        self.errors: list[str] = []
        for row in sorted(rows, key=lambda r: (r.oem, -r.version)):
            if row.state not in LIVE_STATES:
                continue
            try:
                adapter = compile_spec(row.oem, row.version, row.spec)
            except Exception as e:  # noqa: BLE001
                # A bad spec must never take the worker down. Skip it and report.
                self.errors.append(f"{row.oem} v{row.version}: {e}")
                continue
            r = self.routes.setdefault(row.oem, OemRoutes(row.oem))
            r.by_version[row.version] = adapter
            if row.state == ST_ACTIVE:
                r.active.append(adapter)
            elif r.canary is None:
                r.canary = adapter
                r.canary_pct = max(0, min(100, int(row.canary_pct)))

        for r in self.routes.values():
            if len(r.by_version) == 1 and r.canary is None:
                r.single = r.active[0]

    def for_oem(self, oem: str) -> OemRoutes | None:
        return self.routes.get(oem)

    def describe(self) -> dict[str, Any]:
        return {
            oem: {
                "active": [a.version for a in r.active],
                "canary": r.canary.version if r.canary else None,
                "canary_pct": r.canary_pct,
            }
            for oem, r in self.routes.items()
        }

    def _unused(self) -> None:  # pragma: no cover
        pass
