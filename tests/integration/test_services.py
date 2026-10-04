"""Registry rules, the audit chain and erasure, against a real database."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text, update

from rosetta.db.models import AuditLog, Driver, MappingAction, MappingVersion, VehicleDriverAssignment
from rosetta.db.session import session_scope
from rosetta.services import audit, erasure, registry
from rosetta.simulator.dialects import SPEC_HELIX, SPEC_NORDVIK
from tests.support import World

ENG = "engineer@rosetta.example"


def draft(s, spec=SPEC_HELIX, oem="helix", **kw):
    return registry.create_version(s, oem, spec, source="human", actor=ENG, **kw)


# ------------------------------------------------------------------- registry
def test_life_cycle_happy_path(fresh_world: World):
    with session_scope() as s:
        e0 = registry.current_epoch(s)
        mv = draft(s)
        assert mv.state == "draft" and mv.version == 1
        assert registry.current_epoch(s) == e0            # a draft changes nothing that runs
        mv, rep = registry.validate_version(s, "helix", 1, actor=ENG, label="helix")
        assert mv.state == "validated" and rep.passed == rep.total >= 150
        assert registry.transition(s, "helix", 1, "approve", actor=ENG, canary_pct=30).state == "canary"
        assert registry.current_epoch(s) == e0 + 1
        assert registry.transition(s, "helix", 1, "promote", actor=ENG).state == "active"
        assert registry.transition(s, "helix", 1, "retire", actor=ENG).state == "retired"
        acts = [a.action for a in s.execute(select(MappingAction).order_by(MappingAction.id)).scalars()
                if a.mapping_version_id == mv.id]
        assert acts == ["create", "validate", "approve", "promote", "retire"]
        assert registry.get_oem(s, "helix").status == "live"


@pytest.mark.parametrize("state,action", [
    ("draft", "approve"), ("draft", "promote"), ("draft", "rollback"),
    ("validated", "rollback"), ("validated", "retire"),
    ("active", "approve"), ("active", "promote"), ("active", "reject"),
    ("retired", "approve"), ("retired", "promote"), ("rejected", "approve"),
])
def test_illegal_transitions_are_refused(fresh_world: World, state, action):
    with session_scope() as s:
        mv = draft(s)
        s.execute(update(MappingVersion).where(MappingVersion.id == mv.id).values(state=state))
        s.flush()
        s.refresh(mv)
        with pytest.raises(registry.RegistryError) as e:
            registry.transition(s, "helix", mv.version, action, actor=ENG)
        assert e.value.code == "conflict"
        assert mv.state == state


@pytest.mark.parametrize("action", ["approve", "promote", "retire", "reject", "rollback"])
def test_agent_can_never_change_what_runs(fresh_world: World, action):
    with session_scope() as s:
        mv = draft(s)
        registry.validate_version(s, "helix", mv.version, actor="agent", actor_kind="agent", label="helix")
        with pytest.raises(registry.RegistryError) as e:
            registry.transition(s, "helix", mv.version, action, actor="agent@rosetta.example", actor_kind="agent")
        assert e.value.code == "forbidden"
    with session_scope() as s:
        assert registry.get_version(s, "helix", 1).state == "validated"
        denied = s.execute(select(AuditLog).where(AuditLog.outcome == "denied")).scalars().all()
        assert [d.action for d in denied] == [f"mapping.{action}"]      # the attempt itself is on record


def test_system_may_roll_back_but_not_promote(fresh_world: World):
    with session_scope() as s:
        draft(s)
        registry.validate_version(s, "helix", 1, actor=ENG, label="helix")
        registry.transition(s, "helix", 1, "approve", actor=ENG)
        with pytest.raises(registry.RegistryError):
            registry.transition(s, "helix", 1, "promote", actor="canary-guard", actor_kind="system")
    with session_scope() as s:
        assert registry.transition(s, "helix", 1, "rollback", actor="canary-guard",
                                   actor_kind="system").state == "retired"


@pytest.mark.parametrize("bad", [
    {"decoder": {"type": "json"}, "fields": {}},
    {"decoder": {"type": "json"}, "fields": {"vin": {"path": "a"}}},                       # required missing
    {"decoder": {"type": "exec"}, "fields": SPEC_NORDVIK["fields"]},                       # unknown decoder
    {"decoder": {"type": "json"}, "fields": {**SPEC_NORDVIK["fields"], "password": {"path": "x"}}},
    {"decoder": {"type": "json"}, "fields": {**SPEC_NORDVIK["fields"],
                                             "speed_kmh": {"path": "v", "transforms": [{"op": "eval", "code": "1"}]}}},
    {"decoder": {"type": "json"}, "fields": {**SPEC_NORDVIK["fields"],
                                             "speed_kmh": {"path": "v", "transforms": [{"op": "scale", "factor": "__import__"}]}}},
    {"decoder": {"type": "json"}, "fields": {**SPEC_NORDVIK["fields"], "speed_kmh": {"path": "a..b"}}},
    {"decoder": {"type": "json"}, "fields": SPEC_NORDVIK["fields"], "on_load": "rm -rf /"},
])
def test_a_spec_that_does_not_compile_is_never_stored(fresh_world: World, bad):
    with session_scope() as s:
        with pytest.raises(registry.RegistryError) as e:
            draft(s, spec=bad)
        assert e.value.code == "invalid"
        assert s.execute(select(MappingVersion).where(MappingVersion.source == "human")).first() is None


def test_only_one_canary_per_source(fresh_world: World):
    with session_scope() as s:
        for _ in range(2):
            mv = draft(s)
            registry.validate_version(s, "helix", mv.version, actor=ENG, label="helix")
            registry.transition(s, "helix", mv.version, "approve", actor=ENG, canary_pct=10)
        assert registry.get_version(s, "helix", 1).state == "retired"
        assert registry.get_version(s, "helix", 2).state == "canary"
        assert [(r.oem, r.version, r.state) for r in registry.live_rows(s) if r.oem == "helix"] == [("helix", 2, "canary")]


def test_unknown_source_and_version(fresh_world: World):
    with session_scope() as s:
        with pytest.raises(registry.RegistryError) as e:
            registry.get_oem(s, "tesla")
        assert e.value.code == "not_found"
        with pytest.raises(registry.RegistryError):
            registry.get_version(s, "helix", 99)


# ---------------------------------------------------------------------- audit
def test_chain_verifies_and_detects_edits(fresh_world: World):
    with session_scope() as s:
        for i in range(20):
            audit.record(s, actor_kind="user", actor=ENG, action="test.read", resource=f"thing/{i}", detail={"i": i})
    with session_scope() as s:
        ok = audit.verify_chain(s)
        assert ok["valid"] and ok["checked"] >= 20
        victim = s.execute(select(AuditLog.id).order_by(AuditLog.id).offset(7).limit(1)).scalar()
        # someone with database access rewrites history
        s.execute(update(AuditLog).where(AuditLog.id == victim).values(actor="someone.else@rosetta.example"))
    with session_scope() as s:
        res = audit.verify_chain(s)
        assert res == {"valid": False, "checked": 7, "broken_at_id": victim}


def test_chain_detects_deleted_row(fresh_world: World):
    with session_scope() as s:
        for i in range(10):
            audit.record(s, actor_kind="user", actor=ENG, action="test.read", resource=f"thing/{i}")
    with session_scope() as s:
        victim = s.execute(select(AuditLog.id).order_by(AuditLog.id).offset(4).limit(1)).scalar()
        s.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": victim})
    with session_scope() as s:
        res = audit.verify_chain(s)
        assert not res["valid"] and res["broken_at_id"] == victim + 1


def test_audit_rolls_back_with_the_business_change(fresh_world: World):
    with session_scope() as s:
        before = len(s.execute(select(AuditLog.id)).all())
    with pytest.raises(RuntimeError):
        with session_scope() as s:
            draft(s)
            raise RuntimeError("the transaction fails after the audit entry was written")
    with session_scope() as s:
        assert len(s.execute(select(AuditLog.id)).all()) == before
        assert s.execute(select(MappingVersion).where(MappingVersion.source == "human")).first() is None
        assert audit.verify_chain(s)["valid"]


# -------------------------------------------------------------------- erasure
def test_erasure_removes_identity_and_links(fresh_world: World):
    with session_scope() as s:
        d = s.execute(select(Driver).limit(1)).scalar_one()
        did, tid, name = d.id, d.tenant_id, d.full_name
        assert name and s.execute(select(VehicleDriverAssignment).where(
            VehicleDriverAssignment.driver_id == did)).first() is not None
        req = erasure.erase_driver(s, did, actor="admin@rosetta.example", user_id=None, tenant_id=None)
        assert req.status == "completed" and req.evidence["verified"] is True
        assert set(req.evidence["fields_erased"]) == {"full_name", "email", "phone", "license_hash"}
    with session_scope() as s:
        d = s.get(Driver, did)
        assert (d.full_name, d.email, d.phone, d.license_hash) == (None, None, None, None)
        assert d.erased_at is not None and d.tenant_id == tid
        assert s.execute(select(VehicleDriverAssignment).where(
            VehicleDriverAssignment.driver_id == did)).first() is None
        # the personal data is nowhere in the audit trail either
        for row in s.execute(select(AuditLog)).scalars():
            assert name not in str(row.detail) and name not in row.resource
        with pytest.raises(erasure.ErasureError) as e:
            erasure.erase_driver(s, did, actor="a", user_id=None, tenant_id=None)
        assert e.value.code == "conflict"


def test_erasure_respects_tenancy(fresh_world: World):
    with session_scope() as s:
        d = s.execute(select(Driver).limit(1)).scalar_one()
        with pytest.raises(erasure.ErasureError) as e:
            erasure.erase_driver(s, d.id, actor="x", user_id=None, tenant_id=d.tenant_id + 1)
        assert e.value.code == "not_found"
        assert s.get(Driver, d.id).full_name is not None
