from __future__ import annotations

import numpy as np
import orjson
from behave import given, then, when
from sqlalchemy import func, select, update

from rosetta.db.models import AgentStep, Alert, AuditLog, DlqGroup, Driver, VehicleDriverAssignment
from rosetta.db.session import session_scope
from rosetta.ports.broker import T_ALERTS
from rosetta.services import agent_service, audit, registry
from rosetta.simulator.dialects import SPEC_HELIX
from rosetta.simulator.fleet import EVT_CODE, OEM_INDEX
from tests.support import KNOWN_SOURCES, PASSWORD, World

ENG = "engineer@rosetta.example"
AGENT = "agent@rosetta.example"


def clean(w: World) -> None:
    w.sim.s.dup_pct = w.sim.s.reorder_pct = w.sim.s.malformed_pct = 0.0


def source(w: World, key: str):
    return w.by_source().get(key)


# ------------------------------------------------------------------- platform
@given("a running platform with {n:d} simulated vehicles")
def step_platform(context, n):
    context.world = World(vehicles=n, sources=[], mode="all")
    clean(context.world)


@given("the known makers are sending telemetry")
def step_known(context):
    context.world.sim.s.enabled = list(KNOWN_SOURCES)


@given("the network is clean")
def step_clean(context):
    context.world.sim.s.enabled = list(KNOWN_SOURCES)
    clean(context.world)


@given("the network duplicates {p:d} percent of messages")
def step_dups(context, p):
    context.world.sim.s.enabled = list(KNOWN_SOURCES)
    context.world.sim.s.dup_pct = float(p)


@given("the network delays {p:d} percent of messages")
def step_delay(context, p):
    context.world.sim.s.enabled = list(KNOWN_SOURCES)
    context.world.sim.s.reorder_pct = float(p)


@when('"{oem}" starts sending telemetry')
def step_start(context, oem):
    w = context.world
    w.run(ticks=2)
    context.memo["known_before"] = sum(o["total_ok"] for o in w.metrics()["oems"])
    w.sim.s.enabled = [*w.sim.s.enabled, oem]


@given('"{oem}" has been sending for {sec:d} seconds')
def step_sending(context, oem, sec):
    w = context.world
    w.sim.s.enabled = [*KNOWN_SOURCES, oem]
    w.sim.s.mode = "realistic"
    for _ in range(sec):
        w.tick()
        w.pump(rounds=1)
    w.release_delayed()
    w.pump()


@when("the fleet runs for {sec:d} seconds")
@given("the fleet has run for {sec:d} seconds")
def step_run(context, sec):
    context.world.run(ticks=sec)


@when("the fleet runs for {sec:d} seconds without being processed")
def step_run_unprocessed(context, sec):
    context.memo["sent"] = context.world.tick(sec)


@when('{p:d} percent of "{oem}" vehicles receive the firmware update')
@given('{p:d} percent of "{oem}" vehicles have received the firmware update')
def step_drift(context, p, oem):
    w = context.world
    w.sim.s.drift_pct = p
    w.sim.s.mode = "realistic"
    for _ in range(30):
        w.tick()
        w.pump(rounds=1)
    w.pump()
    context.memo["mismatch"] = source(w, oem)["reasons"].get("SCHEMA_MISMATCH", 0)


# ---------------------------------------------------------------- dead letters
@then('no "{oem}" event has been translated')
def step_none(context, oem):
    assert source(context.world, oem)["total_ok"] == 0


@then('every "{oem}" message is parked with the reason "{reason}"')
def step_parked(context, oem, reason):
    o = source(context.world, oem)
    assert o["total_failed"] > 0 and o["reasons"] == {reason: o["total_failed"]}, o["reasons"]
    with session_scope() as s:
        n = s.execute(select(func.sum(DlqGroup.count)).where(DlqGroup.oem_key == oem)).scalar()
    assert n == o["total_failed"]


@then('"{oem}" messages are parked with the reason "{reason}"')
def step_parked_some(context, oem, reason):
    assert source(context.world, oem)["reasons"].get(reason, 0) > 0


