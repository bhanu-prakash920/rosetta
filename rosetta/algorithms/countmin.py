"""Count-Min Sketch and a top-K tracker built on it.

Count-Min estimates how often each key appeared using a small fixed table:
    width  w = ceil(e / eps)      depth  d = ceil(ln(1 / delta))
The estimate never undercounts, and overcounts by at most eps * N with
probability 1 - delta, where N is the total count added.

add / estimate: O(d). Space: O(w * d) counters, independent of distinct keys.
"""
from __future__ import annotations

import heapq
import math

import numpy as np
import xxhash


class CountMinSketch:
    __slots__ = ("width", "depth", "table", "total")

    def __init__(self, eps: float = 0.001, delta: float = 0.01) -> None:
        if not 0 < eps < 1 or not 0 < delta < 1:
            raise ValueError("eps and delta must be in (0, 1)")
        self.width = int(math.ceil(math.e / eps))
        self.depth = int(math.ceil(math.log(1.0 / delta)))
        self.table = np.zeros((self.depth, self.width), dtype=np.int64)
        self.total = 0

    def _cols(self, key: str):
        h = xxhash.xxh3_128_intdigest(key)
        h1, h2 = h & 0xFFFFFFFFFFFFFFFF, (h >> 64) | 1
        w = self.width
        return [(h1 + i * h2) % w for i in range(self.depth)]

    def add(self, key: str, count: int = 1) -> int:
        """Add and return the new estimate for `key`."""
        est = None
        t = self.table
        for row, col in enumerate(self._cols(key)):
            v = int(t[row, col]) + count
            t[row, col] = v
            est = v if est is None or v < est else est
        self.total += count
        return est or 0

    def estimate(self, key: str) -> int:
        t = self.table
        return int(min(t[row, col] for row, col in enumerate(self._cols(key))))

    def merge(self, other: CountMinSketch) -> None:
        """Sketches from different workers add together: that is what makes them scale."""
        if (self.width, self.depth) != (other.width, other.depth):
            raise ValueError("sketches must have the same shape")
        self.table += other.table
        self.total += other.total


class TopK:
    """Heavy hitters: the K most frequent keys of a stream, in O(log K) per update."""

    __slots__ = ("k", "sketch", "_heap", "_members")

    def __init__(self, k: int = 10, eps: float = 0.001, delta: float = 0.01) -> None:
        self.k = k
        self.sketch = CountMinSketch(eps, delta)
        self._heap: list[tuple[int, str]] = []
        self._members: dict[str, int] = {}

    def add(self, key: str, count: int = 1) -> None:
        est = self.sketch.add(key, count)
        if key in self._members:
            self._members[key] = est
            return
        if len(self._members) < self.k:
            self._members[key] = est
            heapq.heappush(self._heap, (est, key))
            return
        # Lazy heap: drop stale entries until the root reflects a live minimum.
        while self._heap:
            c, kk = self._heap[0]
            live = self._members.get(kk)
            if live is None:
                heapq.heappop(self._heap)
            elif live != c:
                heapq.heapreplace(self._heap, (live, kk))
            else:
                break
        if self._heap and est > self._heap[0][0]:
            _, evicted = heapq.heapreplace(self._heap, (est, key))
            self._members.pop(evicted, None)
            self._members[key] = est

    def items(self) -> list[tuple[str, int]]:
        return sorted(self._members.items(), key=lambda kv: (-kv[1], kv[0]))
