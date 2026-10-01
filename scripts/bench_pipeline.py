"""Throughput benchmark: the full pipeline as separate processes on one machine.

  simulator shards -> gateway -> raw topic -> N normalisers -> canonical topic
  -> M processors -> hot state + Parquet archive

Usage: python scripts/bench_pipeline.py --seconds 60 --hz 1 --normalizers 4 --processors 3 --shards 2
Writes a JSON report to docs/evidence/.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--vehicles", type=int, default=100_000)
    ap.add_argument("--hz", type=float, default=1.0)
    ap.add_argument("--normalizers", type=int, default=4)
    ap.add_argument("--processors", type=int, default=3)
    ap.add_argument("--shards", type=int, default=2)
    ap.add_argument("--burst-hz", type=float, default=0.0, help="if set, raise hz to this for --burst-seconds")
    ap.add_argument("--burst-seconds", type=int, default=0)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--label", default="steady")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--drain-max", type=int, default=120, help="seconds to wait for the backlog to drain at the end")
    a = ap.parse_args()

    data = Path(a.data_dir or f"./data/bench-{a.label}").resolve()
    shutil.rmtree(data, ignore_errors=True)
    os.environ["ROSETTA_DATA_DIR"] = str(data)
    os.environ.setdefault("ROSETTA_LOG_RETENTION_MB", "600")
    os.environ.setdefault("ROSETTA_ARCHIVE_RETENTION_MB", "300")
    os.environ["SIM_MODE"] = "all"
    os.environ["SIM_HZ"] = str(a.hz)
    os.environ["SIM_ENABLED"] = json.dumps(["nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix"])

    import orjson

    from rosetta.db.session import init_db, session_scope
    from rosetta.factory import load_vehicle_index, make_broker, make_hot_state
    from rosetta.pipeline.metrics import Aggregator
    from rosetta.pipeline.runtime import Supervisor, default_specs
    from rosetta.ports.broker import T_METRICS
    from rosetta.services import registry, seed
    from rosetta.simulator.dialects import SPEC_HELIX
    from rosetta.simulator.run import send_control

    init_db()
    with session_scope() as s:
        seed.seed(s, vehicles=a.vehicles, log=lambda *x: None)
        mv = registry.create_version(s, "helix", SPEC_HELIX, source="human", actor="bench")
        registry.validate_version(s, "helix", mv.version, actor="bench")
        registry.transition(s, "helix", mv.version, "approve", actor="bench")
        registry.transition(s, "helix", mv.version, "promote", actor="bench")
    vins, _ = load_vehicle_index()
    make_hot_state(vins, writable=True).close()

    broker = make_broker()
    agg = Aggregator()
    mc = broker.consumer(T_METRICS, "bench", start="end")
    sup = Supervisor(default_specs(a.normalizers, a.processors, a.shards, a.vehicles), data / "logs")
    sup.start()
    print(f"started {len(sup.states)} processes, warming up {a.warmup}s")

    import subprocess

    def rss_mb() -> dict[str, float]:
        """Resident memory of each process, from ps. Used to spot leaks in the soak run."""
        out = {}
        for p in sup.status():
            if p["pid"]:
                try:
                    kb = subprocess.run(["ps", "-o", "rss=", "-p", str(p["pid"])], capture_output=True,
                                        text=True, timeout=2).stdout.strip()
                    out[p["name"]] = round(int(kb) / 1024, 1)
                except Exception:
                    pass
        return out

    samples = []
    memory = []
    t_start = time.time()
    burst_on = burst_off = False
    try:
        while True:
            time.sleep(1.0)
            for r in mc.poll(2000, 0.0):
                agg.ingest(orjson.loads(r.value))
            el = time.time() - t_start
            if el < a.warmup:
                continue
            t = el - a.warmup
            if a.burst_hz and not burst_on and t >= a.seconds / 4:
                send_control(broker, {"hz": a.burst_hz}); burst_on = True
                print(f"  burst on: hz={a.burst_hz}")
            if a.burst_hz and burst_on and not burst_off and t >= a.seconds / 4 + a.burst_seconds:
                send_control(broker, {"hz": a.hz}); burst_off = True
                print("  burst off")
            ov = agg.overview(window=5)
            row = {"t": round(t, 1), "sent_per_s": ov["throughput"]["sent_per_s"],
                   "ok_per_s": ov["throughput"]["ok_per_s"], "processed_per_s": ov["throughput"]["processed_per_s"],
                   "lag_normalizer": ov["lag"]["normalizer"], "lag_processor": ov["lag"]["processor"],
                   "e2e_p50": ov["latency_ms"]["ingest_to_dashboard"]["p50"],
                   "e2e_p95": ov["latency_ms"]["ingest_to_dashboard"]["p95"],
                   "e2e_p99": ov["latency_ms"]["ingest_to_dashboard"]["p99"],
                   "norm_p95": ov["latency_ms"]["normalize"]["p95"],
                   "alert_p99": ov["latency_ms"]["alert"]["p99"]}
            samples.append(row)
            if int(t) % 10 == 0:
                memory.append({"t": round(t), **rss_mb()})
            if int(t) % 5 == 0:
                print(f"  t={row['t']:5.0f}s sent={row['sent_per_s']:>9,.0f}/s ok={row['ok_per_s']:>9,.0f}/s "
                      f"sink={row['processed_per_s']:>9,.0f}/s lagN={row['lag_normalizer']:>8,} lagP={row['lag_processor']:>8,} "
                      f"e2e p95={row['e2e_p95']:.0f}ms p99={row['e2e_p99']:.0f}ms")
            if t >= a.seconds:
                break
        # drain: stop the simulator, wait for lag to reach zero, then check nothing was lost
        send_control(broker, {"paused": True})
        drain_start = time.time()
        while time.time() - drain_start < a.drain_max:
            time.sleep(1.0)
            for r in mc.poll(2000, 0.0):
                agg.ingest(orjson.loads(r.value))
            ov = agg.overview(window=3)
            if ov["lag"]["normalizer"] == 0 and ov["lag"]["processor"] == 0 and ov["throughput"]["ok_per_s"] == 0 \
                    and time.time() - drain_start > 6:
                break
        drain_s = time.time() - drain_start
    finally:
        sup.stop()
    time.sleep(0.5)
    for r in mc.poll(5000, 0.2):
        agg.ingest(orjson.loads(r.value))
    ov = agg.overview()
    c, tot = ov["counters"], ov["totals"]
    steady = [s for s in samples if s["ok_per_s"] > 0]
    burst = [s for s in steady if a.burst_hz and a.seconds / 4 <= s["t"] <= a.seconds / 4 + a.burst_seconds]
    import statistics as st

    accounted = tot["ok"] + tot["failed"] + tot["duplicates"]
    report = {
        "label": a.label,
        "machine": {"cpu": platform.processor() or platform.machine(), "cores": os.cpu_count(),
                    "python": platform.python_version(), "os": platform.platform()},
        "config": {"vehicles": a.vehicles, "hz": a.hz, "normalizers": a.normalizers, "processors": a.processors,
                   "simulator_shards": a.shards, "broker": os.environ.get("ROSETTA_BROKER", "log"),
                   "seconds": a.seconds, "burst_hz": a.burst_hz, "burst_seconds": a.burst_seconds},
        "throughput": {
            "sent_per_s_mean": round(st.mean(s["sent_per_s"] for s in steady), 0) if steady else 0,
            "normalized_per_s_mean": round(st.mean(s["ok_per_s"] for s in steady), 0) if steady else 0,
            "normalized_per_s_max": max((s["ok_per_s"] for s in steady), default=0),
            "processed_per_s_mean": round(st.mean(s["processed_per_s"] for s in steady), 0) if steady else 0,
        },
        "latency_ms": {
            "ingest_to_dashboard_p50_median": st.median(s["e2e_p50"] for s in steady) if steady else 0,
            "ingest_to_dashboard_p95_median": st.median(s["e2e_p95"] for s in steady) if steady else 0,
            "ingest_to_dashboard_p99_median": st.median(s["e2e_p99"] for s in steady) if steady else 0,
            "ingest_to_dashboard_p99_worst": max((s["e2e_p99"] for s in steady), default=0),
            "alert_p99_worst": max((s["alert_p99"] for s in steady), default=0),
        },
        "burst": ({"sent_per_s_mean": round(st.mean(s["sent_per_s"] for s in burst), 0),
                   "normalized_per_s_mean": round(st.mean(s["ok_per_s"] for s in burst), 0),
                   "lag_max": max(s["lag_normalizer"] + s["lag_processor"] for s in burst),
                   "e2e_p99_worst_ms": max(s["e2e_p99"] for s in burst)} if burst else None),
        "lag": {"normalizer_max": max((s["lag_normalizer"] for s in samples), default=0),
                "processor_max": max((s["lag_processor"] for s in samples), default=0),
                "drain_seconds": round(drain_s, 1)},
        "accounting": {
            "simulator_sent": c.get("sim_sent", 0), "normalized_ok": tot["ok"], "dead_lettered": tot["failed"],
            "duplicates_dropped": tot["duplicates"], "accounted": accounted,
            "unaccounted": c.get("sim_sent", 0) - accounted,
            "processed_by_sink": c.get("processed", 0), "sink_duplicates": c.get("sink_duplicates", 0),
            "archived_rows": c.get("archived_rows", 0),
        },
        "restarts": {p["name"]: p["restarts"] for p in sup.status()},
        "memory_mb": {"first": memory[0] if memory else {}, "last": memory[-1] if memory else {},
                      "growth": {k: round(memory[-1][k] - memory[0][k], 1) for k in memory[0]
                                 if k != "t" and k in memory[-1]} if len(memory) > 1 else {}},
        "memory_samples": memory,
        "samples": samples,
    }
    out = Path("docs/evidence")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"bench_{a.label}.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k not in ("samples", "memory_samples")}, indent=2))
    print("report:", path)
    if not a.keep:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
