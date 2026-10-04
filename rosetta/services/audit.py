"""Tamper-evident audit trail.

Each entry stores hash = SHA-256(prev_hash + canonical JSON of the entry). To
change or delete an old entry an attacker would have to recompute every later
hash, and `verify_chain` detects any break. Every data access through the API
and every action of the AI agent is written here.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from typing import Any

import orjson
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db.models import AuditLog

GENESIS = "0" * 64
# Writers must not read the same chain head twice. Inside one process this lock
# serialises them; across processes PostgreSQL does it with an advisory lock.
CHAIN_LOCK = threading.RLock()
_PG_LOCK_KEY = 72_260_001


def _digest(prev: str, ts: datetime, actor_kind: str, actor: str, tenant_id: int | None, action: str,
            resource: str, outcome: str, detail: dict[str, Any]) -> str:
    body = orjson.dumps(
        [prev, ts.astimezone(UTC).isoformat(timespec="microseconds"), actor_kind, actor,
         tenant_id, action, resource, outcome, detail],
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(body).hexdigest()


def record(s: Session, *, actor_kind: str, actor: str, action: str, resource: str,
           tenant_id: int | None = None, outcome: str = "ok", ip: str = "",
           detail: dict[str, Any] | None = None) -> AuditLog:
    """Append one entry. Runs inside the caller's transaction, so the business
    change and its audit entry commit or roll back together."""
    detail = detail or {}
    if s.get_bind().dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _PG_LOCK_KEY})
    last = s.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)).scalar()
    prev = last or GENESIS
    ts = datetime.now(UTC)
    row = AuditLog(ts=ts, actor_kind=actor_kind, actor=actor, tenant_id=tenant_id, action=action,
                   resource=resource, outcome=outcome, ip=ip, detail=detail, prev_hash=prev,
                   hash=_digest(prev, ts, actor_kind, actor, tenant_id, action, resource, outcome, detail))
    s.add(row)
    s.flush()
    return row


def verify_chain(s: Session, limit: int = 100_000) -> dict[str, Any]:
    """Recompute the chain from the first entry. Returns where it breaks, if it does."""
    prev = GENESIS
    n = 0
    for row in s.execute(select(AuditLog).order_by(AuditLog.id).limit(limit)).scalars():
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC)
        expect = _digest(prev, ts, row.actor_kind, row.actor, row.tenant_id, row.action,
                         row.resource, row.outcome, row.detail or {})
        if row.prev_hash != prev or row.hash != expect:
            return {"valid": False, "checked": n, "broken_at_id": row.id}
        prev = row.hash
        n += 1
    return {"valid": True, "checked": n, "head": prev}
