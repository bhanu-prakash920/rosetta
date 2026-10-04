"""Right to erasure (GDPR article 17, India's DPDP Act section 12).

What is erased   the driver's name, e-mail, phone and licence hash. The row
                 itself stays, with those columns set to NULL and `erased_at`
                 set, so foreign keys keep working and the erasure is provable.
What is cut      the link between the driver and vehicles and trips. Telemetry
                 is keyed by vehicle, not by person. Once the assignment rows
                 are gone, no stored event can be tied to this person.
What is kept     the audit entries about the driver, which identify the person
                 only by an internal number. Law requires the record that an
                 erasure happened to outlive the data.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..db.models import Driver, ErasureRequest, Trip, VehicleDriverAssignment
from . import audit


class ErasureError(Exception):
    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(message)
        self.code = code


def erase_driver(s: Session, driver_id: int, *, actor: str, user_id: int | None,
                 tenant_id: int | None, ip: str = "") -> ErasureRequest:
    d = s.get(Driver, driver_id)
    if d is None or (tenant_id is not None and d.tenant_id != tenant_id):
        raise ErasureError("driver not found")
    if d.erased_at is not None:
        raise ErasureError("this driver was already erased", "conflict")
    req = ErasureRequest(tenant_id=d.tenant_id, subject_kind="driver", subject_id=d.id, status="running",
                         requested_by=user_id)
    s.add(req)
    s.flush()
    had = [k for k in ("full_name", "email", "phone", "license_hash") if getattr(d, k) is not None]
    n_assign = s.execute(select(func.count()).select_from(VehicleDriverAssignment)
                         .where(VehicleDriverAssignment.driver_id == d.id)).scalar() or 0
    n_trips = s.execute(select(func.count()).select_from(Trip).where(Trip.driver_id == d.id)).scalar() or 0
    s.execute(update(Trip).where(Trip.driver_id == d.id).values(driver_id=None))
    s.execute(delete(VehicleDriverAssignment).where(VehicleDriverAssignment.driver_id == d.id))
    now = datetime.now(UTC)
    d.full_name = d.email = d.phone = d.license_hash = None
    d.erased_at = now
    s.flush()

    # Verify instead of assuming: read back what is left.
    left = s.get(Driver, d.id)
    remaining = [k for k in ("full_name", "email", "phone", "license_hash") if getattr(left, k) is not None]
    links = (s.execute(select(func.count()).select_from(VehicleDriverAssignment)
                       .where(VehicleDriverAssignment.driver_id == d.id)).scalar() or 0) + \
            (s.execute(select(func.count()).select_from(Trip).where(Trip.driver_id == d.id)).scalar() or 0)
    ok = not remaining and links == 0
    req.status = "completed" if ok else "failed"
    req.completed_at = now
    req.evidence = {"fields_erased": had, "assignments_removed": int(n_assign), "trips_unlinked": int(n_trips),
                    "fields_remaining": remaining, "links_remaining": int(links), "verified": ok}
    audit.record(s, actor_kind="user", actor=actor, tenant_id=d.tenant_id, action="compliance.erasure",
                 resource=f"driver/{d.id}", outcome="ok" if ok else "error", ip=ip,
                 detail={"request": req.id, **req.evidence})
    return req


def request_view(r: ErasureRequest) -> dict[str, Any]:
    return {"id": r.id, "tenant_id": r.tenant_id, "subject": f"{r.subject_kind}/{r.subject_id}",
            "status": r.status, "legal_basis": r.legal_basis, "requested_at": r.requested_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None, "evidence": r.evidence}
