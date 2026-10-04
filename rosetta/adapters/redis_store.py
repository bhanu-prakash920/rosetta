"""Redis-backed exact store for late-event de-duplication.

SET key NX EX is one atomic command: it stores the key only when it is absent
and tells us which case it was. Keys expire, so memory stays bounded.
"""
from __future__ import annotations

from typing import Any


class RedisExactStore:
    def __init__(self, url: str, ttl_s: int = 172_800, client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(url)
        self.r = client
        self.ttl_s = ttl_s

    def add_if_absent(self, key: str) -> bool:
        return bool(self.r.set(f"rosetta:late:{key}", b"1", nx=True, ex=self.ttl_s))
