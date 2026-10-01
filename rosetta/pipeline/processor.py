"""Stream processor: canonical events in, state, alerts and history out.

For each batch of canonical events it
  * updates the hot state (latest value per vehicle, last-write-wins)
  * raises alerts from driving events and new trouble codes
  * appends to the Parquet archive in micro-batches
  * measures ingest-to-dashboard latency, the number the NFR is about

Event time and late data: windows are keyed by event time. An event whose
timestamp is older than the watermark (newest event time seen minus the
allowed lateness) is counted as late. It is still stored, so batch jobs see
it, but it does not reopen a real-time window.
"""
from __future__ import annotations

import os
import signal
import time
from collections.abc import Sequence
from typing import Any

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.json as paj
from sqlalchemy import insert

from ..adapters.archive import SCHEMA as ARCHIVE_SCHEMA
from ..algorithms.geohash import encode_many
from ..algorithms.replay_window import BatchReplayWindow
from ..algorithms.sliding_window import LatencyHistogram
from ..config import get_settings
from ..domain.canonical import EVENT_TYPES
from ..factory import (
    flush_broker,
    load_vehicle_index,
    make_archive,
    make_broker,
    make_hot_state,
    make_telemetry_sink,
)
from ..observability import tracing
from ..ports.broker import T_ALERTS, T_CANONICAL, Record
from ..simulator.fleet import OEM_KEYS
from . import metrics

# What the processor reads from a canonical event. Unknown keys are ignored, so a
# new optional field in the canonical schema never breaks a running processor.
CANON = pa.schema([
    ("vin", pa.string()), ("ts", pa.int64()), ("seq", pa.int64()), ("oem", pa.string()),
    ("map_v", pa.int32()), ("lat", pa.float64()), ("lon", pa.float64()),
    ("speed_kmh", pa.float64()), ("heading_deg", pa.float64()), ("odo_km", pa.float64()),
    ("soc_pct", pa.float64()), ("fuel_pct", pa.float64()), ("ambient_c", pa.float64()),
    ("ignition", pa.bool_()), ("dtc", pa.list_(pa.string())), ("evt", pa.string()),
    ("rx_ts", pa.int64()), ("norm_ts", pa.int64()), ("replayed", pa.bool_()),
])
_PARSE = paj.ParseOptions(explicit_schema=CANON, unexpected_field_behavior="ignore")
_READ = paj.ReadOptions(use_threads=False, block_size=64 << 20)
_OEM_SET = pa.array(list(OEM_KEYS), pa.string())
_EVT_SET = pa.array(list(EVENT_TYPES), pa.string())


def _np(t: pa.Table, name: str, dtype: Any) -> np.ndarray:
    col = t[name].combine_chunks()
    if col.null_count and np.issubdtype(dtype, np.floating):
        return col.to_numpy(zero_copy_only=False).astype(dtype)   # nulls become NaN
    if col.null_count:
        col = col.fill_null(0)
    return col.to_numpy(zero_copy_only=False).astype(dtype, copy=False)

ALLOWED_LATENESS_MS = 30_000
ALERT_EVENTS = {"HARSH_BRAKE": "warning", "HARSH_ACCEL": "warning", "SPEEDING": "warning", "LOW_SOC": "critical"}
CRITICAL_DTC_PREFIX = ("P0A", "P0AA", "U01", "P1E")


