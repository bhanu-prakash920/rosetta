"""Normaliser worker: the process that runs the engine against the raw topic.

Loop: poll -> normalise -> produce canonical + dead letters -> commit offsets.
Offsets are committed only after the outputs were produced, so a crash can
repeat work but can never lose an event (at-least-once). The de-duplication
state is checkpointed together with the offsets, so a restarted worker does
not let through events it had already seen.

Hot reload: the worker watches the registry epoch. When a mapping is approved,
promoted or rolled back, the epoch changes and the worker swaps in a freshly
compiled routing table between two batches. No restart, no dropped message.
"""
from __future__ import annotations

import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from ..algorithms.sliding_window import LatencyHistogram
from ..config import get_settings
from ..engine.dedup import Deduplicator
from ..engine.normalizer import BatchStats, Normalizer
from ..engine.router import RoutingTable
from ..factory import flush_broker, make_broker, make_exact_store
from ..observability import tracing
from ..ports.broker import T_CANONICAL, T_DLQ, T_RAW
from . import metrics


def load_table() -> RoutingTable:
    from ..db.session import session_scope
    from ..services import registry

    with session_scope() as s:
        return RoutingTable(registry.live_rows(s), registry.current_epoch(s))


def read_epoch() -> int:
    from ..db.session import session_scope
    from ..services import registry

    with session_scope() as s:
        return registry.current_epoch(s)


def _merge(total: BatchStats, part: BatchStats) -> None:
    total.received += part.received
    total.bytes_in += part.bytes_in
    total.fallbacks += part.fallbacks
    total.replayed_ok += part.replayed_ok
    for name in ("ok", "failed", "failed_field", "canary_failed", "duplicates"):
        dst, src = getattr(total, name), getattr(part, name)
        for k, v in src.items():
            dst[k] += v


class NormalizerWorker:
    def __init__(self, worker_id: int = 0, workers: int = 1, broker: Any = None,
                 partitions: Sequence[int] | None = None, batch: int = 4000,
                 checkpoint_dir: Path | None = None, group: str = "normalizer") -> None:
        st = get_settings()
        self.id = worker_id
        self.broker = broker or make_broker(st)
        n = self.broker.partitions(T_RAW)
        if partitions is None and st.broker != "kafka":
            partitions = [p for p in range(n) if p % workers == worker_id]
        self.consumer = self.broker.consumer(T_RAW, group, partitions)
        self.batch = batch
        self.norm = Normalizer(load_table(), Deduplicator(exact=make_exact_store(st)))
        self.ckpt = (checkpoint_dir or st.data_dir / "checkpoints") / f"normalizer-{worker_id}.json"
        self.ckpt.parent.mkdir(parents=True, exist_ok=True)
        self._restore()
        self.running = True
        self.acc = BatchStats()
        self.hist = LatencyHistogram()
        self._last_pub = time.monotonic()
        self._last_epoch_check = 0.0
        self._last_ckpt = time.monotonic()
        self._avg_size = 250.0
        self.processed = 0

    # ------------------------------------------------------------- checkpoints
    def _restore(self) -> None:
        try:
            self.norm.dedup.restore(orjson.loads(self.ckpt.read_bytes()))
        except (FileNotFoundError, orjson.JSONDecodeError, TypeError, ValueError):
            pass      # no usable checkpoint: start with an empty window, the late path still dedups

    def _checkpoint(self) -> None:
        tmp = self.ckpt.with_suffix(".tmp")
        tmp.write_bytes(orjson.dumps(self.norm.dedup.snapshot()))   # JSON: a checkpoint is data, never code
        os.replace(tmp, self.ckpt)
        self.consumer.commit()

    # -------------------------------------------------------------------- loop
    def maybe_reload(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_epoch_check < 0.5:
            return False
        self._last_epoch_check = now
        try:
            if read_epoch() != self.norm.table.epoch:
                self.norm.swap_table(load_table())
                return True
        except Exception:
            pass  # registry unreachable: keep serving with the table we have
        return False

    def step(self, timeout_s: float = 0.2) -> int:
        self.maybe_reload()
        recs = self.consumer.poll(self.batch, timeout_s)
        if recs:
            now_ms = int(time.time() * 1000)
            parent = next((r.headers["tp"] for r in recs if "tp" in r.headers), None)
            with tracing.span("normalizer.batch", parent=parent, records=len(recs), worker=self.id) as tp:
                res = self.norm.process(recs, now_ms)
                if tp and res.canonical:
                    res.canonical[0].headers["tp"] = tp
                if res.canonical:
                    self.broker.produce(T_CANONICAL, res.canonical)
                if res.dead:
                    self.broker.produce(T_DLQ, res.dead)
            _merge(self.acc, res.stats)
            if res.stats.latency_ms:
                self.hist.record_many(np.asarray(res.stats.latency_ms, dtype=np.float64))
            self.processed += len(recs)
            self._avg_size = 0.9 * self._avg_size + 0.1 * (res.stats.bytes_in / len(recs) + 60)
        now = time.monotonic()
        if now - self._last_ckpt >= 2.0:
            flush_broker(self.broker)
            self._checkpoint()
            self._last_ckpt = now
        if now - self._last_pub >= 1.0:
            self.publish()
            self._last_pub = now
        return len(recs)

    def publish(self) -> None:
        lag = self.consumer.lag()
        is_bytes = get_settings().broker != "kafka"
        metrics.publish(self.broker, "normalizer", f"n{self.id}", {
            "pid": os.getpid(), "epoch": self.norm.table.epoch, "lag": lag,
            "lag_records": int(lag / self._avg_size) if is_bytes else lag,
            "stats": self.acc.to_dict(), "lat": metrics.hist_to_sparse(self.hist), "lat_max": self.hist.max,
            "table": self.norm.table.describe(), "table_errors": self.norm.table.errors,
            "dedup": {"vehicles": len(self.norm.dedup.window), "late": self.norm.dedup.late_seen,
                      "bloom_hits": self.norm.dedup.bloom_hits},
        })
        self.acc = BatchStats()
        self.hist.reset()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        tracing.setup("normalizer")
        try:
            while self.running:
                self.step()
        except KeyboardInterrupt:
            pass
        finally:
            flush_broker(self.broker)
            self._checkpoint()  # graceful shutdown: nothing to redo after restart
            self.publish()


def main(worker_id: int = 0, workers: int = 1) -> None:
    NormalizerWorker(worker_id, workers).run()


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0, int(sys.argv[2]) if len(sys.argv) > 2 else 1)
