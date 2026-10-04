"""Sliding-window aggregates and a latency histogram for stream metrics.

SlidingWindow keeps one bucket per second in a ring buffer, so "events in the
last 60 s" is O(window) to read and O(1) to update, with O(window) memory no
matter how many events arrive.

LatencyHistogram uses log-spaced buckets (HDR style): record is O(1), any
percentile is O(buckets), relative error is bounded by the bucket growth.
"""
from __future__ import annotations

import math

import numpy as np


class SlidingWindow:
    __slots__ = ("size", "_buckets", "_stamp")

    def __init__(self, seconds: int = 60) -> None:
        self.size = seconds
        self._buckets = np.zeros(seconds, dtype=np.float64)
        self._stamp = np.full(seconds, -1, dtype=np.int64)

    def add(self, ts_s: int, value: float = 1.0) -> None:
        i = ts_s % self.size
        if self._stamp[i] > ts_s:
            return      # older than the window: its slot already holds a newer second
        if self._stamp[i] != ts_s:
            self._stamp[i] = ts_s
            self._buckets[i] = 0.0
        self._buckets[i] += value

    def total(self, now_s: int) -> float:
        live = (self._stamp > now_s - self.size) & (self._stamp <= now_s)
        return float(self._buckets[live].sum())

    def rate(self, now_s: int) -> float:
        """Average per second over the window."""
        return self.total(now_s) / self.size

    def series(self, now_s: int) -> list[float]:
        """Oldest to newest, one value per second, zero where nothing arrived."""
        out = []
        for t in range(now_s - self.size + 1, now_s + 1):
            i = t % self.size
            out.append(float(self._buckets[i]) if self._stamp[i] == t else 0.0)
        return out


class LatencyHistogram:
    """Percentiles over a stream without keeping the samples."""

    __slots__ = ("_lo", "_growth", "_log_growth", "counts", "n", "max", "sum")

    def __init__(self, lo_ms: float = 0.01, hi_ms: float = 120_000.0, growth: float = 1.08) -> None:
        self._lo = lo_ms
        self._growth = growth
        self._log_growth = math.log(growth)
        nb = int(math.ceil(math.log(hi_ms / lo_ms) / self._log_growth)) + 2
        self.counts = np.zeros(nb, dtype=np.int64)
        self.n = 0
        self.max = 0.0
        self.sum = 0.0

    def _bucket(self, ms: float) -> int:
        if ms <= self._lo:
            return 0
        return min(len(self.counts) - 1, int(math.log(ms / self._lo) / self._log_growth) + 1)

    def record(self, ms: float, count: int = 1) -> None:
        self.counts[self._bucket(ms)] += count
        self.n += count
        self.sum += ms * count
        if ms > self.max:
            self.max = ms

    def record_many(self, ms: np.ndarray) -> None:
        if ms.size == 0:
            return
        x = np.maximum(ms.astype(np.float64), self._lo)
        idx = np.minimum(len(self.counts) - 1,
                         (np.log(x / self._lo) / self._log_growth).astype(np.int64) + 1)
        idx[ms <= self._lo] = 0
        self.counts += np.bincount(idx, minlength=len(self.counts))
        self.n += int(ms.size)
        self.sum += float(ms.sum())
        self.max = max(self.max, float(ms.max()))

    def percentile(self, p: float) -> float:
        if self.n == 0:
            return 0.0
        target = max(1, int(math.ceil(self.n * p / 100.0)))
        cum = np.cumsum(self.counts)
        b = int(np.searchsorted(cum, target))
        upper = self._lo * (self._growth ** b)
        return min(upper, self.max) if self.max else upper

    def mean(self) -> float:
        return self.sum / self.n if self.n else 0.0

    def merge(self, other: LatencyHistogram) -> None:
        self.counts += other.counts
        self.n += other.n
        self.sum += other.sum
        self.max = max(self.max, other.max)

    def reset(self) -> None:
        self.counts[:] = 0
        self.n = 0
        self.max = 0.0
        self.sum = 0.0
