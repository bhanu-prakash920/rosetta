"""EXPLAIN ANALYZE before and after, for the three queries that matter most.

Starts PostgreSQL 16 in Docker, creates the schema WITHOUT the performance
indexes, loads realistic volumes (100,000 vehicles, 1,000,000 alerts, 400,000
audit entries), runs each query, adds the index or rewrite, runs it again.
Writes docs/evidence/sql_explain.json and docs/evidence/sql_explain.md.

    python scripts/sql_optimisation.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import psycopg2  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402

PERF_INDEXES = ["ix_vehicle_fleet_id_id", "ix_alert_ts_id", "ix_alert_vehicle_ts", "ix_alert_critical_ts", "ix_audit_ts_id",
                "ix_audit_actor", "ix_audit_tenant_id", "ix_dlq_last_seen", "ix_metric_minute_minute"]


def main() -> None:
    c = (DockerContainer("postgres:16-alpine").with_exposed_ports(5432)
         .with_env("POSTGRES_USER", "rosetta").with_env("POSTGRES_PASSWORD", "bench-only").with_env("POSTGRES_DB", "rosetta")
         .with_command("postgres -c shared_buffers=256MB -c work_mem=16MB -c max_parallel_workers_per_gather=0"))
    c.start()
    try:
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(5432))
        dsn = f"postgresql://rosetta:bench-only@{host}:{port}/rosetta"
        for _ in range(120):
            try:
                psycopg2.connect(dsn, connect_timeout=2).close()
                break
            except Exception:
                time.sleep(0.5)
        run(dsn)
    finally:
        c.stop()


def run(dsn: str) -> None:
    os.environ["ROSETTA_DATABASE_URL"] = dsn.replace("postgresql://", "postgresql+psycopg2://")
    os.environ["ROSETTA_DATA_DIR"] = str(ROOT / "data" / "sqlbench")
    from rosetta.db.session import get_engine, init_db, session_scope
    from rosetta.services import seed

    init_db()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    for ix in PERF_INDEXES:                                   # start from the naive schema
        cur.execute(f"DROP INDEX IF EXISTS {ix}")
    print("seeding 100,000 vehicles")
    with session_scope() as s:
        seed.seed(s, vehicles=100_000, log=lambda *a: None)
    print("loading 1,000,000 alerts and 400,000 audit entries")
    cur.execute("""
        INSERT INTO alert (vehicle_id, kind, severity, ts, detected_ts, detail)
        SELECT 1 + (g::bigint * 7919) % 100000,
               (ARRAY['HARSH_BRAKE','HARSH_ACCEL','SPEEDING','LOW_SOC','DTC'])[1 + g % 5],
               (ARRAY['warning','warning','warning','critical','critical'])[1 + g % 5],
               1790000000000 + g * 350, 1790000000000 + g * 350 + 200,
               json_build_object('speed_kmh', g % 130)
        FROM generate_series(1, 1000000) g""")
    cur.execute("""
        INSERT INTO audit_log (ts, actor_kind, actor, tenant_id, action, resource, outcome, ip, detail, prev_hash, hash)
        SELECT now() - (g || ' seconds')::interval,
               (ARRAY['user','user','agent','service'])[1 + g % 4],
               'user' || (g % 300) || '@rosetta.example', 1 + g % 40,
               (ARRAY['api.get','mapping.approve','agent.tool.profile_fields','auth.login'])[1 + g % 4],
               '/api/v1/vehicles/' || g, 'ok', '10.0.0.1', '{}'::json, md5(g::text) || md5(g::text), md5((g+1)::text) || md5(g::text)
        FROM generate_series(1, 400000) g""")
    cur.execute("ANALYZE")

    cur.execute("SELECT id FROM tenant ORDER BY id LIMIT 1")
    tenant = cur.fetchone()[0]
    cur.execute("SELECT max(v.id) FROM vehicle v JOIN fleet f ON f.id = v.fleet_id WHERE f.tenant_id = %s", (tenant,))
    far_id = cur.fetchone()[0] - 50
    cur.execute("SELECT vehicle_id FROM alert GROUP BY vehicle_id ORDER BY count(*) DESC LIMIT 1")
    busy_vehicle = cur.fetchone()[0]

    cases = [
        {
            "name": "Fleet manager pages through their vehicles (page 400)",
            "why": "The console's vehicle list for one tenant. Offset pagination rereads every earlier page.",
            "before_sql": """SELECT v.id, v.vin, v.powertrain, o.key, f.name FROM vehicle v
                             JOIN fleet f ON f.id = v.fleet_id JOIN oem o ON o.id = v.oem_id
                             WHERE f.tenant_id = %(t)s ORDER BY v.id LIMIT 25 OFFSET 10000""",
            "after_sql": """SELECT v.id, v.vin, v.powertrain, o.key, f.name FROM vehicle v
                            JOIN fleet f ON f.id = v.fleet_id JOIN oem o ON o.id = v.oem_id
                            WHERE f.tenant_id = %(t)s AND v.id > %(after)s ORDER BY v.id LIMIT 25""",
            "change": "Keyset pagination (WHERE id > last seen id) instead of OFFSET, plus the composite index "
                      "(fleet_id, id) so each fleet's vehicles are read in id order.",
            "ddl": ["CREATE INDEX ix_vehicle_fleet_id_id ON vehicle (fleet_id, id)"],
            "params": {"t": tenant, "after": far_id},
        },
        {
            "name": "Latest alerts, newest first",
            "why": "The alerts panel refreshes every three seconds for every open console.",
            "before_sql": """SELECT a.id, a.kind, a.severity, a.ts, v.vin FROM alert a JOIN vehicle v ON v.id = a.vehicle_id
                             ORDER BY a.ts DESC, a.id DESC LIMIT 30""",
            "after_sql": None,
            "change": "Composite index (ts, id) matching the ORDER BY: the newest 30 rows are read from the end "
                      "of the index instead of sorting a million rows.",
            "ddl": ["CREATE INDEX ix_alert_ts_id ON alert (ts, id)"],
            "params": {},
        },
        {
            "name": "Alert history of one vehicle",
            "why": "The vehicle drawer and the agent's evidence both ask 'what happened to this vehicle'.",
            "before_sql": """SELECT id, kind, severity, ts FROM alert WHERE vehicle_id = %(v)s
                             ORDER BY ts DESC LIMIT 50""",
            "after_sql": None,
            "change": "Nothing to add. The unique constraint (vehicle_id, kind, ts) that makes alert inserts "
                      "idempotent already starts with vehicle_id, so the planner uses it. A second index "
                      "(vehicle_id, ts) was measured and removed from the schema: it cost writes and saved nothing.",
            "ddl": [],
            "params": {"v": busy_vehicle},
        },
        {
            "name": "Critical alerts only (partial index)",
            "why": "An on-call view shows only critical alerts. They are 40% of rows here and far fewer in real fleets.",
            "before_sql": """SELECT id, vehicle_id, kind, ts FROM alert WHERE severity = 'critical'
                             ORDER BY ts DESC LIMIT 30""",
            "after_sql": None,
            "change": "Partial index on (ts) WHERE severity = 'critical': smaller than a full index and exactly "
                      "what the query needs.",
            "ddl": ["CREATE INDEX ix_alert_critical_ts ON alert (ts) WHERE severity = 'critical'"],
            "params": {},
        },
        {
            "name": "Audit trail of one person",
            "why": "A compliance question: everything one user did, newest first.",
            "before_sql": """SELECT id, ts, action, resource FROM audit_log WHERE actor = %(a)s
                             ORDER BY id DESC LIMIT 50""",
            "after_sql": None,
            "change": "Composite index (actor, id).",
            "ddl": ["CREATE INDEX ix_audit_actor ON audit_log (actor, id)"],
            "params": {"a": "user17@rosetta.example"},
        },
        {
            "name": "Vehicles per source and powertrain (materialised view)",
            "why": "The sources page and reports aggregate the whole vehicle table on every load.",
            "before_sql": """SELECT o.key, v.powertrain, count(*) FROM vehicle v JOIN oem o ON o.id = v.oem_id
                             GROUP BY o.key, v.powertrain""",
            "after_sql": "SELECT oem, powertrain, vehicles FROM mv_fleet_mix",
            "change": "Materialised view refreshed when vehicles are added (REFRESH MATERIALIZED VIEW CONCURRENTLY "
                      "needs the unique index created with it).",
            "ddl": ["""CREATE MATERIALIZED VIEW mv_fleet_mix AS SELECT o.key AS oem, v.powertrain, count(*) AS vehicles
                       FROM vehicle v JOIN oem o ON o.id = v.oem_id GROUP BY o.key, v.powertrain""",
                    "CREATE UNIQUE INDEX ux_mv_fleet_mix ON mv_fleet_mix (oem, powertrain)"],
            "params": {},
        },
    ]

    def explain(sql: str, params: dict) -> tuple[float, str, list[float]]:
        times = []
        for _ in range(5):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
            times.append(cur.fetchone()[0][0]["Execution Time"])
        cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
        text = "\n".join(r[0] for r in cur.fetchall())
        return statistics.median(times), text, times

    results = []
    created: list[str] = []
    for case in cases:
        # every case starts from the same baseline: none of the other cases' indexes
        for ix in created:
            cur.execute(f"DROP INDEX IF EXISTS {ix}")
        created.clear()
        cur.execute("ANALYZE alert")
        before_ms, before_plan, _ = explain(case["before_sql"], case["params"])
        for ddl in case["ddl"]:
            cur.execute(ddl)
            if ddl.lstrip().upper().startswith("CREATE INDEX"):
                created.append(ddl.split()[2])
        cur.execute("ANALYZE")
        after_ms, after_plan, _ = explain(case["after_sql"] or case["before_sql"], case["params"])
        print(f"{case['name'][:60]:60s} {before_ms:9.2f} ms -> {after_ms:7.3f} ms  ({before_ms / max(after_ms, 1e-3):.0f}x)")
        results.append({**{k: case[k] for k in ("name", "why", "change", "ddl")},
                        "before_sql": " ".join(case["before_sql"].split()),
                        "after_sql": " ".join((case["after_sql"] or case["before_sql"]).split()),
                        "before_ms": round(before_ms, 3), "after_ms": round(after_ms, 3),
                        "speedup": round(before_ms / max(after_ms, 1e-3), 1),
                        "before_plan": before_plan, "after_plan": after_plan})

    cur.execute("SELECT version()")
    version = cur.fetchone()[0]
    cur.execute("SELECT (SELECT count(*) FROM vehicle), (SELECT count(*) FROM alert), (SELECT count(*) FROM audit_log)")
    counts = dict(zip(("vehicles", "alerts", "audit_entries"), cur.fetchone()))
    out = ROOT / "docs" / "evidence"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sql_explain.json").write_text(json.dumps({"postgres": version, "rows": counts, "cases": results}, indent=2))
    md = [f"# Query optimisation, measured\n\n{version.split(',')[0]}, in Docker on the development laptop. "
          f"Rows: {counts['vehicles']:,} vehicles, {counts['alerts']:,} alerts, {counts['audit_entries']:,} audit entries. "
          "Times are the median of five `EXPLAIN (ANALYZE, BUFFERS)` runs with a warm cache.\n",
          "| Query | Before | After | Change |", "|---|---:|---:|---|"]
    for r in results:
        md.append(f"| {r['name']} | {r['before_ms']:.2f} ms | {r['after_ms']:.3f} ms | {r['change']} |")
    for r in results:
        md += [f"\n## {r['name']}\n", r["why"], "\n**Before**\n", "```sql", r["before_sql"], "```", "```",
               r["before_plan"], "```", "\n**Change**\n", r["change"], "```sql", *r["ddl"], "```",
               "\n**After**\n", "```sql", r["after_sql"], "```", "```", r["after_plan"], "```"]
    (out / "sql_explain.md").write_text("\n".join(md) + "\n")
    print("wrote docs/evidence/sql_explain.md")
    cur.close()
    conn.close()
    get_engine().dispose()


if __name__ == "__main__":
    main()