@then('the most failing field is "{key}"')
def step_top(context, key):
    top = context.world.metrics()["top_failing_fields"]
    assert top and top[0]["key"].startswith(key), top[:3]


@then("the known makers are still translated without interruption")
def step_uninterrupted(context):
    now = sum(o["total_ok"] for o in context.world.metrics()["oems"] if o["oem"] in KNOWN_SOURCES)
    assert now > context.memo["known_before"]
    for k in KNOWN_SOURCES:
        assert source(context.world, k)["total_failed"] == 0


@then('"{oem}" vehicles on the old firmware are still translated')
def step_old_ok(context, oem):
    o = source(context.world, oem)
    assert o["total_ok"] > o["total_failed"] > 0


# ----------------------------------------------------------------------- agent
def run_agent(context, oem):
    with session_scope() as s:
        run = agent_service.run_agent(s, oem, requested_by=ENG, engine="workflow")
        res = run.__dict__["_result"]
        context.memo.update({"run_id": run.id, "result": res, "oem": oem,
                             "version": res["result"].get("version"), "status": run.status})
    return res


@when('an engineer asks the agent for a mapping for "{oem}"')
def step_ask(context, oem):
    run_agent(context, oem)


@given('the agent has submitted a validated draft for "{oem}"')
def step_draft_ready(context, oem):
    res = run_agent(context, oem)
    assert res["result"]["state"] == "validated", res


@then('the agent submits a draft in state "{state}"')
def step_draft_state(context, state):
    assert context.memo["result"]["result"]["state"] == state
    assert context.memo["status"] == "awaiting_approval"


@then("the draft passes {p:d} percent of the golden cases")
def step_golden(context, p):
    r = context.memo["result"]["result"]
    assert r["golden_cases"] >= 150 and r["pass_rate"] * 100 == p, r


@then('the draft maps the source field "{path}" to "{canonical}" as "{encoding}"')
def step_maps(context, path, canonical, encoding):
    f = next(x for x in context.memo["result"]["final"] if x["canonical"] == canonical)
    assert (f["path"], f["encoding"]) == (path, encoding), f


@then("nothing reads the draft yet")
def step_inert(context):
    w = context.world
    w.reload()
    assert context.memo["oem"] not in w.normalizer.norm.table.describe()
    w.run(ticks=2)
    assert source(w, context.memo["oem"])["total_ok"] == 0


# -------------------------------------------------------------------- approval
def act(context, action, **kw):
    with session_scope() as s:
        registry.transition(s, context.memo["oem"], context.memo["version"], action, actor=ENG, **kw)


@when("an engineer approves and promotes the draft")
def step_approve_promote(context):
    from rosetta.pipeline.dlq import queue_replay

    w = context.world
    context.memo["worker"] = w.normalizer
    context.memo["parked"] = source(w, context.memo["oem"])["total_failed"]
    act(context, "approve", canary_pct=100)
    act(context, "promote")
    with session_scope() as s:
        queue_replay(s, context.memo["oem"], trigger="promote")
    w.reload()
    w.pump()


@when("an engineer approves the draft for {p:d} percent of vehicles")
def step_approve_canary(context, p):
    from rosetta.pipeline.dlq import queue_replay

    w = context.world
    context.memo["parked_before"] = source(w, context.memo["oem"])["reasons"].get("SCHEMA_MISMATCH", 0)
    act(context, "approve", canary_pct=p)
    with session_scope() as s:
        queue_replay(s, context.memo["oem"], trigger="approve")
    w.reload()
    w.pump()


@then("the workers load the mapping without restarting")
def step_no_restart(context):
    w = context.world
    assert w.normalizer is context.memo["worker"]
    assert w.normalizer.norm.table.describe()[context.memo["oem"]]["active"] == [context.memo["version"]]


@then('every parked "{oem}" message that was not damaged is replayed')
def step_replayed(context, oem):
    w = context.world
    o = source(w, oem)
    assert o["total_ok"] >= context.memo["parked"] * 0.98, (o["total_ok"], context.memo["parked"])
    assert w.metrics()["counters"]["replayed_ok"] == o["total_ok"]
    context.memo["after_replay"] = o["total_ok"]


