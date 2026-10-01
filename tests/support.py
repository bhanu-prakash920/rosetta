"""Test harness: a small but complete Rosetta world running inside the test process.

Everything is real except the scale: the real broker adapter (file-backed log),
the real database engine (SQLite), the real workers. Only the fleet is small.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import orjson

ALL_SOURCES = ["nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix"]
KNOWN_SOURCES = [s for s in ALL_SOURCES if s != "helix"]
PASSWORD = "rosetta-demo-2026"


def fresh_environment(data_dir: Path) -> None:
    """Point every singleton at a new data directory."""
    from rosetta import config
    from rosetta.db import session

    os.environ.update({"ROSETTA_DATA_DIR": str(data_dir), "ROSETTA_ENV": "test",
                       "ROSETTA_BROKER": "log", "ROSETTA_STATE_STORE": "memory",
                       "ROSETTA_ARCHIVE": "fs", "ROSETTA_TELEMETRY_STORE": "parquet",
                       "ROSETTA_LLM_PROVIDER": "none", "ROSETTA_DEMO_PASSWORD": PASSWORD,
                       "ROSETTA_RATE_LIMIT_PER_MIN": "100000"})
    for k in ("ROSETTA_DATABASE_URL", "ROSETTA_JWT_SECRET", "ROSETTA_OIDC_JWKS_URL", "ANTHROPIC_API_KEY",
              "ANTHROPIC_AUTH_TOKEN", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        os.environ.pop(k, None)
    config.reset_settings()
    session.reset_engine()


class World:
    """Database, broker, simulator and workers for `vehicles` simulated vehicles."""

    def __init__(self, vehicles: int = 3000, sources: list[str] | None = None, seed: int = 7,
                 faults: bool = True, mode: str = "all") -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="rosetta-test-"))
        fresh_environment(self.dir)
        from rosetta.db.session import init_db, session_scope
        from rosetta.factory import load_vehicle_index, make_broker, make_hot_state
        from rosetta.pipeline.dlq import DlqWorker
        from rosetta.pipeline.metrics import Aggregator
        from rosetta.pipeline.normalizer_worker import NormalizerWorker
        from rosetta.pipeline.processor import Processor
        from rosetta.ports.broker import T_METRICS
        from rosetta.services import agent_service
        from rosetta.services import seed as seeding
        from rosetta.simulator.fleet import Fleet
        from rosetta.simulator.run import SimSettings, SimulatorRunner

        init_db()
        with session_scope() as s:
            self.seeded = seeding.seed(s, vehicles=vehicles, seed_value=seed, log=lambda *a: None)
        with session_scope() as s:
            agent_service.seed_memory(s, vehicles=600, ticks=20, log=lambda *a: None)
        vins, _ = load_vehicle_index()
        make_hot_state(vins, writable=True).close()
        self.vins = vins
        self.broker = make_broker()
        self.fleet = Fleet(vehicles, seed=seed)
        st = SimSettings(mode=mode, enabled=list(sources or KNOWN_SOURCES))
        if not faults:
            st.dup_pct = st.reorder_pct = st.malformed_pct = 0.0
        self.sim = SimulatorRunner(fleet=self.fleet, broker=self.broker, listen_control=False, settings=st)
        self.normalizer = NormalizerWorker(broker=self.broker)
        self.processor = Processor(broker=self.broker)
        self.dlq = DlqWorker(broker=self.broker)
        self.agg = Aggregator()
        self._metrics = self.broker.consumer(T_METRICS, "test-metrics", start="beginning")
        self.clock = 1_790_200_000_000

    # ------------------------------------------------------------------ drive
    def tick(self, n: int = 1) -> int:
        sent = 0
        for _ in range(n):
            self.clock += 1000
            sent += self.sim.tick(now_ms=self.clock, dt=1.0)
        return sent

    def release_delayed(self) -> int:
        """Deliver what the simulator is still holding back (reordered and duplicated events)."""
        return self.sim._release(time.monotonic() + 60.0)

    def pump(self, rounds: int = 4) -> None:
        """Run every worker until the queues are empty."""
        for _ in range(rounds):
            moved = self.dlq.run_queued_jobs()      # the worker does this on a timer
            while True:
                k = self.dlq.step(0.0) + self.normalizer.step(0.0) + self.processor.step(0.0)
                moved += k
                if not k:
                    break
            if not moved:
                break
        self.processor.flush_archive()
        self.processor.consumer.commit()

    def run(self, ticks: int = 5) -> None:
        self.tick(ticks)
        self.release_delayed()
        self.pump()

    def reload(self) -> bool:
        return self.normalizer.maybe_reload(force=True)

    def metrics(self) -> dict[str, Any]:
        self.sim.publish()
        self.normalizer.publish()
        self.processor.publish()
        for r in self._metrics.poll(10_000, 0.0):
            self.agg.ingest(orjson.loads(r.value))
        return self.agg.overview()

    def by_source(self) -> dict[str, dict[str, Any]]:
        return {o["oem"]: o for o in self.metrics()["oems"]}

    def close(self) -> None:
        try:
            self.processor.hot.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def onboard(world: World, source: str, spec: dict[str, Any], canary_pct: int = 100, promote: bool = True,
            label: str | None = None) -> int:
    """Create, validate, approve and optionally promote a mapping as an engineer would."""
    from rosetta.db.session import session_scope
    from rosetta.pipeline.dlq import queue_replay
    from rosetta.services import registry

    with session_scope() as s:
        mv = registry.create_version(s, source, spec, source="human", actor="engineer@rosetta.example")
        v = mv.version
        registry.validate_version(s, source, v, actor="engineer@rosetta.example", label=label or source)
        registry.transition(s, source, v, "approve", actor="engineer@rosetta.example", canary_pct=canary_pct)
        if promote:
            registry.transition(s, source, v, "promote", actor="engineer@rosetta.example")
        queue_replay(s, source, trigger="test")
    world.reload()
    return v
