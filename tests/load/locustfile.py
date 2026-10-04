"""API load test. The pipeline should be running, so the API is measured under real load.

    python -m rosetta up --port 8765            # in one terminal
    locust -f tests/load/locustfile.py --headless -u 60 -r 20 -t 60s \\
           --host http://127.0.0.1:8765 --csv docs/evidence/locust --only-summary

Targets from the problem statement: p95 under 200 ms, p99 under 500 ms.
"""
from __future__ import annotations

import os
import random

from locust import HttpUser, between, events, task

PASSWORD = os.environ.get("ROSETTA_DEMO_PASSWORD", "rosetta-demo-2026")
SOURCES = ["nordvik", "pacifica", "stellaris", "kaizen", "voltaic"]


class Console(HttpUser):
    """Base class: signs in once, then behaves like someone using the console."""
    abstract = True
    wait_time = between(0.2, 1.0)
    email = ""

    tokens: dict[str, str] = {}
    lock = __import__("threading").Lock()

    def on_start(self) -> None:
        # Real consoles sign in once and reuse the token. Sixty simulated users signing
        # in as three accounts would, correctly, be throttled as password guessing.
        with Console.lock:          # one sign-in per account, the others wait for its token
            tok = Console.tokens.get(self.email)
            if tok is None:
                r = self.client.post("/api/v1/auth/token", data={"username": self.email, "password": PASSWORD},
                                     name="POST /auth/token")
                r.raise_for_status()
                tok = Console.tokens[self.email] = r.json()["access_token"]
        self.h = {"Authorization": f"Bearer {tok}"}
        self.vins: list[str] = []
        self.cursor = None

    def get(self, path: str, name: str):
        return self.client.get("/api/v1" + path, headers=self.h, name=name)


class Engineer(Console):
    weight = 3
    email = "engineer@rosetta.example"

    @task(6)
    def overview(self):
        self.get("/overview", "GET /overview")

    @task(4)
    def series(self):
        self.get("/series/all?seconds=120", "GET /series/all")

    @task(3)
    def sources(self):
        self.get("/oems", "GET /oems")

    @task(2)
    def dead_letters(self):
        self.get("/dlq/groups?open_only=true", "GET /dlq/groups")

    @task(2)
    def mapping(self):
        self.get(f"/mappings/{random.choice(SOURCES)}/1", "GET /mappings/{oem}/{version}")

    @task(3)
    def map_points(self):
        la, lo = random.choice([(13.08, 80.27), (12.97, 77.59), (19.07, 72.87), (28.61, 77.20)])
        self.get(f"/map/points?south={la - .4}&north={la + .4}&west={lo - .4}&east={lo + .4}&limit=5000",
                 "GET /map/points")

    @task(1)
    def audit(self):
        self.get("/audit?limit=25", "GET /audit")


class FleetManager(Console):
    weight = 5
    email = "manager@northwind.example"

    @task(5)
    def vehicles(self):
        q = "/vehicles?limit=25" + (f"&cursor={self.cursor}" if self.cursor else "")
        r = self.get(q, "GET /vehicles")
        if r.ok:
            j = r.json()
            self.cursor = j["next_cursor"]
            self.vins = [v["vin"] for v in j["items"]] or self.vins

    @task(4)
    def vehicle(self):
        if self.vins:
            self.get(f"/vehicles/{random.choice(self.vins)}", "GET /vehicles/{vin}")

    @task(4)
    def alerts(self):
        self.get("/alerts?limit=30", "GET /alerts")

    @task(3)
    def map_points(self):
        self.get("/map/points?limit=5000", "GET /map/points")

    @task(2)
    def cells(self):
        self.get("/map/cells?precision=4", "GET /map/cells")

    @task(1)
    def history(self):
        if self.vins:
            self.get(f"/vehicles/{random.choice(self.vins)}/history?limit=100", "GET /vehicles/{vin}/history")


class Analyst(Console):
    weight = 2
    email = "analyst@rosetta.example"

    @task(3)
    def vehicles(self):
        self.get("/vehicles?limit=50", "GET /vehicles")

    @task(2)
    def quality(self):
        self.get("/batch/quality", "GET /batch/quality")

    @task(2)
    def map_points(self):
        self.get("/map/points?limit=5000", "GET /map/points")


@events.quitting.add_listener
def check_targets(environment, **_):
    """Fail the run when the latency targets are missed, so CI can gate on it."""
    s = environment.stats.total
    p95, p99 = s.get_response_time_percentile(0.95), s.get_response_time_percentile(0.99)
    print(f"\\nrequests={s.num_requests} failures={s.num_failures} p50={s.median_response_time}ms "
          f"p95={p95}ms p99={p99}ms rps={s.total_rps:.0f}")
    if s.num_failures or p95 > 200 or p99 > 500:
        environment.process_exit_code = 1
