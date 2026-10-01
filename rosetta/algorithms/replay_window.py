"""Exact duplicate detection with a per-vehicle sliding sequence window.

The same idea IPsec uses against replayed packets. For each vehicle we keep
the highest sequence number seen and a 64-bit mask of which of the previous 64
sequence numbers have arrived. That makes duplicate detection exact (no false
positives, so no data loss) while still accepting out-of-order events.

check_and_set: O(1) time. Space: 2 integers per vehicle (about 16 MB for 100K
vehicles in CPython, 1.6 MB with the numpy variant).
"""
from __future__ import annotations

from typing import Any

NEW = 0        # first time we see this sequence number
DUPLICATE = 1  # seen before, inside the window
STALE = 2      # older than the window: caller must use the late-event path

WINDOW = 64
_MASK = (1 << WINDOW) - 1


class ReplayWindow:
    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, int]] = {}

    def __len__(self) -> int:
        return len(self._state)

    def check_and_set(self, key: str, seq: int) -> int:
        st = self._state.get(key)
        if st is None:
            self._state[key] = (seq, 1)
            return NEW
        top, mask = st
        if seq > top:
            shift = seq - top
            mask = ((mask << shift) | 1) & _MASK if shift < WINDOW else 1
            self._state[key] = (seq, mask)
            return NEW
        back = top - seq
        if back >= WINDOW:
            return STALE
        bit = 1 << back
        if mask & bit:
            return DUPLICATE
        self._state[key] = (top, mask | bit)
        return NEW

    def reset(self, key: str) -> None:
        """Forget a vehicle. Used when a device legitimately restarts its counter."""
        self._state.pop(key, None)

    def snapshot(self) -> dict[str, tuple[int, int]]:
        return dict(self._state)

    def restore(self, snap: dict[str, tuple[int, int]]) -> None:
        self._state = {k: (int(v[0]), int(v[1])) for k, v in snap.items()}


class BatchReplayWindow:
    """The same window, vectorised with numpy for vehicles known by index.

    Used where whole batches are handled at once (the stream processor). State is
    two arrays: the highest sequence number per vehicle and a 64-bit mask of the
    sequence numbers seen just below it.

    check_batch: O(b log b) for a batch of b events (one sort), O(n) memory.
    """

    def __init__(self, n: int) -> None:
        import numpy as np

        self.np = np
        self.top = np.full(n, -1, dtype=np.int64)
        self.mask = np.zeros(n, dtype=np.uint64)

    def snapshot(self) -> dict[str, Any]:
        """Both arrays, copied. The pair is the whole state: nothing else is needed."""
        return {"top": self.top.copy(), "mask": self.mask.copy()}

    def restore(self, snap: dict[str, Any]) -> bool:
        """Load a snapshot taken by this class. False when it does not fit this fleet.

        A SIGKILL takes `top` and `mask` with it, and a window that starts empty
        calls everything new until it has seen each vehicle once. Anything
        redelivered into that gap is accepted a second time. Restoring the exact
        pair keeps detection exact, which guessing cannot: the mark alone says
        nothing about which lower sequence numbers arrived, and assuming they all
        did would drop a late one that never did.
        """
        np = self.np
        top, mask = snap.get("top"), snap.get("mask")
        if top is None or mask is None or len(top) != self.top.size or len(mask) != self.mask.size:
            return False
        self.top = np.asarray(top, dtype=np.int64).copy()
        self.mask = np.asarray(mask, dtype=np.uint64).copy()
        return True

    def check_batch(self, idx, seq):
        """Return a boolean array: True where the event is new, False where it is a duplicate."""
        np = self.np
        n = idx.size
        keep = np.ones(n, dtype=bool)
        if n == 0:
            return keep
        order = np.lexsort((seq, idx))
        i_s, s_s = idx[order], seq[order]
        same = np.zeros(n, dtype=bool)
        same[1:] = (i_s[1:] == i_s[:-1]) & (s_s[1:] == s_s[:-1])     # repeated inside the batch
        back = self.top[i_s] - s_s
        inwin = (back >= 0) & (back < WINDOW) & ~same
        if inwin.any():
            bits = np.left_shift(np.uint64(1), back[inwin].astype(np.uint64))
            seen = (self.mask[i_s[inwin]] & bits) != 0
            dup = np.zeros(n, dtype=bool)
            dup[np.flatnonzero(inwin)[seen]] = True
            same |= dup
            fresh = inwin & ~dup
            if fresh.any():
                np.bitwise_or.at(self.mask, i_s[fresh],
                                 np.left_shift(np.uint64(1), back[fresh].astype(np.uint64)))
        adv = (back < 0) & ~same
        if adv.any():
            ai, as_ = i_s[adv], s_s[adv]
            # sorted by (idx, seq): the last row of each vehicle holds its new top
            last = np.ones(ai.size, dtype=bool)
            last[:-1] = ai[1:] != ai[:-1]
            veh, new_top = ai[last], as_[last]
            shift = new_top - self.top[veh]
            m = self.mask[veh]
            small = shift < WINDOW
            m = np.where(small, np.left_shift(m, np.where(small, shift, 0).astype(np.uint64)), np.uint64(0))
            self.mask[veh] = m
            self.top[veh] = new_top
            pos = self.top[ai] - as_
            ok = pos < WINDOW
            np.bitwise_or.at(self.mask, ai[ok], np.left_shift(np.uint64(1), pos[ok].astype(np.uint64)))
        keep[order] = ~same
        return keep
