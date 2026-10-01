"""Run the Locust API load test headless and write docs/evidence/locust.json.

    python scripts/run_api_load.py --host http://127.0.0.1:8765 --users 60 --seconds 60
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:8765")
    ap.add_argument("--users", type=int, default=60)
    ap.add_argument("--rate", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=60)
    a = ap.parse_args()
    prefix = ROOT / "data" / "locust"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "locust", "-f", str(ROOT / "tests" / "load" / "locustfile.py"), "--headless",
           "-u", str(a.users), "-r", str(a.rate), "-t", f"{a.seconds}s", "--host", a.host, "--csv", str(prefix),
           "--only-summary"]
    # Argument list without a shell, run by a developer on their own machine: no injection.
    proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
    rows = list(csv.DictReader(open(f"{prefix}_stats.csv")))
    per = []
    total = None
    for r in rows:
        item = {"name": r["Name"], "requests": int(r["Request Count"]), "failures": int(r["Failure Count"]),
                "p50": float(r["50%"]), "p95": float(r["95%"]), "p99": float(r["99%"]), "rps": round(float(r["Requests/s"]), 1)}
        if r["Name"] == "Aggregated":
            total = item
        else:
            per.append(item)
    out = {"tool": "locust", "users": a.users, "seconds": a.seconds, "host": a.host,
           "requests": total["requests"], "failures": total["failures"], "p50": total["p50"], "p95": total["p95"],
           "p99": total["p99"], "rps": total["rps"], "per_endpoint": sorted(per, key=lambda x: -x["p95"]),
           "targets_met": total["failures"] == 0 and total["p95"] < 200 and total["p99"] < 500}
    (ROOT / "docs" / "evidence" / "locust.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "per_endpoint"}, indent=1))
    for e in out["per_endpoint"][:6]:
        print(f"  {e['name']:34s} p95 {e['p95']:6.0f} ms  p99 {e['p99']:6.0f} ms  {e['requests']} req")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
