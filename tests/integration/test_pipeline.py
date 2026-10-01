"""The pipeline end to end: simulator, gateway, broker, normaliser, processor, dead letters.

Real broker adapter, real database, real workers, a small fleet.
"""
from __future__ import annotations

import orjson
import pytest
from sqlalchemy import func, select

from rosetta.db.models import Alert, DlqGroup
from rosetta.db.session import session_scope
from rosetta.ports.broker import T_CANONICAL, T_DLQ, T_RAW, Record
from rosetta.simulator.dialects import SPEC_HELIX, SPEC_PACIFICA_V2
from tests.support import ALL_SOURCES, World, onboard


def test_every_event_is_accounted_for(fresh_world: World):
    """Sent = translated + dead-lettered + duplicates dropped. Nothing disappears."""
    w = fresh_world
    w.run(ticks=6)
    m = w.metrics()
    t = m["totals"]
    assert t["sent"] > 10_000
    assert t["sent"] == t["ok"] + t["failed"] + t["duplicates"]
    assert m["counters"]["processed"] == t["ok"]
    assert m["counters"]["archived_rows"] == t["ok"]


def test_injected_faults_are_handled(fresh_world: World):
    w = fresh_world
    w.sim.s.dup_pct, w.sim.s.malformed_pct, w.sim.s.reorder_pct = 5.0, 2.0, 5.0
    w.run(ticks=6)
    m = w.metrics()
    c = m["counters"]
    assert c["sim_duplicates"] > 0 and c["sim_malformed"] > 0 and c["sim_reordered"] > 0
    # every duplicate the simulator sent was dropped, unless the duplicate itself was malformed
    assert 0 < m["totals"]["duplicates"] <= c["sim_duplicates"]
    reasons = {r for o in m["oems"] for r in o["reasons"]}
    assert reasons <= {"DECODE_ERROR", "INVALID", "SCHEMA_MISMATCH", "TRANSFORM_ERROR"}
    assert m["totals"]["failed"] > 0


def test_unknown_source_is_parked_not_lost(fresh_world: World):
    w = fresh_world
    w.sim.s.enabled = list(ALL_SOURCES)
    w.run(ticks=4)
    helix = w.by_source()["helix"]
    assert helix["total_ok"] == 0
    assert helix["reasons"].get("NO_ADAPTER", 0) == helix["total_failed"] > 0
    with session_scope() as s:
        parked = s.execute(select(func.sum(DlqGroup.count)).where(DlqGroup.oem_key == "helix")).scalar()
    assert parked == helix["total_failed"]


def test_onboarding_replays_parked_messages_without_restart(fresh_world: World):
    w = fresh_world
    w.sim.s.enabled = list(ALL_SOURCES)
    w.sim.s.dup_pct = w.sim.s.malformed_pct = w.sim.s.reorder_pct = 0.0
    w.run(ticks=4)
    parked = w.by_source()["helix"]["total_failed"]
    assert parked > 0
    worker = w.normalizer            # the same object before and after: no restart
    epoch_before = worker.norm.table.epoch

    onboard(w, "helix", SPEC_HELIX)
    assert w.normalizer is worker and worker.norm.table.epoch > epoch_before
    w.pump()
    m = w.metrics()
    helix = {o["oem"]: o for o in m["oems"]}["helix"]
    assert helix["total_ok"] == parked                    # every parked message came back
    assert m["counters"]["replayed_ok"] == parked
    with session_scope() as s:
        open_ = s.execute(select(func.sum(DlqGroup.count - DlqGroup.replayed))
                          .where(DlqGroup.oem_key == "helix")).scalar()
    assert open_ == 0

    w.run(ticks=2)                                        # and new traffic flows directly
    assert w.by_source()["helix"]["total_ok"] > parked


def test_replay_twice_does_not_duplicate(fresh_world: World):
    from rosetta.pipeline.dlq import queue_replay

    w = fresh_world
    w.sim.s.enabled = list(ALL_SOURCES)
    w.sim.s.dup_pct = w.sim.s.malformed_pct = w.sim.s.reorder_pct = 0.0
    w.run(ticks=3)
    onboard(w, "helix", SPEC_HELIX)
    w.pump()
    ok = w.by_source()["helix"]["total_ok"]
    with session_scope() as s:
        queue_replay(s, "helix", trigger="again")
    w.pump()
    assert w.by_source()["helix"]["total_ok"] == ok


