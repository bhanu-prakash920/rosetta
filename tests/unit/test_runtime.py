"""rosetta.pipeline.runtime: the local process supervisor."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from rosetta.pipeline.runtime import ProcSpec, ProcState, Supervisor, default_specs

SLEEPER = "rosetta_unit_sleeper"
QUITTER = "rosetta_unit_quitter"


@pytest.fixture()
def modules(tmp_path) -> dict[str, str]:
    """Two trivial modules on a private PYTHONPATH: one sleeps, one exits at once with code 3."""
    (tmp_path / f"{SLEEPER}.py").write_text(
        "import os, sys, time\n"
        "print('started', os.environ.get('MARKER', ''), *sys.argv[1:], flush=True)\n"
        "time.sleep(60)\n")
    (tmp_path / f"{QUITTER}.py").write_text("import sys\nprint('bye', flush=True)\nsys.exit(3)\n")
    return {"PYTHONPATH": str(tmp_path)}


def wait_for(condition, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -------------------------------------------------------------- default_specs
def test_default_specs():
    specs = default_specs(normalizers=3, processors=2, sim_shards=2, vehicles=5000)
    assert [s.name for s in specs] == ["normalizer-0", "normalizer-1", "normalizer-2", "processor-0", "processor-1",
                                       "dlq-0", "simulator-0", "simulator-1"]
    by_name = {s.name: s for s in specs}
    assert by_name["normalizer-1"].module == "rosetta.pipeline.normalizer_worker"
    assert by_name["normalizer-1"].args == ["1", "3"], "each worker knows its index and the group size"
    assert by_name["processor-1"].args == ["1", "2"]
    assert by_name["dlq-0"].module == "rosetta.pipeline.dlq" and by_name["dlq-0"].args == []
    assert by_name["simulator-1"].args == ["1", "2"]
    assert by_name["simulator-1"].env == {"ROSETTA_VEHICLES": "5000"}
    assert by_name["normalizer-0"].env == {}


def test_default_specs_without_the_simulator():
    names = [s.name for s in default_specs(normalizers=1, processors=1, simulator=False)]
    assert names == ["normalizer-0", "processor-0", "dlq-0"]


def test_default_spec_names_are_unique():
    names = [s.name for s in default_specs()]
    assert len(names) == len(set(names)) == 4 + 2 + 1 + 1


def test_proc_spec_and_state_defaults_are_not_shared():
    a, b = ProcSpec("a", "m"), ProcSpec("b", "m")
    a.args.append("x")
    a.env["K"] = "v"
    assert b.args == [] and b.env == {}
    state = ProcState(a)
    assert (state.proc, state.restarts, state.last_exit) == (None, 0, None)


# ------------------------------------------------------------------ Supervisor
def test_status_before_start():
    sup = Supervisor([ProcSpec("b", "m"), ProcSpec("a", "m")])
    status = sup.status()
    assert [s["name"] for s in status] == ["a", "b"]
    assert all(s["pid"] is None and s["alive"] is False and s["restarts"] == 0 for s in status)
    assert sup.kill("a") is None, "nothing to kill yet"
    assert sup.kill("unknown") is None
    sup.stop(timeout_s=0.2)


def test_killed_child_is_restarted(modules, tmp_path):
    sup = Supervisor([ProcSpec("sleeper", SLEEPER, env=modules)], log_dir=tmp_path / "logs")
    sup.start()
    try:
        first = sup.status()[0]
        assert first["alive"] is True and first["restarts"] == 0 and first["last_exit"] is None
        assert first["pid"] != os.getpid()

        assert sup.kill("sleeper") == first["pid"]
        assert wait_for(lambda: sup.status()[0]["restarts"] == 1), "the watcher did not restart the child"

        second = sup.status()[0]
        assert second["alive"] is True
        assert second["pid"] != first["pid"]
        assert second["last_exit"] == -signal.SIGKILL
        assert not alive(first["pid"])
    finally:
        sup.stop(timeout_s=5.0)
    assert wait_for(lambda: not sup.status()[0]["alive"])
    assert sup.status()[0]["restarts"] == 1, "stop does not count as a crash"


def test_child_that_exits_by_itself_is_restarted_again_and_again(modules):
    sup = Supervisor([ProcSpec("quitter", QUITTER, env=modules)])
    sup.start()
    try:
        assert wait_for(lambda: sup.status()[0]["restarts"] >= 2)
        assert sup.status()[0]["last_exit"] == 3
    finally:
        sup.stop(timeout_s=5.0)


def test_only_the_killed_child_is_restarted(modules):
    sup = Supervisor([ProcSpec("a", SLEEPER, env=modules), ProcSpec("b", SLEEPER, env=modules)])
    sup.start()
    try:
        before = {s["name"]: s["pid"] for s in sup.status()}
        sup.kill("a")
        assert wait_for(lambda: {s["name"]: s["restarts"] for s in sup.status()}["a"] == 1)
        after = {s["name"]: s for s in sup.status()}
        assert after["b"]["pid"] == before["b"] and after["b"]["restarts"] == 0
        assert after["a"]["pid"] != before["a"]
    finally:
        sup.stop(timeout_s=5.0)


def test_child_gets_its_arguments_environment_and_a_log_file(modules, tmp_path):
    logs = tmp_path / "nested" / "logs"
    spec = ProcSpec("worker-7", SLEEPER, args=["7", "8"], env={**modules, "MARKER": "from-spec"})
    sup = Supervisor([spec], log_dir=logs)
    sup.start()
    try:
        log = logs / "worker-7.log"
        assert wait_for(lambda: log.exists() and b"started" in log.read_bytes())
        assert log.read_text().strip() == "started from-spec 7 8"

        sup.kill("worker-7")
        assert wait_for(lambda: log.read_text().count("started") == 2), "the log is appended to after a restart"
    finally:
        sup.stop(timeout_s=5.0)


def test_stop_terminates_every_child(modules):
    sup = Supervisor([ProcSpec(f"s{i}", SLEEPER, env=modules) for i in range(3)])
    sup.start()
    pids = [s["pid"] for s in sup.status()]
    assert all(alive(p) for p in pids)
    sup.stop(timeout_s=5.0)
    assert wait_for(lambda: not any(s["alive"] for s in sup.status()))
    time.sleep(0.7)                                      # longer than one watcher period
    assert all(s["restarts"] == 0 for s in sup.status()), "nothing is restarted after stop"


def test_kill_with_a_gentle_signal(modules):
    sup = Supervisor([ProcSpec("sleeper", SLEEPER, env=modules)])
    sup.start()
    try:
        sup.kill("sleeper", sig=signal.SIGTERM)
        assert wait_for(lambda: sup.status()[0]["restarts"] == 1)
        assert sup.status()[0]["last_exit"] == -signal.SIGTERM
    finally:
        sup.stop(timeout_s=5.0)


def test_kill_of_a_child_that_is_already_gone_returns_none(modules):
    sup = Supervisor([ProcSpec("quitter", QUITTER, env=modules)])
    sup._spawn(sup.states["quitter"])                    # no watcher: the child stays dead
    proc = sup.states["quitter"].proc
    proc.wait(timeout=10)
    assert sup.kill("quitter") is None
    assert sup.status()[0]["alive"] is False
    sup.stop(timeout_s=0.5)


def test_children_run_with_the_same_interpreter(modules, tmp_path):
    (Path(modules["PYTHONPATH"]) / "rosetta_unit_whoami.py").write_text(
        "import sys, time\nprint(sys.executable, flush=True)\ntime.sleep(60)\n")
    sup = Supervisor([ProcSpec("who", "rosetta_unit_whoami", env=modules)], log_dir=tmp_path / "logs")
    sup.start()
    try:
        log = tmp_path / "logs" / "who.log"
        assert wait_for(lambda: log.exists() and log.read_text().strip() != "")
        assert os.path.realpath(log.read_text().strip()) == os.path.realpath(sys.executable)
    finally:
        sup.stop(timeout_s=5.0)