class Processor:
    def __init__(self, worker_id: int = 0, workers: int = 1, broker: Any = None,
                 partitions: Sequence[int] | None = None, batch: int = 5000,
                 archive_every_s: float = 5.0, archive_rows: int = 60_000,
                 write_db_alerts: bool = True, group: str = "processor") -> None:
        st = get_settings()
        self.id = worker_id
        self.broker = broker or make_broker(st)
        n = self.broker.partitions(T_CANONICAL)
        if partitions is None and st.broker != "kafka":
            partitions = [p for p in range(n) if p % workers == worker_id]
        self.consumer = self.broker.consumer(T_CANONICAL, group, partitions)
        self.batch = batch
        vins, ids = load_vehicle_index()
        self.vehicle_ids = np.asarray(ids, dtype=np.int64)
        self.hot = make_hot_state(vins, writable=True, settings=st)
        self.index = self.hot.index
        self.archive = make_archive(f"p{worker_id}", st)
        # Resume from what the archive already holds, not only from the committed
        # offsets: a crash between "file written" and "offsets committed" would
        # otherwise store the same rows twice.
        seek = getattr(self.consumer, "seek", None)
        if seek is not None:
            have = self.consumer.positions()
            for part, off in self.archive.last_offsets().items():
                if part in have and off > have[part]:
                    seek(part, off)
        self.sink = make_telemetry_sink(st)
        self.archive_every_s = archive_every_s
        self.archive_rows = archive_rows
        self.pending: list[pa.Table] = []
        self.pending_rows = 0
        self.vin_array = pa.array(vins, pa.string())
        # idempotence: the canonical topic is at-least-once, so the sink drops repeats
        self.window = BatchReplayWindow(len(vins))
        self.dtc_seen: dict[int, frozenset] = {}
        self.write_db_alerts = write_db_alerts
        self.watermark = 0
        self.running = True
        self.e2e = LatencyHistogram()
        self.alert_lat = LatencyHistogram()
        self.n_events = self.n_alerts = self.n_late = self.n_dup = self.n_archived = self.n_unknown = 0
        self.n_replayed = 0
        self._last_pub = self._last_flush = self._last_commit = time.monotonic()
        self._avg_size = 330.0
        self.processed = 0

    # ------------------------------------------------------------------ batch
    def handle(self, recs: list[Record]) -> int:
        """One batch, handled as columns. The JSON is parsed once, in C++, straight into
        Arrow arrays. After that every step is a vector operation: no per-event Python."""
        buf = b"\n".join([r.value for r in recs])
        t = paj.read_json(pa.BufferReader(buf), read_options=_READ, parse_options=_PARSE)
        n = t.num_rows
        if not n:
            return 0
        idx = pc.index_in(t["vin"], value_set=self.vin_array).combine_chunks() \
            .fill_null(-1).to_numpy(zero_copy_only=False).astype(np.int64)
        seq = _np(t, "seq", np.int64)
        known = idx >= 0
        keep = np.ones(n, dtype=bool)
        if known.any():
            keep[known] = self.window.check_batch(idx[known], seq[known])
        dropped = int(n - keep.sum())
        if dropped:
            self.n_dup += dropped
            t = t.filter(pa.array(keep))
            idx, seq, known = idx[keep], seq[keep], known[keep]
            n = t.num_rows
            if not n:
                return 0
        ts, rx = _np(t, "ts", np.int64), _np(t, "rx_ts", np.int64)
        lat, lon = _np(t, "lat", np.float64), _np(t, "lon", np.float64)

        newest = int(ts.max())
        if newest > self.watermark + ALLOWED_LATENESS_MS:
            self.watermark = newest - ALLOWED_LATENESS_MS
        self.n_late += int((ts < self.watermark).sum())
        self.n_unknown += int(n - known.sum())

        now_ms = int(time.time() * 1000)
        oem = pc.index_in(t["oem"], value_set=_OEM_SET).combine_chunks().fill_null(-1) \
            .to_numpy(zero_copy_only=False).astype(np.int8)
        evt = pc.index_in(t["evt"], value_set=_EVT_SET).combine_chunks()
        evt_i = (pc.add(evt, 1).fill_null(0)).to_numpy(zero_copy_only=False).astype(np.int8)
        dtc_n = pc.list_value_length(t["dtc"]).combine_chunks().fill_null(0) \
            .to_numpy(zero_copy_only=False).astype(np.int64)
        k = known
        self.hot.update_batch({
            "idx": idx[k], "ts": ts[k], "rx_ts": rx[k],
            "seen_ts": np.full(int(k.sum()), now_ms, dtype=np.int64), "seq": seq[k],
            "lat": lat[k], "lon": lon[k],
            "speed_kmh": _np(t, "speed_kmh", np.float32)[k], "heading_deg": _np(t, "heading_deg", np.float32)[k],
            "odo_km": _np(t, "odo_km", np.float64)[k], "soc_pct": _np(t, "soc_pct", np.float32)[k],
            "fuel_pct": _np(t, "fuel_pct", np.float32)[k], "ambient_c": _np(t, "ambient_c", np.float32)[k],
            "ignition": pc.fill_null(t["ignition"], False).combine_chunks().to_numpy(zero_copy_only=False).astype(np.int8)[k],
            "oem": oem[k], "evt": evt_i[k], "dtc_n": np.minimum(dtc_n, 127).astype(np.int8)[k],
            "map_v": _np(t, "map_v", np.int32)[k],
        })
        done_ms = int(time.time() * 1000)
        fresh = ~pc.fill_null(t["replayed"], False).combine_chunks().to_numpy(zero_copy_only=False).astype(bool)
        self.n_replayed += int(n - fresh.sum())
        self.e2e.record_many((done_ms - rx[fresh]).astype(np.float64))

        hits = np.flatnonzero((evt_i > 0) | (dtc_n > 0))
        if hits.size:
            self._alerts(t.take(pa.array(hits)).to_pylist(), idx[hits], done_ms)

        gh = encode_many(lat, lon, 5)
        self.pending.append(t.append_column("geohash5", pa.array(gh.astype("U5"), pa.string())))
        self.pending_rows += n
        self.n_events += n
        return n

    def _alerts(self, evs: list[dict[str, Any]], idx: np.ndarray, now_ms: int) -> None:
        rows, recs = [], []
        ids = self.vehicle_ids
        for j, e in enumerate(evs):
            kind = e.get("evt")
            sev = ALERT_EVENTS.get(kind) if kind else None
            found = []
            if sev:
                if kind == "LOW_SOC" and (e.get("soc_pct") or 0) >= 8:
                    sev = "warning"
                found.append((kind, sev, {"speed_kmh": e["speed_kmh"], "soc_pct": e.get("soc_pct")}))
            dtc = e.get("dtc")
            if dtc:
                i = int(idx[j])
                cur = frozenset(dtc)
                prev = self.dtc_seen.get(i)
                if prev != cur:
                    self.dtc_seen[i] = cur
                    new = sorted(cur - (prev or frozenset()))
                    # the first sighting after a restart is state, not news
                    if prev is not None and new:
                        crit = any(c.startswith(CRITICAL_DTC_PREFIX) for c in new)
                        found.append(("DTC", "critical" if crit else "warning", {"codes": new}))
            for kind, sev, detail in found:
                i = int(idx[j])
                detail.update({"lat": e["lat"], "lon": e["lon"], "oem": e["oem"]})
                if i >= 0:
                    rows.append({"vehicle_id": int(ids[i]), "kind": kind, "severity": sev, "ts": e["ts"],
                                 "detected_ts": now_ms, "detail": detail})
                recs.append(Record(key=e["vin"].encode(), value=orjson.dumps(
                    {"vin": e["vin"], "kind": kind, "severity": sev, "ts": e["ts"], "detected_ts": now_ms,
                     "rx_ts": e["rx_ts"], "detail": detail})))
                if not e.get("replayed"):
                    self.alert_lat.record(now_ms - e["rx_ts"])
        if recs:
            self.broker.produce(T_ALERTS, recs)
            self.n_alerts += len(recs)
        if rows and self.write_db_alerts:
            self._store_alerts(rows)

    def _store_alerts(self, rows: list[dict[str, Any]]) -> None:
        from ..db.models import Alert
        from ..db.session import get_engine

        eng = get_engine()
        stmt = insert(Alert)
        # idempotent insert: a replayed event cannot create a second alert
        stmt = stmt.prefix_with("OR IGNORE") if eng.dialect.name == "sqlite" else stmt
        try:
            with eng.begin() as c:
                if eng.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    c.execute(pg_insert(Alert).on_conflict_do_nothing(constraint="uq_alert_idempotent"), rows)
                else:
                    c.execute(stmt, rows)
        except Exception:
            pass  # the alert is already on the alerts topic; the table is a read model

    def flush_archive(self) -> None:
        if not self.pending:
            return
        table = pa.concat_tables(self.pending).select(list(ARCHIVE_SCHEMA.names)).cast(ARCHIVE_SCHEMA)
        self.archive.write(table, int(time.time() * 1000), offsets=self.consumer.positions())
        if self.sink is not None:
            self.sink.write(table)       # idempotent: ON CONFLICT DO NOTHING
        self.n_archived += table.num_rows
        self.pending, self.pending_rows = [], 0

    # ------------------------------------------------------------------- loop
    def step(self, timeout_s: float = 0.2) -> int:
        recs = self.consumer.poll(self.batch, timeout_s)
        n = 0
        if recs:
            parent = next((r.headers["tp"] for r in recs if "tp" in r.headers), None)
            with tracing.span("processor.batch", parent=parent, records=len(recs), worker=self.id):
                n = self.handle(recs)
            self.processed += len(recs)
            self._avg_size = 0.9 * self._avg_size + 0.1 * (sum(len(r.value) for r in recs[:50]) / min(50, len(recs)) + 60)
        now = time.monotonic()
        if self.pending_rows >= self.archive_rows or (self.pending and now - self._last_flush >= self.archive_every_s):
            self.flush_archive()
            self._last_flush = now
            flush_broker(self.broker)
            self.consumer.commit()   # commit only after the rows are durable in the archive
            self._last_commit = now
        if now - self._last_pub >= 1.0:
            self.publish()
            self._last_pub = now
        return n

    def publish(self) -> None:
        lag = self.consumer.lag()
        is_bytes = get_settings().broker != "kafka"
        metrics.publish(self.broker, "processor", f"p{self.id}", {
            "pid": os.getpid(), "lag": lag, "lag_records": int(lag / self._avg_size) if is_bytes else lag,
            "events": self.n_events, "alerts": self.n_alerts, "late": self.n_late,
            "duplicates": self.n_dup, "archived_rows": self.n_archived, "unknown_vehicle": self.n_unknown,
            "replayed": self.n_replayed,
            "e2e": metrics.hist_to_sparse(self.e2e), "e2e_max": self.e2e.max,
            "alert_lat": metrics.hist_to_sparse(self.alert_lat), "alert_lat_max": self.alert_lat.max,
        })
        self.n_events = self.n_alerts = self.n_late = self.n_dup = self.n_archived = self.n_unknown = 0
        self.n_replayed = 0
        self.e2e.reset()
        self.alert_lat.reset()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        tracing.setup("processor")
        try:
            while self.running:
                self.step()
        except KeyboardInterrupt:
            pass
        finally:
            self.flush_archive()
            flush_broker(self.broker)
            self.consumer.commit()
            self.hot.close()
            self.publish()


def main(worker_id: int = 0, workers: int = 1) -> None:
    Processor(worker_id, workers).run()


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0, int(sys.argv[2]) if len(sys.argv) > 2 else 1)
