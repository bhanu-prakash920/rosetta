"""Local runtime: a small process supervisor.

In production every service is its own container and Kubernetes restarts what
dies. The local mode needs the same behaviour without any infrastructure, so
this supervisor starts each service as a child process and restarts it when it
exits. That is also what the chaos test relies on: kill a worker, watch it come
back and resume from its checkpoint.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProcSpec:
    name: str
    module: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ProcState:
    spec: ProcSpec
    proc: subprocess.Popen | None = None
    restarts: int = 0
    started_at: float = 0.0
    last_exit: int | None = None


def default_specs(normalizers: int = 4, processors: int = 2, sim_shards: int = 1,
                  vehicles: int = 100_000, simulator: bool = True) -> list[ProcSpec]:
    specs = []
    for i in range(normalizers):
        specs.append(ProcSpec(f"normalizer-{i}", "rosetta.pipeline.normalizer_worker", [str(i), str(normalizers)]))
    for i in range(processors):
        specs.append(ProcSpec(f"processor-{i}", "rosetta.pipeline.processor", [str(i), str(processors)]))
    specs.append(ProcSpec("dlq-0", "rosetta.pipeline.dlq"))
    if simulator:
        for i in range(sim_shards):
            specs.append(ProcSpec(f"simulator-{i}", "rosetta.simulator.run", [str(i), str(sim_shards)],
                                  {"ROSETTA_VEHICLES": str(vehicles)}))
    return specs


class Supervisor:
    def __init__(self, specs: list[ProcSpec], log_dir: Path | None = None) -> None:
        self.states = {s.name: ProcState(s) for s in specs}
        self.log_dir = log_dir
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _spawn(self, st: ProcState) -> None:
        env = {**os.environ, **st.spec.env, "PYTHONUNBUFFERED": "1"}
        out: Any = subprocess.DEVNULL
        if self.log_dir:
            out = open(self.log_dir / f"{st.spec.name}.log", "ab")
        st.proc = subprocess.Popen([sys.executable, "-m", st.spec.module, *st.spec.args],  # noqa: S603 - fixed argv, no shell
                                   env=env, stdout=out, stderr=subprocess.STDOUT)
        st.started_at = time.time()

    def start(self) -> None:
        with self._lock:
            for st in self.states.values():
                self._spawn(st)
        self._thread = threading.Thread(target=self._watch, daemon=True, name="supervisor")
        self._thread.start()

    def _watch(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                for st in self.states.values():
                    if st.proc is not None and st.proc.poll() is not None:
                        st.last_exit = st.proc.returncode
                        st.restarts += 1
                        self._spawn(st)

    def kill(self, name: str, sig: int = signal.SIGKILL) -> int | None:
        """Chaos hook: kill one child. The watcher restarts it."""
        with self._lock:
            st = self.states.get(name)
            if st is None or st.proc is None:
                return None
            pid = st.proc.pid
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return None
            return pid

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": n, "pid": st.proc.pid if st.proc else None,
                     "alive": bool(st.proc and st.proc.poll() is None), "restarts": st.restarts,
                     "last_exit": st.last_exit, "uptime_s": int(time.time() - st.started_at)}
                    for n, st in sorted(self.states.items())]

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        with self._lock:
            procs = [st.proc for st in self.states.values() if st.proc is not None]
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + timeout_s
        for p in procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