@then('new "{oem}" telemetry is translated directly')
def step_direct(context, oem):
    w = context.world
    w.run(ticks=3)
    assert source(w, oem)["total_ok"] > context.memo["after_replay"]


@then('"{oem}" has live version {a:d} and canary version {c:d}')
def step_versions(context, oem, a, c):
    r = context.world.normalizer.norm.table.describe()[oem]
    assert r["active"] == [a] and r["canary"] == c, r


@then("both versions translate events")
def step_both(context):
    per = {v["version"]: v["events"] for v in source(context.world, context.memo["oem"])["versions"]}
    assert per.get(1, 0) > 0 and per.get(2, 0) > 0, per


@then('no new "{oem}" message is parked for "{reason}"')
def step_no_new(context, oem, reason):
    assert source(context.world, oem)["reasons"].get(reason, 0) == context.memo["parked_before"]


# ----------------------------------------------------------------- reliability
@then("every duplicate that was sent is dropped")
def step_dups_dropped(context):
    m = context.world.metrics()
    assert m["counters"]["sim_duplicates"] > 100
    assert m["totals"]["duplicates"] == m["counters"]["sim_duplicates"]


@then("every event that was sent is accounted for")
def step_accounted(context):
    m = context.world.metrics()
    t = m["totals"]
    sent = context.memo.get("sent") or t["sent"]
    assert sent > 0 and sent == t["ok"] + t["failed"] + t["duplicates"], (sent, t)


@then("the archive holds each event once")
def step_archive_once(context):
    import pyarrow.compute as pc

    w = context.world
    t = w.processor.archive.dataset().to_table(columns=["vin", "seq"])
    keys = pc.binary_join_element_wise(t["vin"], pc.cast(t["seq"], "string"), ":")
    assert t.num_rows == w.metrics()["totals"]["ok"] == pc.count_distinct(keys).as_py()


@then("no event was counted twice")
def step_not_twice(context):
    import pyarrow.compute as pc

    w = context.world
    w.pump()
    t = w.processor.archive.dataset().to_table(columns=["vin", "seq"])
    keys = pc.binary_join_element_wise(t["vin"], pc.cast(t["seq"], "string"), ":")
    assert pc.count_distinct(keys).as_py() == t.num_rows


@then("the latest state of each vehicle is its newest event")
def step_newest(context):
    w = context.world
    checked = 0
    for i in range(0, len(w.vins), 11):
        st = w.processor.hot.get(w.vins[i])
        if st is not None:
            assert st["seq"] == int(w.fleet.seq[i]), (w.vins[i], st["seq"], int(w.fleet.seq[i]))
            checked += 1
    assert checked > 50


@when("the normaliser is killed after its first batch")
def step_kill(context):
    w = context.world
    w.normalizer.batch = 1500
    w.normalizer.step(0.0)
    w.normalizer._checkpoint()
    w.normalizer.publish()
    assert 0 < w.normalizer.processed < context.memo["sent"]
    w.normalizer = None


@when("a replacement normaliser starts")
def step_replace(context):
    from rosetta.pipeline.normalizer_worker import NormalizerWorker

    w = context.world
    w.normalizer = NormalizerWorker(broker=w.broker)
    w.pump()


@when("a vehicle brakes hard")
def step_brake(context):
    w = context.world
    w.run(ticks=2)
    i = int(np.flatnonzero((w.fleet.oem == OEM_INDEX["nordvik"]) & (w.fleet.speed > 30))[0])
    context.memo["vin"] = w.vins[i]
    w.clock += 1000
    real_step = w.fleet.step

    def step_with_event(dt, now_ms, start_boost=1.0):
        real_step(dt, now_ms, start_boost)
        w.fleet.evt[i] = EVT_CODE["HARSH_BRAKE"]

    w.fleet.step = step_with_event
    w.sim.tick(now_ms=w.clock, dt=1.0)
    w.fleet.step = real_step
    w.pump()


