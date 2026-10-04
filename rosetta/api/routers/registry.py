"""Sources, the canonical schema and the mapping registry."""
from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from ...db.models import DlqGroup, MappingAction, MappingVersion, Oem, ValidationRun, Vehicle
from ...domain import canonical
from ...domain.errors import NormalizeError, SpecError
from ...domain.transforms import ALLOWED_OPS, UNITS
from ...engine.compiler import compile_spec
from ...engine.decoders import DECODER_TYPES, MAX_PAYLOAD_BYTES, flatten
from ...pipeline.dlq import queue_replay
from ...services import registry
from ..security import EVERYONE, OPERATORS, STAFF, Principal, require
from ..state import read_db, state, write_db

router = APIRouter(tags=["registry"])
CODE_STATUS = {"not_found": 404, "invalid": 422, "forbidden": 403, "conflict": 409}


def _raise(e: registry.RegistryError) -> None:
    raise HTTPException(CODE_STATUS.get(e.code, 409), str(e)) from None


def _version(mv: MappingVersion, oem_key: str, full: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {"oem": oem_key, "version": mv.version, "state": mv.state, "canary_pct": mv.canary_pct,
                         "source": mv.source, "parent_version": mv.parent_version, "spec_hash": mv.spec_hash[:12],
                         "created_at": mv.created_at.isoformat(),
                         "activated_at": mv.activated_at.isoformat() if mv.activated_at else None,
                         "fields": len(mv.fields)}
    if full:
        spec = dict(mv.spec)
        dec = dict(spec.get("decoder") or {})
        if "descriptor_b64" in dec:
            dec["descriptor_b64"] = f"<{len(dec['descriptor_b64'])} chars>"
        d["decoder"] = dec
        d["spec"] = {**spec, "decoder": dec}
        order = [f.name for f in canonical.CANONICAL_FIELDS]
        d["field_map"] = sorted(
            ({"canonical": f.canonical_field, "path": f.source_path, "transforms": f.transforms,
              "confidence": f.confidence, "rationale": f.rationale,
              "required": canonical.FIELD_BY_NAME[f.canonical_field].required,
              "unit": canonical.FIELD_BY_NAME[f.canonical_field].unit} for f in mv.fields),
            key=lambda x: order.index(x["canonical"]))
    return d


@router.get("/schema/canonical", summary="The canonical event: JSON Schema, field list, allowed transforms")
def schema(_: Principal = Depends(require(*EVERYONE))):
    return {"json_schema": canonical.json_schema(),
            "fields": [{"name": f.name, "kind": f.kind, "required": f.required, "unit": f.unit, "min": f.lo,
                        "max": f.hi, "description": f.description, "pii": f.pii}
                       for f in canonical.CANONICAL_FIELDS],
            "event_types": list(canonical.EVENT_TYPES), "transform_ops": list(ALLOWED_OPS),
            "units": {q: sorted(u) for q, u in UNITS.items()}, "decoders": list(DECODER_TYPES)}


@router.get("/oems", summary="Every telemetry source with its live mapping and health")
def oems(_: Principal = Depends(require(*EVERYONE))):
    with read_db() as s:
        rows = s.execute(select(Oem).order_by(Oem.id)).scalars().all()
        counts = dict(s.execute(select(Vehicle.oem_id, func.count()).group_by(Vehicle.oem_id)).all())
        vers = s.execute(select(MappingVersion).order_by(MappingVersion.version)).scalars().all()
        dead = dict(s.execute(select(DlqGroup.oem_key, func.sum(DlqGroup.count - DlqGroup.replayed))
                              .group_by(DlqGroup.oem_key)).all())
        live = {o["oem"]: o for o in state.agg.overview()["oems"]}
        out = []
        for o in rows:
            mine = [v for v in vers if v.oem_id == o.id]
            out.append({
                "key": o.key, "name": o.name, "wmi": o.wmi, "wire_format": o.wire_format, "status": o.status,
                "vehicles": counts.get(o.id, 0),
                "active_versions": [v.version for v in mine if v.state == "active"],
                "canary": next(({"version": v.version, "pct": v.canary_pct} for v in mine if v.state == "canary"), None),
                "pending_review": [v.version for v in mine if v.state == "validated"],
                "versions": len(mine), "open_dead_letters": int(dead.get(o.key) or 0),
                "live": live.get(o.key),
            })
        return {"items": out}


@router.get("/mappings", summary="Mapping versions, newest first")
def mappings(oem: str | None = Query(None, max_length=32), state_: str | None = Query(None, alias="state", max_length=16),
             _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        q = select(MappingVersion, Oem.key).join(Oem, Oem.id == MappingVersion.oem_id)
        if oem:
            q = q.where(Oem.key == oem)
        if state_:
            q = q.where(MappingVersion.state == state_)
        rows = s.execute(q.order_by(MappingVersion.created_at.desc(), MappingVersion.id.desc()).limit(200)).all()
        return {"items": [_version(mv, k) for mv, k in rows]}


@router.get("/mappings/{oem}/{version}", summary="One mapping version with its fields, history and validation runs")
def mapping(oem: str, version: int, _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        try:
            mv = registry.get_version(s, oem, version)
        except registry.RegistryError as e:
            _raise(e)
        d = _version(mv, oem, full=True)
        d["actions"] = [{"action": a.action, "from": a.from_state, "to": a.to_state, "actor_kind": a.actor_kind,
                         "comment": a.comment, "ts": a.ts.isoformat()}
                        for a in s.execute(select(MappingAction).where(MappingAction.mapping_version_id == mv.id)
                                           .order_by(MappingAction.id)).scalars()]
        d["validation_runs"] = [{"total": v.total, "passed": v.passed, "field_accuracy": v.field_accuracy,
                                 "failures": v.failures, "ts": v.ts.isoformat()}
                                for v in s.execute(select(ValidationRun).where(ValidationRun.mapping_version_id == mv.id)
                                                   .order_by(ValidationRun.id.desc()).limit(5)).scalars()]
        d["canary_health"] = state.agg.canary_health(oem, version) if mv.state == "canary" else None
        return d


@router.get("/mappings/{oem}/{version}/diff", summary="What changed against another version")
def diff(oem: str, version: int, against: int = Query(..., ge=1), _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        try:
            a, b = registry.get_version(s, oem, against), registry.get_version(s, oem, version)
        except registry.RegistryError as e:
            _raise(e)
        fa, fb = a.spec["fields"], b.spec["fields"]
        out = []
        for name in [f.name for f in canonical.CANONICAL_FIELDS]:
            x, y = fa.get(name), fb.get(name)
            if x == y:
                continue
            out.append({"canonical": name, "change": "added" if x is None else "removed" if y is None else "changed",
                        "before": x, "after": y})
        dec_changed = {k: v for k, v in a.spec["decoder"].items() if k != "descriptor_b64"} != \
                      {k: v for k, v in b.spec["decoder"].items() if k != "descriptor_b64"}
        return {"oem": oem, "from": against, "to": version, "fields": out, "decoder_changed": dec_changed}


class SpecBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decoder: dict[str, Any]
    fields: dict[str, dict[str, Any]] = Field(max_length=32)
    notes: str | None = Field(None, max_length=2000)
    comment: str = Field("", max_length=500)


@router.post("/mappings/{oem}", status_code=201, summary="Create a draft mapping by hand")
def create(oem: str, body: SpecBody, p: Principal = Depends(require(*OPERATORS))):
    spec = {"decoder": body.decoder, "fields": body.fields}
    if body.notes:
        spec["notes"] = body.notes
    with write_db() as s:
        try:
            mv = registry.create_version(s, oem, spec, source="human", actor=p.email, user_id=p.user_id,
                                         comment=body.comment)
        except registry.RegistryError as e:
            _raise(e)
        return _version(mv, oem)


class ValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(None, max_length=64)


@router.post("/mappings/{oem}/{version}/validate", summary="Run the golden set against a draft")
def validate(oem: str, version: int, body: ValidateBody, p: Principal = Depends(require(*OPERATORS))):
    with write_db() as s:
        try:
            mv, rep = registry.validate_version(s, oem, version, actor=p.email, user_id=p.user_id, label=body.label)
        except registry.RegistryError as e:
            _raise(e)
        return {"state": mv.state, **rep.to_dict()}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field("", max_length=500)
    canary_pct: int | None = Field(None, ge=1, le=100)
    replay: bool = True


@router.post("/mappings/{oem}/{version}/actions/{action}",
             summary="approve, promote, rollback, retire or reject. Humans only")
def act(oem: str, version: int, action: str, body: ActionBody, p: Principal = Depends(require(*OPERATORS))):
    if action not in ("approve", "promote", "rollback", "retire", "reject"):
        raise HTTPException(404, "unknown action")
    with write_db() as s:
        try:
            mv = registry.transition(s, oem, version, action, actor=p.email, user_id=p.user_id,
                                     comment=body.comment, canary_pct=body.canary_pct)
        except registry.RegistryError as e:
            _raise(e)
        job = None
        if action in ("approve", "promote") and body.replay:
            # Going live is what makes parked messages readable: replay them now.
            job = queue_replay(s, oem, p.user_id, trigger=action)
        return {**_version(mv, oem), "registry_epoch": registry.current_epoch(s),
                "replay_job": job.id if job else None}


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: str | None = Field(None, max_length=MAX_PAYLOAD_BYTES * 2)
    payload_b64: str | None = Field(None, max_length=MAX_PAYLOAD_BYTES * 2)


@router.post("/mappings/{oem}/{version}/preview",
             summary="Translate one payload with this version and show each field's source")
def preview(oem: str, version: int, body: PreviewBody, _: Principal = Depends(require(*STAFF))):
    if (body.payload is None) == (body.payload_b64 is None):
        raise HTTPException(422, "send exactly one of payload and payload_b64")
    try:
        raw = body.payload.encode() if body.payload is not None else base64.b64decode(body.payload_b64 or "", validate=True)
    except Exception:
        raise HTTPException(422, "payload_b64 is not valid base64") from None
    with read_db() as s:
        try:
            mv = registry.get_version(s, oem, version)
        except registry.RegistryError as e:
            _raise(e)
        spec = mv.spec
    try:
        ad = compile_spec(oem, version, spec)
    except SpecError as e:
        raise HTTPException(422, f"spec does not compile: {e}") from None
    out: dict[str, Any] = {"oem": oem, "version": version}
    try:
        decoded = ad.decode(raw)
        out["decoded"] = {k: (v if not isinstance(v, (bytes, bytearray)) else "<bytes>")
                          for k, v in list(flatten(decoded).items())[:80]}
    except NormalizeError as e:
        return {**out, "ok": False, "error": {"reason": e.reason, "field": e.field, "detail": e.detail}}
    try:
        ev = ad.normalize(raw)
        out.update({"ok": True, "event": ev})
    except NormalizeError as e:
        out.update({"ok": False, "error": {"reason": e.reason, "field": e.field, "detail": e.detail}})
    out["lineage"] = [{"canonical": n, "path": f["path"], "transforms": f.get("transforms") or [],
                       "source_value": out["decoded"].get(f["path"])} for n, f in spec["fields"].items()]
    return out
