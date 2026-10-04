"""Bloom filter: a compact set that answers "definitely not seen" or "maybe seen".

Used as the first stage of late-event de-duplication so that most lookups never
touch the exact store. Sized from the expected item count n and target false
positive rate p:  m = -n ln p / (ln 2)^2 bits,  k = (m / n) ln 2 hashes.

add / contains: O(k) time. Space: m bits. No false negatives, ever.
"""
from __future__ import annotations

import math

import xxhash


class BloomFilter:
    __slots__ = ("m", "k", "bits", "count")

    def __init__(self, capacity: int, fp_rate: float = 0.01) -> None:
        if capacity <= 0 or not 0.0 < fp_rate < 1.0:
            raise ValueError("capacity must be > 0 and 0 < fp_rate < 1")
        m = int(math.ceil(-capacity * math.log(fp_rate) / (math.log(2) ** 2)))
        self.m = max(64, m)
        self.k = max(1, int(round(self.m / capacity * math.log(2))))
        self.bits = bytearray((self.m + 7) // 8)
        self.count = 0

    def _positions(self, key: bytes):
        # Kirsch-Mitzenmacher double hashing: k positions from two 64-bit hashes.
        h = xxhash.xxh3_128_intdigest(key)
        h1, h2 = h & 0xFFFFFFFFFFFFFFFF, (h >> 64) | 1
        m = self.m
        return [(h1 + i * h2) % m for i in range(self.k)]

    def add(self, key: bytes) -> bool:
        """Insert. Returns True when the key was possibly already present."""
        bits = self.bits
        present = True
        for pos in self._positions(key):
            byte, mask = pos >> 3, 1 << (pos & 7)
            if not bits[byte] & mask:
                present = False
                bits[byte] |= mask
        if not present:
            self.count += 1
        return present

    def __contains__(self, key: bytes) -> bool:
        bits = self.bits
        for pos in self._positions(key):
            if not bits[pos >> 3] & (1 << (pos & 7)):
                return False
        return True

    def estimated_fp_rate(self) -> float:
        return (1.0 - math.exp(-self.k * self.count / self.m)) ** self.k

    @property
    def size_bytes(self) -> int:
        return len(self.bits)


class RotatingBloom:
    """Two Bloom filters that rotate, so memory stays bounded on an endless stream.

    A key is "maybe seen" when either generation has it. After `capacity`
    inserts the older generation is dropped, which gives every key a lifetime
    between one and two generations.
    """

    __slots__ = ("capacity", "fp_rate", "current", "previous")

    def __init__(self, capacity: int, fp_rate: float = 0.01) -> None:
        self.capacity = capacity
        self.fp_rate = fp_rate
        self.current = BloomFilter(capacity, fp_rate)
        self.previous = BloomFilter(capacity, fp_rate)

    def __contains__(self, key: bytes) -> bool:
        return key in self.current or key in self.previous

    def add(self, key: bytes) -> bool:
        seen = key in self.previous
        seen = self.current.add(key) or seen
        if self.current.count >= self.capacity:
            self.previous = self.current
            self.current = BloomFilter(self.capacity, self.fp_rate)
        return seen
