"""Process-wide state of the API: connections and caches built once at start-up."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import orjson
from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import Fleet, MetricMinute, Vehicle
from ..db.session import init_db, session_scope
from ..factory import make_archive, make_broker, make_hot_state
from ..pipeline.metrics import Aggregator, MetricsReader
from ..pipeline.runtime import Supervisor, default_specs
from ..services import audit, registry

log = logging.getLogger("rosetta.api.state")


@dataclass
class AppState:
    broker: Any = None
    agg: Aggregator = field(default_factory=Aggregator)
    reader: MetricsReader | None = None
    hot: Any = None
    archive: Any = None
    supervisor: Supervisor | None = None
    vins: list[str] = field(default_factory=list)
    vehicle_ids: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    tenant_of: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    started: float = field(default_factory=time.time)
    threads: list[threading.Thread] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    cache: dict[str, tuple[float, Any]] = field(default_factory=dict)

    locks: dict[str, threading.Lock] = field(default_factory=dict)
    locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def rendered(self, key: str, ttl: float, fn, stale_ok: bool = False) -> Response:
        """Like `cached`, but caches the serialised JSON bytes. Rendering a large response
        (5,000 map points is 300 KB) cost more than computing it."""
        body = self.cached("json:" + key, ttl, lambda: orjson.dumps(fn(), option=orjson.OPT_SERIALIZE_NUMPY),
                           stale_ok=stale_ok)
        return Response(content=body, media_type="application/json")

    def cached(self, key: str, ttl: float, fn, stale_ok: bool = False):
        """Short-lived shared cache with single flight.

        When an entry expires, one request recomputes it and concurrent requests for
        the same key wait for that result instead of all recomputing: without this, a
        burst of requests after expiry each paid the full cost at once."""
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        with self.locks_guard:
            lock = self.locks.setdefault(key, threading.Lock())
        if hit and stale_ok:
            # Serve the previous value now; one background thread refreshes it.
            if lock.acquire(blocking=False):
                def refresh() -> None:
                    try:
                        self.cache[key] = (time.monotonic(), fn())
                    finally:
                        lock.release()

                threading.Thread(target=refresh, daemon=True, name=f"refresh:{key[:30]}").start()
            return hit[1]
        with lock:
            hit = self.cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            val = fn()
            if len(self.cache) > 5000:
                self.cache.clear()
            self.cache[key] = (time.monotonic(), val)
            return val


state = AppState()


@contextmanager
def read_db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


@contextmanager
def write_db() -> Iterator[Session]:
    """Write transaction that may append to the audit chain."""
    with audit.CHAIN_LOCK, session_scope() as s:
        yield s


def load_vehicles() -> None:
    with session_scope() as s:
        rows = s.execute(select(Vehicle.vin, Vehicle.id, Fleet.tenant_id)
                         .join(Fleet, Fleet.id == Vehicle.fleet_id).order_by(Vehicle.id)).all()
    state.vins = [r[0] for r in rows]
    state.vehicle_ids = np.asarray([r[1] for r in rows], dtype=np.int64)
    state.tenant_of = np.asarray([r[2] for r in rows], dtype=np.int64)


def _rollup_loop() -> None:
    """Persist finished minutes to metric_minute, the table behind the history charts."""
    while not state.stop.wait(20.0):
        if len(state.vins) == 0:
            # Started before the seed job finished: pick the fleet up once it exists.
            try:
                load_vehicles()
            except Exception:
                log.debug("fleet not readable yet", exc_info=True)
        try:
            rows = state.agg.drain_minutes(int(time.time()) // 60)
            if not rows:
                continue
            with session_scope() as s:
                for r in rows:
                    cur = s.get(MetricMinute, (r["oem_key"], r["minute"]))
                    if cur is None:
                        cur = MetricMinute(oem_key=r["oem_key"], minute=r["minute"], received=0, ok=0,
                                           failed=0, duplicates=0)
                        s.add(cur)
                    cur.ok += r.get("ok", 0)
                    cur.failed += r.get("failed", 0)
                    cur.duplicates += r.get("duplicates", 0)
                    cur.received = cur.ok + cur.failed + cur.duplicates
        except Exception:
            continue


def _canary_guard() -> None:
    """Take a canary out of traffic when it produces invalid events.

    Rule: at least 200 canary faults and a fault rate above 5 percent in the
    last minute. The rollback is recorded as a system action in the audit log.
    """
    while not state.stop.wait(5.0):
        try:
            with session_scope() as s:
                rows = [r for r in registry.live_rows(s) if r.state == "canary"]
            for r in rows:
                h = state.agg.canary_health(r.oem, r.version)
                if h["failed"] >= 200 and (h["failure_rate"] or 0) > 0.05:
                    with write_db() as s:
                        registry.transition(s, r.oem, r.version, "rollback", actor="canary-guard",
                                            actor_kind="system",
                                            comment=f"automatic: {h['failed']} invalid events, "
                                                    f"fault rate {h['failure_rate']:.1%} in {h['window_s']}s")
        except Exception:
            continue


def startup() -> None:
    import anyio.to_thread

    # Sync endpoints and the audit hand-off share this pool; the default of 40 queued
    # requests behind each other under load.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 128
    state.stop.clear()          # a previous shutdown in this process must not end new streams
    st = get_settings()
    init_db()
    load_vehicles()
    state.broker = make_broker(st)
    state.archive = make_archive("api", st)
    if state.vins:
        state.hot = make_hot_state(state.vins, writable=False, settings=st)
    from .support import AUDIT

    AUDIT.start()
    state.reader = MetricsReader(state.broker, state.agg)
    state.reader.start()
    for fn in (_rollup_loop, _canary_guard):
        t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
        t.start()
        state.threads.append(t)
    if st.embed_pipeline and state.vins:
        state.supervisor = Supervisor(default_specs(st.normalizers, st.processors, st.sim_shards, st.vehicles),
                                      st.data_dir / "logs")
        state.supervisor.start()


def shutdown() -> None:
    from .support import AUDIT

    AUDIT.stop()
    state.stop.set()
    if state.reader:
        state.reader.stop()
    if state.supervisor:
        state.supervisor.stop()
    if state.hot is not None:
        state.hot.close()