@then('a "{kind}" alert is raised for that vehicle')
def step_alert(context, kind):
    w = context.world
    recs = w.broker.consumer(T_ALERTS, "bdd", start="beginning").poll(10_000, 0.0)
    mine = [orjson.loads(r.value) for r in recs]
    mine = [a for a in mine if a["vin"] == context.memo["vin"] and a["kind"] == kind]
    assert len(mine) == 1, mine
    context.memo["alert"] = mine[0]
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(Alert).where(Alert.kind == kind)).scalar() >= 1


@then("it was raised less than {ms:d} milliseconds after the event arrived")
def step_alert_latency(context, ms):
    a = context.memo["alert"]
    assert 0 <= a["detected_ts"] - a["rx_ts"] < ms, a


# ------------------------------------------------------------------ guardrails
@when('the agent tries to "{action}" its own draft')
def step_agent_tries(context, action):
    try:
        with session_scope() as s:
            registry.transition(s, context.memo["oem"], context.memo["version"], action, actor=AGENT,
                                actor_kind="agent")
        context.memo["refused"] = None
    except registry.RegistryError as e:
        context.memo["refused"] = e.code
        context.memo["tried"] = action


@then('the registry refuses with "{code}"')
def step_refused(context, code):
    assert context.memo["refused"] == code


@then('the draft is still in state "{state}"')
def step_still(context, state):
    with session_scope() as s:
        assert registry.get_version(s, context.memo["oem"], context.memo["version"]).state == state


@then("the refusal is recorded in the audit log")
def step_refusal_audited(context):
    with session_scope() as s:
        rows = s.execute(select(AuditLog).where(AuditLog.outcome == "denied")).scalars().all()
        assert [(r.action, r.actor_kind) for r in rows] == [(f"mapping.{context.memo['tried']}", "agent")]


@then("each step of the agent run has an audit entry")
def step_steps_audited(context):
    with session_scope() as s:
        steps = s.execute(select(AgentStep).where(AgentStep.run_id == context.memo["run_id"])).scalars().all()
        rows = s.execute(select(AuditLog).where(AuditLog.resource == f"agent_run/{context.memo['run_id']}",
                                                AuditLog.action.like("agent.tool.%"))).scalars().all()
        assert len(steps) == len(rows) >= 5
        assert {r.actor_kind for r in rows} == {"agent"}


@then("the audit chain is intact")
def step_chain(context):
    with session_scope() as s:
        assert audit.verify_chain(s)["valid"]


@given('an engineer writes a mapping for "{oem}" that reads speed in the wrong unit')
def step_bad_mapping(context, oem):
    bad = {**SPEC_HELIX, "fields": {**SPEC_HELIX["fields"], "speed_kmh": {"path": "fahrt.v"}}}
    with session_scope() as s:
        mv = registry.create_version(s, oem, bad, source="human", actor=ENG)
        context.memo.update({"oem": oem, "version": mv.version})


@when("the mapping is tested against the golden set")
def step_test_mapping(context):
    with session_scope() as s:
        _, rep = registry.validate_version(s, context.memo["oem"], context.memo["version"], actor=ENG, label="helix")
        context.memo["pass_rate"] = rep.pass_rate


@then('it stays in state "{state}"')
def step_stays(context, state):
    assert context.memo["pass_rate"] < 0.99
    step_still(context, state)


@then("an engineer cannot approve it")
def step_cannot_approve(context):
    try:
        act(context, "approve")
        raise AssertionError("approval of an untested mapping was accepted")
    except registry.RegistryError as e:
        assert e.code == "conflict"


@when("someone edits an old audit entry in the database")
def step_tamper(context):
    with session_scope() as s:
        victim = s.execute(select(AuditLog.id).order_by(AuditLog.id).offset(3).limit(1)).scalar()
        s.execute(update(AuditLog).where(AuditLog.id == victim).values(outcome="ok", actor="nobody"))
        context.memo["victim"] = victim


@then("verifying the audit chain reports where it breaks")
def step_breaks(context):
    with session_scope() as s:
        res = audit.verify_chain(s)
    assert res["valid"] is False and res["broken_at_id"] == context.memo["victim"]