def test_format_drift_both_firmwares_live(fresh_world: World):
    """After an over-the-air update part of a fleet speaks the new format. Both must be read."""
    w = fresh_world
    w.sim.s.drift_pct = 40
    w.sim.s.dup_pct = w.sim.s.malformed_pct = w.sim.s.reorder_pct = 0.0
    w.run(ticks=3)
    before = w.by_source()["pacifica"]
    assert before["reasons"].get("SCHEMA_MISMATCH", 0) > 0

    v = onboard(w, "pacifica", SPEC_PACIFICA_V2, canary_pct=25, promote=False, label="pacifica_v2")
    routes = w.normalizer.norm.table.describe()["pacifica"]
    assert routes == {"active": [1], "canary": v, "canary_pct": 25}
    w.pump()
    w.run(ticks=3)
    after = w.by_source()["pacifica"]
    versions = {x["version"]: x["events"] for x in after["versions"]}
    assert versions[1] > 0 and versions[v] > 0
    assert after["total_failed"] == before["total_failed"]        # no new failures once v2 is live
    assert w.metrics()["counters"]["fallbacks"] > 0               # some devices needed a second attempt


def test_a_bad_canary_never_hurts_traffic(fresh_world: World):
    """A canary that maps speed in the wrong unit produces valid-looking events, so the
    golden set must stop it. This checks the gate, not the router."""
    from rosetta.services import registry

    bad = {**SPEC_HELIX, "fields": {**SPEC_HELIX["fields"], "speed_kmh": {"path": "fahrt.v"}}}
    with session_scope() as s:
        mv = registry.create_version(s, "helix", bad, source="human", actor="e")
        mv, rep = registry.validate_version(s, "helix", mv.version, actor="e", label="helix")
        assert mv.state == "draft" and rep.pass_rate < 0.5
        with pytest.raises(registry.RegistryError):
            registry.transition(s, "helix", mv.version, "approve", actor="e")


def test_hot_state_keeps_newest_event(fresh_world: World):
    w = fresh_world
    w.sim.s.dup_pct = w.sim.s.malformed_pct = 0.0
    w.sim.s.reorder_pct = 20.0
    w.run(ticks=8)
    hot = w.processor.hot
    seen = 0
    for i in range(0, len(w.vins), 37):
        st = hot.get(w.vins[i])
        if st is None:
            continue
        seen += 1
        assert st["seq"] == int(w.fleet.seq[i])          # the newest sequence number won
        assert st["events"] >= 1
    assert seen > 20


def test_archive_has_one_row_per_event(fresh_world: World):
    import pyarrow.compute as pc

    w = fresh_world
    w.run(ticks=5)
    t = w.processor.archive.dataset().to_table(columns=["vin", "seq", "geohash5", "oem"])
    assert t.num_rows == w.metrics()["totals"]["ok"]
    keys = pc.binary_join_element_wise(t["vin"], pc.cast(t["seq"], "string"), ":")
    assert pc.count_distinct(keys).as_py() == t.num_rows
    assert all(len(g) == 5 for g in t["geohash5"].to_pylist()[:200])


def test_alerts_are_idempotent(fresh_world: World):
    w = fresh_world
    w.sim.s.dup_pct = 30.0
    w.fleet.evt[:] = 0
    w.run(ticks=6)
    with session_scope() as s:
        total = s.execute(select(func.count()).select_from(Alert)).scalar()
        distinct = s.execute(select(func.count()).select_from(
            select(Alert.vehicle_id, Alert.kind, Alert.ts).distinct().subquery())).scalar()
    assert total == distinct


def test_worker_restart_resumes_from_checkpoint(fresh_world: World):
    """Kill the normaliser between batches. A new one must continue without loss."""
    from rosetta.pipeline.normalizer_worker import NormalizerWorker

    w = fresh_world
    w.sim.s.dup_pct = w.sim.s.malformed_pct = w.sim.s.reorder_pct = 0.0
    sent = w.tick(3)
    w.normalizer.step(0.0)                 # one batch processed...
    w.normalizer._checkpoint()             # ...and checkpointed
    done = w.normalizer.processed
    assert 0 < done < sent
    w.normalizer.publish()
    w.normalizer = NormalizerWorker(broker=w.broker)      # the replacement process
    w.pump()
    m = w.metrics()
    assert m["totals"]["ok"] + m["totals"]["failed"] + m["totals"]["duplicates"] == sent
    assert m["totals"]["duplicates"] == 0


