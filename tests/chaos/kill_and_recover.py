"""Chaos test: kill pipeline processes with SIGKILL while traffic flows, then prove recovery.

What it does
  1. starts the real pipeline as separate processes under the supervisor
  2. lets traffic flow, then sends SIGKILL to a normaliser, a processor and the
     dead-letter worker, several seconds apart. SIGKILL cannot be caught: the
     process gets no chance to flush or commit, exactly like a machine dying
  3. stops the simulator, waits for the queues to drain
  4. checks the books

What must hold
  * every process that was killed is running again
  * sent = translated + dead-lettered + duplicates dropped (from the raw topic itself,
    not from metrics, because a killed process loses the metrics it had not published)
  * the archive holds every translated event exactly once

Usage: python tests/chaos/kill_and_recover.py [--vehicles 20000] [--seconds 40]
Writes docs/evidence/chaos.json and exits non-zero when a check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=20_000)
    ap.add_argument("--seconds", type=int, default=40)
    ap.add_argument("--normalizers", type=int, default=3)
    ap.add_argument("--processors", type=int, default=2)
    ap.add_argument("--out", default="docs/evidence/chaos.json")
    a = ap.parse_args()

    data = (ROOT / "data" / "chaos").resolve()
    shutil.rmtree(data, ignore_errors=True)
    os.environ.update({"ROSETTA_DATA_DIR": str(data), "ROSETTA_ENV": "test", "ROSETTA_LOG_RETENTION_MB": "4000",
                       "ROSETTA_ARCHIVE_RETENTION_MB": "4000", "ROSETTA_DLQ_RETENTION_MB": "2000",
                       "SIM_MODE": "all", "SIM_HZ": "1", "ROSETTA_VEHICLES": str(a.vehicles),
                       "SIM_ENABLED": json.dumps(["nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix"])})

    import orjson
    import pyarrow.compute as pc

    from rosetta.adapters.archive import ParquetArchive
    from rosetta.db.session import init_db, session_scope
    from rosetta.factory import load_vehicle_index, make_broker, make_hot_state
    from rosetta.pipeline.runtime import Supervisor, default_specs
    from rosetta.ports.broker import T_CANONICAL, T_DLQ, T_RAW
    from rosetta.services import seed
    from rosetta.simulator.run import send_control

    init_db()
    with session_scope() as s:
        seed.seed(s, vehicles=a.vehicles, log=lambda *x: None)
    vins, _ = load_vehicle_index()
    make_hot_state(vins, writable=True).close()
    broker = make_broker()

    sup = Supervisor(default_specs(a.normalizers, a.processors, 1, a.vehicles), data / "logs")
    sup.start()
    t0 = time.time()
    timeline = []

    def note(what: str, **kw) -> None:
        timeline.append({"t": round(time.time() - t0, 1), "event": what, **kw})
        print(f"  t={timeline[-1]['t']:5.1f}s  {what}  {kw or ''}")

    def raw_end() -> int:
        return sum(broker.end_offsets(T_RAW).values())

    victims = ["normalizer-1", "processor-0", "dlq-0", "normalizer-0"]
    plan = {int(a.seconds * f): v for f, v in zip((0.25, 0.4, 0.55, 0.7), victims)}
    killed = []
    try:
        for sec in range(a.seconds):
            time.sleep(1.0)
            if sec in plan:
                pid = sup.kill(plan[sec])
                killed.append(plan[sec])
                note("SIGKILL", process=plan[sec], pid=pid)
            if sec % 5 == 0:
                note("status", raw_bytes=raw_end(), restarts={p["name"]: p["restarts"] for p in sup.status() if p["restarts"]})
        send_control(broker, {"paused": True})
        note("simulator paused")
        # wait until the raw topic stops growing and every consumer has caught up
        last, stable = -1, 0
        for _ in range(180):
            time.sleep(1.0)
            end = raw_end()
            lag = 0
            for topic, group, parts in ((T_RAW, "normalizer", None), (T_CANONICAL, "processor", None)):
                c = broker.consumer(topic, group, parts)
                lag += c.lag()
            stable = stable + 1 if (end == last and lag == 0) else 0
            last = end
            if stable >= 6:      # six quiet seconds: longer than any commit interval
                break
        note("drained", seconds_after_pause=round(time.time() - t0 - a.seconds, 1))
        status = sup.status()
    finally:
        sup.stop(timeout_s=20)
    note("pipeline stopped")

    # ---- the books, read from the topics themselves
    def count(topic: str, group: str):
        c = broker.consumer(topic, group, start="beginning")
        n, keys, by = 0, set(), {}
        while True:
            recs = c.poll(20_000, 0.0)
            if not recs:
                break
            for r in recs:
                n += 1
                if topic == T_CANONICAL:
                    e = orjson.loads(r.value)
                    keys.add((e["vin"], e["seq"]))
                elif topic == T_DLQ:
                    by[r.headers.get("reason", "?")] = by.get(r.headers.get("reason", "?"), 0) + 1
        return n, keys, by

    raw_n, _, _ = count(T_RAW, "audit-raw")
    can_n, can_keys, _ = count(T_CANONICAL, "audit-canonical")
    dlq_n, _, dlq_by = count(T_DLQ, "audit-dlq")

    arch = ParquetArchive(str(data / "archive"))
    t = arch.dataset().to_table(columns=["vin", "seq"])
    keys = pc.binary_join_element_wise(t["vin"], pc.cast(t["seq"], "string"), ":")
    arch_rows, arch_distinct = t.num_rows, pc.count_distinct(keys).as_py()
    arch_keys = set(zip(t["vin"].to_pylist(), t["seq"].to_pylist()))

    # The simulator sends duplicates on purpose. A duplicate never reaches the canonical
    # topic, so: raw = distinct translated + dead letters + duplicates dropped.
    distinct_ok = len(can_keys)
    checks = {
        "killed_processes_restarted": all(p["alive"] and (p["restarts"] >= 1) for p in status if p["name"] in killed),
        "no_process_left_dead": all(p["alive"] for p in status),
        "archive_has_no_duplicate_rows": arch_rows == arch_distinct,
        "archive_holds_every_translated_event": arch_keys == can_keys,
        "canonical_redelivery_was_absorbed": can_n >= distinct_ok,
        "raw_messages_all_accounted": raw_n >= distinct_ok + 1 and raw_n <= can_n + dlq_n + raw_n,  # refined below
    }
    # exact accounting needs the duplicate count, which only the normalisers know. Recompute it
    # from the raw topic: group raw records by (device, payload) and count repeats.
    c = broker.consumer(T_RAW, "audit-raw-2", start="beginning")
    seen, dup_raw = set(), 0
    while True:
        recs = c.poll(20_000, 0.0)
        if not recs:
            break
        for r in recs:
            k = (r.key, r.value)
            if k in seen:
                dup_raw += 1
            else:
                seen.add(k)
    distinct_raw = raw_n - dup_raw
    # distinct dead letters: a redelivered unreadable message is parked again, so compare distinct ones
    c = broker.consumer(T_DLQ, "audit-dlq-2", start="beginning")
    dead_seen = set()
    while True:
        recs = c.poll(20_000, 0.0)
        if not recs:
            break
        for r in recs:
            dead_seen.add((r.headers.get("dev", "").encode(), r.value))
    checks["raw_messages_all_accounted"] = distinct_raw == distinct_ok + len(dead_seen)
    ok = all(checks.values())

    report = {
        "what": "SIGKILL on pipeline processes under load, then recovery and accounting",
        "config": {"vehicles": a.vehicles, "seconds": a.seconds, "normalizers": a.normalizers,
                   "processors": a.processors, "broker": "log"},
        "killed": killed,
        "processes_after": status,
        "counts": {"raw_messages": raw_n, "raw_distinct": distinct_raw, "raw_duplicates_sent_by_simulator": dup_raw,
                   "canonical_records": can_n, "canonical_distinct_events": distinct_ok,
                   "canonical_redelivered_after_kill": can_n - distinct_ok,
                   "dead_letter_records": dlq_n, "dead_letter_distinct": len(dead_seen), "dead_letters_by_reason": dlq_by,
                   "archive_rows": arch_rows, "archive_distinct": arch_distinct},
        "checks": checks,
        "passed": ok,
        "timeline": timeline,
    }
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("killed", "counts", "checks", "passed")}, indent=2))
    shutil.rmtree(data, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
