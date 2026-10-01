"""Operational metrics: workers publish snapshots, the API aggregates them.

Workers never talk to the dashboard. Once per second each one publishes a
small JSON snapshot to the ops.metrics topic. Snapshots are deltas (what
happened since the last one), so they add up correctly across any number of
workers, and latency travels as histogram buckets that can be merged exactly.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

import numpy as np
import orjson

from ..algorithms.countmin import TopK
from ..algorithms.sliding_window import LatencyHistogram, SlidingWindow
from ..ports.broker import T_METRICS, Record

WINDOW_S = 300


def hist_to_sparse(h: LatencyHistogram) -> list[list[int]]:
    nz = np.flatnonzero(h.counts)
    return [[int(i), int(h.counts[i])] for i in nz]


def publish(broker: Any, svc: str, ident: str, body: dict[str, Any]) -> None:
    msg = {"svc": svc, "id": ident, "ts": int(time.time() * 1000), **body}
    broker.produce(T_METRICS, [Record(key=f"{svc}:{ident}".encode(), value=orjson.dumps(msg))])


class _Hist:
    """Ring of per-second histograms, so percentiles cover 'the last N seconds'."""

    def __init__(self, seconds: int = 60) -> None:
        self.proto = LatencyHistogram()
        self.seconds = seconds
        self.ring: deque[tuple[int, np.ndarray, float]] = deque()

    def add(self, sec: int, sparse: list[list[int]], mx: float) -> None:
        arr = np.zeros(len(self.proto.counts), dtype=np.int64)
        for i, c in sparse:
            if 0 <= i < arr.size:
                arr[i] += c
        self.ring.append((sec, arr, mx))

    def merged(self, now_s: int) -> LatencyHistogram:
        while self.ring and self.ring[0][0] <= now_s - self.seconds:
            self.ring.popleft()
        h = LatencyHistogram()
        for _, arr, mx in self.ring:
            h.counts += arr
            h.max = max(h.max, mx)
        h.n = int(h.counts.sum())
        return h

    def summary(self, now_s: int) -> dict[str, float]:
        h = self.merged(now_s)
        return {"n": h.n, "p50": round(h.percentile(50), 2), "p95": round(h.percentile(95), 2),
                "p99": round(h.percentile(99), 2), "max": round(h.max, 2)}


class Aggregator:
    """Merges snapshots from every worker into the numbers the dashboard shows."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.win: dict[tuple[str, str], SlidingWindow] = defaultdict(lambda: SlidingWindow(WINDOW_S))
        self.totals: dict[tuple[str, str], int] = defaultdict(int)
        self.by_version: dict[tuple[str, int], int] = defaultdict(int)
        self.version_win: dict[tuple[str, int], SlidingWindow] = defaultdict(lambda: SlidingWindow(WINDOW_S))
        self.canary_fail_win: dict[tuple[str, int], SlidingWindow] = defaultdict(lambda: SlidingWindow(WINDOW_S))
        self.reasons: dict[tuple[str, str], int] = defaultdict(int)
        self.reason_win: dict[tuple[str, str], SlidingWindow] = defaultdict(lambda: SlidingWindow(WINDOW_S))
        self.top_fields = TopK(12)
        self.norm_lat = _Hist(60)
        self.e2e_lat = _Hist(60)
        self.alert_lat = _Hist(300)
        self.workers: dict[str, dict[str, Any]] = {}
        self.sim: dict[str, dict[str, Any]] = {}
        self.minute: dict[tuple[str, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.started = time.time()
        self.counters: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ ingest
    def ingest(self, msg: dict[str, Any]) -> None:
        svc, ident, ts = msg.get("svc"), msg.get("id", "?"), int(msg.get("ts", 0))
        sec = ts // 1000
        with self.lock:
            self.workers[f"{svc}:{ident}"] = {
                "svc": svc, "id": ident, "ts": ts, "lag": msg.get("lag", 0),
                "lag_records": msg.get("lag_records", 0), "epoch": msg.get("epoch"),
                "pid": msg.get("pid"), "rate": msg.get("rate", 0),
            }
            if svc == "normalizer":
                st = msg.get("stats", {})
                for oem, ver, n in st.get("ok", []):
                    self._add(oem, "ok", sec, n)
                    self.by_version[(oem, ver)] += n
                    self.version_win[(oem, ver)].add(sec, n)
                for oem, reason, n in st.get("failed", []):
                    self._add(oem, "failed", sec, n)
                    self.reasons[(oem, reason)] += n
                    self.reason_win[(oem, reason)].add(sec, n)
                for oem, reason, fld, n in st.get("failed_field", []):
                    if fld:
                        self.top_fields.add(f"{oem}.{fld} ({reason})", n)
                for oem, ver, n in st.get("canary_failed", []):
                    self.canary_fail_win[(oem, ver)].add(sec, n)
                for oem, n in st.get("duplicates", {}).items():
                    self._add(oem, "duplicates", sec, n)
                self.counters["fallbacks"] += st.get("fallbacks", 0)
                self.counters["replayed_ok"] += st.get("replayed_ok", 0)
                self.counters["bytes_in"] += st.get("bytes_in", 0)
                self.norm_lat.add(sec, msg.get("lat", []), msg.get("lat_max", 0.0))
                if "table" in msg:
                    self.workers[f"{svc}:{ident}"]["table"] = msg["table"]
                    self.workers[f"{svc}:{ident}"]["table_errors"] = msg.get("table_errors", [])
            elif svc == "processor":
                self.counters["processed"] += msg.get("events", 0)
                self.counters["alerts"] += msg.get("alerts", 0)
                self.counters["archived_rows"] += msg.get("archived_rows", 0)
                self.counters["late_events"] += msg.get("late", 0)
                self.counters["sink_duplicates"] += msg.get("duplicates", 0)
                self.e2e_lat.add(sec, msg.get("e2e", []), msg.get("e2e_max", 0.0))
                self.alert_lat.add(sec, msg.get("alert_lat", []), msg.get("alert_lat_max", 0.0))
                self.win[("_all", "processed")].add(sec, msg.get("events", 0))
            elif svc == "simulator":
                self.sim[ident] = msg
                for oem, n in msg.get("by_oem", {}).items():
                    self._add(oem, "sent", sec, n)
                self.counters["sim_sent"] += msg.get("sent", 0)
                self.counters["sim_duplicates"] += msg.get("dups", 0)
                self.counters["sim_malformed"] += msg.get("malformed", 0)
                self.counters["sim_reordered"] += msg.get("reordered", 0)
            elif svc == "dlq":
                self.counters["dlq_indexed"] += msg.get("indexed", 0)
                self.counters["dlq_replayed"] += msg.get("replayed", 0)

    def _add(self, oem: str, kind: str, sec: int, n: int) -> None:
        self.win[(oem, kind)].add(sec, n)
        self.win[("_all", kind)].add(sec, n)
        self.totals[(oem, kind)] += n
        self.totals[("_all", kind)] += n
        if kind in ("ok", "failed", "duplicates"):
            self.minute[(oem, sec // 60)][kind] += n

    # ------------------------------------------------------------------- views
    def overview(self, window: int = 10) -> dict[str, Any]:
        now = int(time.time())
        with self.lock:
            oems = sorted({o for (o, _k) in self.win if o != "_all"})

            def rate(oem: str, kind: str) -> float:
                w = self.win.get((oem, kind))
                if w is None:
                    return 0.0
                s = w.series(now - 1)[-window:]
                return round(sum(s) / window, 1)

            per_oem = []
            for o in oems:
                ok, failed = rate(o, "ok"), rate(o, "failed")
                tot = ok + failed
                versions = sorted(((v, n) for (oo, v), n in self.by_version.items() if oo == o))
                per_oem.append({
                    "oem": o, "ok_per_s": ok, "failed_per_s": failed, "dup_per_s": rate(o, "duplicates"),
                    "sent_per_s": rate(o, "sent"),
                    "success_rate": round(ok / tot, 5) if tot else None,
                    "total_ok": self.totals[(o, "ok")], "total_failed": self.totals[(o, "failed")],
                    "total_duplicates": self.totals[(o, "duplicates")],
                    "versions": [{"version": v, "events": n,
                                  "per_s": round(sum(self.version_win[(o, v)].series(now - 1)[-window:]) / window, 1)}
                                 for v, n in versions],
                    "reasons": {r: n for (oo, r), n in self.reasons.items() if oo == o},
                })
            workers = [w for w in self.workers.values() if now * 1000 - w["ts"] < 15_000]
            table: dict[str, Any] = {}
            for w in workers:
                if w.get("table"):
                    table = w["table"]
                    break
            return {
                "now": now * 1000,
                "uptime_s": int(time.time() - self.started),
                "throughput": {"ok_per_s": rate("_all", "ok"), "failed_per_s": rate("_all", "failed"),
                               "dup_per_s": rate("_all", "duplicates"), "sent_per_s": rate("_all", "sent"),
                               "processed_per_s": rate("_all", "processed")},
                "totals": {k: self.totals[("_all", k)] for k in ("ok", "failed", "duplicates", "sent")},
                "counters": dict(self.counters),
                "latency_ms": {"normalize": self.norm_lat.summary(now), "ingest_to_dashboard": self.e2e_lat.summary(now),
                               "alert": self.alert_lat.summary(now)},
                "lag": {"normalizer": sum(w["lag_records"] for w in workers if w["svc"] == "normalizer"),
                        "processor": sum(w["lag_records"] for w in workers if w["svc"] == "processor")},
                "oems": per_oem,
                "routing": table,
                "workers": sorted(({k: v for k, v in w.items() if k not in ("table", "table_errors")}
                                   for w in workers), key=lambda w: (w["svc"], str(w["id"]))),
                "top_failing_fields": [{"key": k, "count": n} for k, n in self.top_fields.items()],
                "simulator": next(iter(self.sim.values()), {}).get("settings", {}),
            }

    def series(self, oem: str = "_all", seconds: int = 120) -> dict[str, Any]:
        now = int(time.time()) - 1
        with self.lock:
            out = {"now": now * 1000, "step_s": 1, "oem": oem}
            for kind in ("ok", "failed", "duplicates", "sent"):
                w = self.win.get((oem, kind))
                out[kind] = [int(x) for x in (w.series(now)[-seconds:] if w else [0] * seconds)]
            return out

    def series_all(self, seconds: int = 120) -> dict[str, Any]:
        """ok per second for every source, plus failures, on one time base."""
        now = int(time.time()) - 1
        with self.lock:
            oems = sorted({o for (o, k) in self.win if o != "_all" and k == "ok"})
            out: dict[str, Any] = {"now": now * 1000, "seconds": seconds, "ok": {}, "failed": [], "duplicates": []}
            for o in oems:
                out["ok"][o] = [int(x) for x in self.win[(o, "ok")].series(now)[-seconds:]]
            for kind in ("failed", "duplicates"):
                w = self.win.get(("_all", kind))
                out[kind] = [int(x) for x in (w.series(now)[-seconds:] if w else [0] * seconds)]
            return out

    def canary_health(self, oem: str, version: int, window: int = 60) -> dict[str, Any]:
        now = int(time.time()) - 1
        with self.lock:
            ok = sum(self.version_win[(oem, version)].series(now)[-window:])
            bad = sum(self.canary_fail_win[(oem, version)].series(now)[-window:])
        return {"ok": int(ok), "failed": int(bad), "window_s": window,
                "failure_rate": round(bad / (ok + bad), 4) if ok + bad else None}

    def drain_minutes(self, before_minute: int) -> list[dict[str, Any]]:
        """Completed minutes, for the metric_minute rollup table."""
        with self.lock:
            keys = [k for k in self.minute if k[1] < before_minute]
            out = [{"oem_key": o, "minute": m, **self.minute.pop((o, m))} for (o, m) in keys]
        return out

    def prometheus(self) -> str:
        ov = self.overview()
        lines = [
            "# HELP rosetta_events_total Events by OEM source and outcome.",
            "# TYPE rosetta_events_total counter",
        ]
        for o in ov["oems"]:
            for kind, key in (("ok", "total_ok"), ("failed", "total_failed"), ("duplicate", "total_duplicates")):
                lines.append(f'rosetta_events_total{{oem="{o["oem"]}",outcome="{kind}"}} {o[key]}')
        lines += ["# HELP rosetta_dead_letters_total Dead-lettered events by OEM and reason.",
                  "# TYPE rosetta_dead_letters_total counter"]
        for o in ov["oems"]:
            for r, n in o["reasons"].items():
                lines.append(f'rosetta_dead_letters_total{{oem="{o["oem"]}",reason="{r}"}} {n}')
        lines += ["# HELP rosetta_throughput_events_per_second Normalised events per second, 10 s average.",
                  "# TYPE rosetta_throughput_events_per_second gauge",
                  f'rosetta_throughput_events_per_second {ov["throughput"]["ok_per_s"]}',
                  "# HELP rosetta_consumer_lag_records Records waiting, by service.",
                  "# TYPE rosetta_consumer_lag_records gauge"]
        for svc, n in ov["lag"].items():
            lines.append(f'rosetta_consumer_lag_records{{service="{svc}"}} {n}')
        lines += ["# HELP rosetta_latency_milliseconds Latency percentiles over the last minute.",
                  "# TYPE rosetta_latency_milliseconds gauge"]
        for stage, d in ov["latency_ms"].items():
            for q in ("p50", "p95", "p99"):
                lines.append(f'rosetta_latency_milliseconds{{stage="{stage}",quantile="{q}"}} {d[q]}')
        # The lag is a sum over the workers that report, and a sum over none is 0.
        # This gauge is what tells "idle" from "gone": alert when it drops to 0.
        now = time.time() * 1000
        alive: dict[str, int] = {"normalizer": 0, "processor": 0, "dlq": 0}
        for w in self.workers.values():
            if now - w.get("ts", 0) < 15_000:
                alive[w.get("svc", "unknown")] = alive.get(w.get("svc", "unknown"), 0) + 1
        lines += ["# HELP rosetta_workers_reporting Workers that reported in the last 15 s, by service.",
                  "# TYPE rosetta_workers_reporting gauge"]
        for svc, n in sorted(alive.items()):
            lines.append(f'rosetta_workers_reporting{{service="{svc}"}} {n}')
        return "\n".join(lines) + "\n"


class MetricsReader(threading.Thread):
    """Background thread in the API process: tails ops.metrics into the Aggregator."""

    def __init__(self, broker: Any, agg: Aggregator, group: str | None = None) -> None:
        super().__init__(daemon=True, name="metrics-reader")
        self.agg = agg
        self.consumer = broker.consumer(T_METRICS, group or f"api-metrics-{time.time_ns()}", start="end")
        self.stop_flag = threading.Event()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                recs = self.consumer.poll(500, 0.25)
            except Exception:
                time.sleep(0.5)
                continue
            for r in recs:
                try:
                    self.agg.ingest(orjson.loads(r.value))
                except Exception:
                    continue        # one bad snapshot must not cost the ones behind it

    def stop(self) -> None:
        self.stop_flag.set()
