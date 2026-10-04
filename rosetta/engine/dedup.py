"""Two-stage de-duplication: exact for recent events, Bloom-guarded for late ones.

Stage 1  ReplayWindow: per vehicle, the last 64 sequence numbers. Exact, O(1).
Stage 2  Events older than the window ("stale") are rare. A Bloom filter says
         "definitely new" for almost all of them without touching the exact
         store. Only when the filter says "maybe seen" do we consult the exact
         set, so a false positive costs one lookup and never loses an event.
"""
from __future__ import annotations

from typing import Protocol

from ..algorithms.bloom import RotatingBloom
from ..algorithms.replay_window import DUPLICATE, NEW, ReplayWindow


class ExactStore(Protocol):
    """Exact membership for late events. In-memory locally, Redis in production."""

    def add_if_absent(self, key: str) -> bool: ...   # True when the key was absent (and is now stored)


class MemoryExactStore:
    def __init__(self, max_items: int = 2_000_000) -> None:
        self._items: dict[str, None] = {}
        self._max = max_items

    def add_if_absent(self, key: str) -> bool:
        if key in self._items:
            return False
        if len(self._items) >= self._max:  # drop the oldest tenth; dict keeps insertion order
            for k in list(self._items)[: self._max // 10]:
                del self._items[k]
        self._items[key] = None
        return True

    def __len__(self) -> int:
        return len(self._items)


class Deduplicator:
    __slots__ = ("window", "bloom", "exact", "late_seen", "bloom_hits", "exact_lookups")

    def __init__(self, exact: ExactStore | None = None, late_capacity: int = 2_000_000,
                 fp_rate: float = 0.01) -> None:
        self.window = ReplayWindow()
        self.bloom = RotatingBloom(late_capacity, fp_rate)
        self.exact = exact if exact is not None else MemoryExactStore()
        self.late_seen = 0
        self.bloom_hits = 0
        self.exact_lookups = 0

    def is_duplicate(self, vin: str, seq: int) -> bool:
        verdict = self.window.check_and_set(vin, seq)
        if verdict == NEW:
            return False
        if verdict == DUPLICATE:
            return True
        # STALE: older than the window, take the late path
        self.late_seen += 1
        key = f"{vin}:{seq}"
        maybe_seen = self.bloom.add(key.encode())
        if not maybe_seen:
            self.exact.add_if_absent(key)
            return False
        self.bloom_hits += 1
        self.exact_lookups += 1
        return not self.exact.add_if_absent(key)

    def snapshot(self) -> dict:
        return {"window": self.window.snapshot()}

    def restore(self, snap: dict) -> None:
        self.window.restore(snap.get("window", {}))
