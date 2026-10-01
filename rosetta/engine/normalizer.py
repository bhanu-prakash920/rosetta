"""The normaliser: raw OEM payloads in, canonical events and dead letters out.

This class is pure logic. It knows nothing about Kafka, files or databases: it
takes a batch of records and returns what should be produced. That keeps it
testable and lets the same code run in the local mode and in production.

Per record: route -> decode -> map -> validate -> de-duplicate.
Time per record O(f * t) for f fields and t transforms, O(1) extra for dedup.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import orjson

from ..domain import errors as E
from ..domain.errors import NormalizeError
from ..ports.broker import Record
from .decoders import MAX_PAYLOAD_BYTES
from .dedup import Deduplicator
from .router import RoutingTable

_dumps = orjson.dumps
FALLBACK_REASONS = frozenset((E.SCHEMA_MISMATCH, E.DECODE_ERROR, E.TRANSFORM_ERROR))
CANARY_FAULTS = frozenset((E.INVALID, E.TRANSFORM_ERROR))
MAX_STICKY = 2_000_000


@dataclass
class BatchStats:
    received: int = 0
    bytes_in: int = 0
    ok: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    failed: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    failed_field: dict[tuple[str, str, str], int] = field(default_factory=lambda: defaultdict(int))
    canary_failed: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    duplicates: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    fallbacks: int = 0
    replayed_ok: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "bytes_in": self.bytes_in,
            "ok": [[o, v, n] for (o, v), n in self.ok.items()],
            "failed": [[o, r, n] for (o, r), n in self.failed.items()],
            "failed_field": [[o, r, f, n] for (o, r, f), n in self.failed_field.items()],
            "canary_failed": [[o, v, n] for (o, v), n in self.canary_failed.items()],
            "duplicates": dict(self.duplicates),
            "fallbacks": self.fallbacks,
            "replayed_ok": self.replayed_ok,
        }


@dataclass
class BatchResult:
    canonical: list[Record]
    dead: list[Record]
    stats: BatchStats


class Normalizer:
    def __init__(self, table: RoutingTable | None = None, dedup: Deduplicator | None = None) -> None:
        self.table = table or RoutingTable([])
        self.dedup = dedup or Deduplicator()
        self._sticky: dict[bytes, int] = {}

    def swap_table(self, table: RoutingTable) -> None:
        """Hot reload. One attribute assignment, so in-flight batches finish on the old table."""
        self.table = table
        self._sticky.clear()

    def process(self, records: list[Record], now_ms: int | None = None) -> BatchResult:
        table = self.table
        dedup = self.dedup
        sticky = self._sticky
        stats = BatchStats()
        out: list[Record] = []
        dead: list[Record] = []
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        lat = stats.latency_ms

        for rec in records:
            stats.received += 1
            payload = rec.value
            stats.bytes_in += len(payload)
            h = rec.headers
            oem = h.get("oem", "unknown")

            if len(payload) > MAX_PAYLOAD_BYTES:
                dead.append(_dead(rec, oem, E.OVERSIZE, "", f"{len(payload)} bytes", 0, now))
                stats.failed[(oem, E.OVERSIZE)] += 1
                continue
            routes = table.routes.get(oem)
            if routes is None or not routes.by_version:
                dead.append(_dead(rec, oem, E.NO_ADAPTER, "", "no live mapping for this source", 0, now))
                stats.failed[(oem, E.NO_ADAPTER)] += 1
                continue

            key = rec.key
            ev = None
            first: NormalizeError | None = None
            first_v = 0
            single = routes.single
            if single is not None:
                # One live version: no routing decision to make.
                try:
                    ev = single.normalize(payload)
                except NormalizeError as e:
                    first, first_v = e, single.version
            else:
              for n, adapter in enumerate(routes.candidates(key, sticky.get(key))):
                  try:
                      ev = adapter.normalize(payload)
                  except NormalizeError as e:
                      # SCHEMA_MISMATCH or DECODE_ERROR only say "this payload is another
                      # format". A canary that reads a payload and produces something invalid
                      # is what the rollback guard must see.
                      if adapter is routes.canary and e.reason in CANARY_FAULTS:
                          stats.canary_failed[(oem, adapter.version)] += 1
                      if first is None:
                          first, first_v = e, adapter.version
                      if e.reason not in FALLBACK_REASONS:
                          break
                      continue
                  if n:
                      stats.fallbacks += 1
                  if sticky.get(key) != adapter.version:
                      if len(sticky) >= MAX_STICKY:
                          sticky.clear()
                      sticky[key] = adapter.version
                  break

            if ev is None:
                assert first is not None
                dead.append(_dead(rec, oem, first.reason, first.field, first.detail, first_v, now))
                stats.failed[(oem, first.reason)] += 1
                stats.failed_field[(oem, first.reason, first.field)] += 1
                continue

            if dedup.is_duplicate(ev["vin"], ev["seq"]):
                stats.duplicates[oem] += 1
                continue

            rx = h.get("rx", now)
            ev["rx_ts"] = rx
            ev["norm_ts"] = now
            if h.get("replay"):
                # A replayed message waited in the dead-letter queue, possibly for hours.
                # That wait is not pipeline latency, so it stays out of the histogram.
                ev["replayed"] = True
                stats.replayed_ok += 1
            else:
                lat.append(now - rx)
            stats.ok[(oem, ev["map_v"])] += 1
            out.append(Record(key=ev["vin"].encode(), value=_dumps(ev),
                              headers={"oem": oem, "v": ev["map_v"]}))
        return BatchResult(out, dead, stats)


def _dead(rec: Record, oem: str, reason: str, fld: str, detail: str, version: int, now: int) -> Record:
    h = dict(rec.headers)
    h.update({"oem": oem, "reason": reason, "field": fld, "detail": detail[:160],
              "tried_v": version, "dead_ts": now, "dev": rec.key.decode("utf-8", "replace"),
              "attempts": int(rec.headers.get("attempts", 0)) + 1})
    return Record(key=oem.encode(), value=rec.value, headers=h)