def test_crash_before_commit_redelivers_and_sink_deduplicates(fresh_world: World):
    """At-least-once in, effectively-once out: work that was not committed is redone,
    and the processor drops what it had already seen."""
    from rosetta.pipeline.normalizer_worker import NormalizerWorker

    w = fresh_world
    w.sim.s.dup_pct = w.sim.s.malformed_pct = w.sim.s.reorder_pct = 0.0
    sent = w.tick(2)
    recs = w.normalizer.consumer.poll(w.normalizer.batch, 0.0)
    res = w.normalizer.norm.process(recs)
    w.broker.produce(T_CANONICAL, res.canonical)          # output written, offsets NOT committed
    crashed = len(res.canonical)
    w.normalizer = NormalizerWorker(broker=w.broker)      # crash and restart
    w.pump()
    w.processor.publish()
    t = w.processor.archive.dataset().to_table(columns=["vin"])
    assert t.num_rows == sent                              # each event stored once
    assert w.metrics()["counters"]["sink_duplicates"] == crashed


def test_oversize_and_garbage_do_not_stop_the_worker(fresh_world: World):
    w = fresh_world
    junk = [Record(key=b"dev-1", value=b"\x00\xff" * 10, headers={"oem": "nordvik", "rx": 1}),
            Record(key=b"dev-2", value=b"{" * 70_000, headers={"oem": "nordvik", "rx": 1}),
            Record(key=b"dev-3", value=b"", headers={"oem": "pacifica", "rx": 1}),
            Record(key=b"dev-4", value=b'{"a":1}', headers={"oem": "does-not-exist", "rx": 1}),
            Record(key=b"dev-5", value=b"null", headers={"oem": "stellaris", "rx": 1}),
            Record(key=b"dev-6", value=b'{"vehicle": 7}', headers={"oem": "nordvik", "rx": 1})]
    w.broker.produce(T_RAW, junk)
    w.run(ticks=1)
    dead = w.broker.consumer(T_DLQ, "check", start="beginning").poll(10_000, 0.0)
    reasons = {(r.headers["dev"], r.headers["reason"]) for r in dead if r.headers["dev"].startswith("dev-")}
    assert reasons == {("dev-1", "DECODE_ERROR"), ("dev-2", "OVERSIZE"), ("dev-3", "DECODE_ERROR"),
                       ("dev-4", "NO_ADAPTER"), ("dev-5", "DECODE_ERROR"),
                       ("dev-6", "SCHEMA_MISMATCH")}
    assert w.metrics()["totals"]["ok"] > 0


def test_canonical_events_are_valid_json_for_consumers(fresh_world: World):
    from rosetta.domain import canonical

    w = fresh_world
    w.run(ticks=2)
    recs = w.broker.consumer(T_CANONICAL, "check", start="beginning").poll(500, 0.0)
    assert recs
    for r in recs:
        ev = orjson.loads(r.value)
        assert canonical.validate(ev) is None
        assert r.key.decode() == ev["vin"]            # keyed by VIN: per-vehicle order downstream
        assert {"oem", "map_v", "rx_ts", "norm_ts"} <= set(ev)


def test_crash_between_archive_write_and_commit_stores_nothing_twice(fresh_world: World):
    """The narrowest window: the file is on disk, the offsets are not committed yet."""
    import pyarrow.compute as pc

    from rosetta.pipeline.processor import Processor

    w = fresh_world
    w.sim.s.dup_pct = w.sim.s.malformed_pct = w.sim.s.reorder_pct = 0.0
    sent = w.tick(3)
    while w.normalizer.step(0.0):
        pass
    p = w.processor
    recs = p.consumer.poll(4000, 0.0)
    assert p.handle(recs) == len(recs) > 0
    p.flush_archive()                                   # durable...
    committed = p.consumer.committed()
    assert all(v == 0 for v in committed.values())      # ...but nothing committed: now it dies
    w.processor = Processor(broker=w.broker)            # the replacement
    assert w.processor.consumer.positions() == p.consumer.positions()
    w.pump()
    t = w.processor.archive.dataset().to_table(columns=["vin", "seq"])
    keys = pc.binary_join_element_wise(t["vin"], pc.cast(t["seq"], "string"), ":")
    assert t.num_rows == sent == pc.count_distinct(keys).as_py()
