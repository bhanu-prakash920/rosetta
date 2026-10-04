"""Audit trail, personal data, and the right to erasure."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...db.models import AuditLog, Driver, ErasureRequest
from ...services import audit, erasure
from ..security import ADMIN, MANAGER, STAFF, Principal, require
from ..state import read_db, write_db
from ..support import AUDIT, decode_cursor, encode_cursor, page

router = APIRouter(tags=["compliance"])


@router.get("/audit", summary="The audit trail, newest first")
def audit_log(cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
              actor_kind: str | None = Query(None, pattern="^(user|agent|service|system|anonymous)$"),
              action: str | None = Query(None, max_length=64, pattern=r"^[a-z_.]+$"),
              p: Principal = Depends(require(*STAFF))):
    after = decode_cursor(cursor)
    AUDIT.flush()                  # queued read-audit entries first, so the log is complete
    with read_db() as s:
        stmt = select(AuditLog)
        if actor_kind:
            stmt = stmt.where(AuditLog.actor_kind == actor_kind)
        if action:
            stmt = stmt.where(AuditLog.action.like(action + "%"))
        if after:
            stmt = stmt.where(AuditLog.id < int(after[0]))
        rows = s.execute(stmt.order_by(AuditLog.id.desc()).limit(limit + 1)).scalars().all()
        items = [{"id": r.id, "ts": r.ts.isoformat(), "actor_kind": r.actor_kind,
                  "actor": r.actor if p.has(ADMIN) or r.actor_kind != "user" else _initials(r.actor),
                  "action": r.action, "resource": r.resource, "outcome": r.outcome, "tenant_id": r.tenant_id,
                  "detail": r.detail, "hash": r.hash[:16], "prev_hash": r.prev_hash[:16]} for r in rows]
    return page(items, limit, lambda it: encode_cursor(it["id"]))


def _initials(email: str) -> str:
    name, _, dom = email.partition("@")
    return f"{name[:2]}***@{dom}" if dom else f"{name[:2]}***"


@router.get("/audit/verify", summary="Recompute the hash chain and report whether it is intact")
def audit_verify(_: Principal = Depends(require(*STAFF))):
    AUDIT.flush()
    with read_db() as s:
        return audit.verify_chain(s)


@router.get("/drivers", summary="Drivers of the caller's tenant (personal data)")
def drivers(cursor: str | None = None, limit: int = Query(25, ge=1, le=100),
            p: Principal = Depends(require(ADMIN, MANAGER))):
    after = decode_cursor(cursor)
    with read_db() as s:
        stmt = select(Driver)
        if p.tenant_id is not None:
            stmt = stmt.where(Driver.tenant_id == p.tenant_id)
        if after:
            stmt = stmt.where(Driver.id > int(after[0]))
        rows = s.execute(stmt.order_by(Driver.id).limit(limit + 1)).scalars().all()
        items = [{"id": d.id, "tenant_id": d.tenant_id, "full_name": d.full_name, "email": d.email,
                  "phone": d.phone, "erased": d.erased_at is not None,
                  "erased_at": d.erased_at.isoformat() if d.erased_at else None} for d in rows]
    return page(items, limit, lambda it: encode_cursor(it["id"]))


class ErasureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver_id: int = Field(ge=1)


@router.post("/compliance/erasure", status_code=201, summary="Erase a driver's personal data and prove it")
def erase(body: ErasureBody, request: Request, p: Principal = Depends(require(ADMIN, MANAGER))):
    with write_db() as s:
        try:
            req = erasure.erase_driver(s, body.driver_id, actor=p.email, user_id=p.user_id,
                                       tenant_id=p.tenant_id, ip=request.client.host if request.client else "")
        except erasure.ErasureError as e:
            raise HTTPException(404 if e.code == "not_found" else 409, str(e)) from None
        return erasure.request_view(req)


@router.get("/compliance/erasure", summary="Erasure requests and their evidence")
def erasures(p: Principal = Depends(require(ADMIN, MANAGER))) -> dict[str, Any]:
    with read_db() as s:
        stmt = select(ErasureRequest)
        if p.tenant_id is not None:
            stmt = stmt.where(ErasureRequest.tenant_id == p.tenant_id)
        rows = s.execute(stmt.order_by(ErasureRequest.id.desc()).limit(50)).scalars().all()
        return {"items": [erasure.request_view(r) for r in rows]}


@router.get("/compliance/policy", summary="What is stored, where, for how long, and who may see it")
def policy(_: Principal = Depends(require(*STAFF, MANAGER))):
    return {
        "personal_data": [
            {"data": "driver name, e-mail, phone, licence hash", "store": "relational (driver table)",
             "retention": "until erasure or contract end", "visible_to": ["admin", "fleet_manager (own tenant)"]},
            {"data": "precise vehicle location", "store": "hot state, time-series store, Parquet archive",
             "retention": "hot 48 h, warm 90 days, cold 13 months",
             "visible_to": ["admin", "platform_engineer", "fleet_manager (own tenant)"]},
            {"data": "VIN", "store": "every telemetry store", "retention": "as location",
             "visible_to": ["admin", "platform_engineer", "fleet_manager (own tenant)"]},
        ],
        "masking": {"analyst": "location snapped to a geohash cell of about 5 km, VIN shortened to 6 characters"},
        "erasure": "driver identity is nulled and every link from telemetry to the person is cut",
        "audit": "every read of fleet data and every change is recorded in a hash-chained log",
        "legal_basis": ["GDPR art. 5, 17, 25, 30, 32", "India DPDP Act 2023 s. 8, 12", "UNECE R155 (logging)"],
    }