# --------------------------------------------------------------------- privacy
def client(context):
    if context.client is None:
        from fastapi.testclient import TestClient

        from rosetta.api.main import create_app

        context.world.metrics()
        context.client = TestClient(create_app())
        context.client.__enter__()
    return context.client


@given('I am signed in as "{email}"')
def step_signin(context, email):
    r = client(context).post("/api/v1/auth/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    context.headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    context.memo["tenant"] = r.json()["user"]["tenant_id"]


@when("I list vehicles")
def step_list(context):
    r = context.client.get("/api/v1/vehicles?limit=100", headers=context.headers)
    assert r.status_code == 200
    context.memo["vehicles"] = r.json()["items"]
    assert context.memo["vehicles"]


@then("every vehicle belongs to tenant {t:d}")
def step_tenant(context, t):
    assert {v["tenant_id"] for v in context.memo["vehicles"]} == {t}


@then('a vehicle of another tenant answers "{code:d}"')
def step_other(context, code):
    from rosetta.db.models import Fleet, Vehicle

    with session_scope() as s:
        vin = s.execute(select(Vehicle.vin).join(Fleet, Fleet.id == Vehicle.fleet_id)
                        .where(Fleet.tenant_id != context.memo["tenant"]).limit(1)).scalar()
    assert context.client.get(f"/api/v1/vehicles/{vin}", headers=context.headers).status_code == code


@then("every vehicle number shows only its last {n:d} characters")
def step_masked_vin(context, n):
    for v in context.memo["vehicles"]:
        assert len(v["vin"]) == 17 and v["vin"][: 17 - n] == "•" * (17 - n) and v["vin_ref"] is None


@then("every position is the centre of a geohash cell of {p:d} characters")
def step_masked_pos(context, p):
    from rosetta.algorithms import geohash

    live = [v["live"] for v in context.memo["vehicles"] if v.get("live")]
    assert live
    for pos in live:
        la, lo = geohash.decode(pos["geohash"])
        assert len(pos["geohash"]) == p and abs(la - pos["lat"]) < 1e-3 and abs(lo - pos["lon"]) < 1e-3


@given("a driver of my tenant")
def step_driver(context):
    r = context.client.get("/api/v1/drivers?limit=1", headers=context.headers).json()["items"][0]
    assert r["full_name"] and r["email"]
    context.memo["driver"] = r


@when("I request erasure of that driver")
def step_erase(context):
    r = context.client.post("/api/v1/compliance/erasure", json={"driver_id": context.memo["driver"]["id"]},
                            headers=context.headers)
    assert r.status_code == 201, r.text
    context.memo["erasure"] = r.json()


@then('the request is "{status}" and verified')
def step_erased(context, status):
    e = context.memo["erasure"]
    assert e["status"] == status and e["evidence"]["verified"] is True


@then("the driver has no name, e-mail, phone or licence on record")
def step_no_pii(context):
    with session_scope() as s:
        d = s.get(Driver, context.memo["driver"]["id"])
        assert (d.full_name, d.email, d.phone, d.license_hash) == (None, None, None, None) and d.erased_at


@then("no vehicle is linked to the driver any more")
def step_no_links(context):
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(VehicleDriverAssignment).where(
            VehicleDriverAssignment.driver_id == context.memo["driver"]["id"])).scalar() == 0


@then("the erasure itself is in the audit log")
def step_erasure_audited(context):
    with session_scope() as s:
        row = s.execute(select(AuditLog).where(AuditLog.action == "compliance.erasure")).scalar_one()
        assert row.resource == f"driver/{context.memo['driver']['id']}" and row.actor == "manager@northwind.example"
        assert context.memo["driver"]["full_name"] not in orjson.dumps(row.detail).decode()


@when("I open the audit log")
def step_open_audit(context):
    context.memo["code"] = context.client.get("/api/v1/audit", headers=context.headers).status_code


@then('I am refused with "{code:d}"')
def step_refused_code(context, code):
    assert context.memo["code"] == code
