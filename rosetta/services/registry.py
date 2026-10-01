"""Mapping registry: versions, their life cycle, and the hot-reload signal.

Life cycle of a mapping version:

    draft --validate--> validated --approve--> canary --promote--> active --retire--> retired
       \\                    \\                    \\
        +--reject--> rejected +--reject           +--rollback--> retired

Rules enforced here, not in the UI:
  * only a version that passed the golden set can be approved
  * an agent can create and validate, but never approve, promote or retire
  * every transition writes a MappingAction row and an audit entry, and bumps
    the registry epoch in the same transaction, which is what workers watch
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import orjson
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import GoldenCase, MappingAction, MappingField, MappingVersion, Oem, RegistryEpoch, ValidationRun
from ..domain.errors import SpecError
from ..engine.compiler import compile_spec
from ..engine.router import (
    LIVE_STATES,
    ST_ACTIVE,
    ST_CANARY,
    ST_DRAFT,
    ST_REJECTED,
    ST_RETIRED,
    ST_VALIDATED,
    MappingRow,
)
from . import audit
from .golden import GoldenReport, run_golden


class RegistryError(Exception):
    """A registry rule was violated. `code` maps to an HTTP status in the API."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message)
        self.code = code


TRANSITIONS = {
    "validate": ((ST_DRAFT, ST_VALIDATED), ST_VALIDATED),
    "approve": ((ST_VALIDATED,), ST_CANARY),
    "promote": ((ST_CANARY, ST_VALIDATED), ST_ACTIVE),
    "rollback": ((ST_CANARY, ST_ACTIVE), ST_RETIRED),
    "retire": ((ST_ACTIVE, ST_CANARY), ST_RETIRED),
    "reject": ((ST_DRAFT, ST_VALIDATED), ST_REJECTED),
}
# Who may perform each transition. The agent appears nowhere: it can create and
# validate drafts, nothing else. "system" may only roll back, which is the safety
# guard taking a misbehaving canary out of traffic.
ALLOWED_ACTORS = {"approve": ("user",), "promote": ("user",), "reject": ("user",), "retire": ("user",),
                  "rollback": ("user", "system")}


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(orjson.dumps(spec, option=orjson.OPT_SORT_KEYS)).hexdigest()


def get_oem(s: Session, key: str) -> Oem:
    oem = s.execute(select(Oem).where(Oem.key == key)).scalar_one_or_none()
    if oem is None:
        raise RegistryError(f"unknown OEM source {key!r}", "not_found")
    return oem


def bump_epoch(s: Session) -> int:
    row = s.get(RegistryEpoch, 1)
    if row is None:
        row = RegistryEpoch(id=1, epoch=0)
        s.add(row)
        s.flush()
    row.epoch = (row.epoch or 0) + 1
    row.updated_at = datetime.now(UTC)
    s.flush()
    return row.epoch


def current_epoch(s: Session) -> int:
    return s.execute(select(RegistryEpoch.epoch).where(RegistryEpoch.id == 1)).scalar() or 0


def live_rows(s: Session) -> list[MappingRow]:
    q = (select(Oem.key, MappingVersion.version, MappingVersion.state, MappingVersion.spec, MappingVersion.canary_pct)
         .join(Oem, Oem.id == MappingVersion.oem_id)
         .where(MappingVersion.state.in_(LIVE_STATES)))
    return [MappingRow(k, v, st, spec, pct) for k, v, st, spec, pct in s.execute(q)]


def create_version(s: Session, oem_key: str, spec: dict[str, Any], *, source: str, actor: str,
                   actor_kind: str = "user", user_id: int | None = None,
                   state: str = ST_DRAFT, comment: str = "") -> MappingVersion:
    """Store a new version as a draft. The spec is compiled first: a spec that does
    not compile is never written."""
    oem = get_oem(s, oem_key)
    spec = {**spec, "oem": oem_key}
    try:
        compile_spec(oem_key, 0, {k: v for k, v in spec.items() if k != "_meta"})
    except SpecError as e:
        raise RegistryError(f"spec rejected: {e}", "invalid") from None
    clean = {k: v for k, v in spec.items() if k != "_meta"}
    last = s.execute(select(func.max(MappingVersion.version)).where(MappingVersion.oem_id == oem.id)).scalar() or 0
    parent = s.execute(
        select(func.max(MappingVersion.version))
        .where(MappingVersion.oem_id == oem.id, MappingVersion.state == ST_ACTIVE)).scalar()
    mv = MappingVersion(oem_id=oem.id, version=last + 1, state=state, spec=clean, spec_hash=spec_hash(clean),
                        source=source, parent_version=parent, created_by=user_id)
    meta = spec.get("_meta", {}).get("fields", {})
    for name, fm in clean["fields"].items():
        m = meta.get(name, {})
        mv.fields.append(MappingField(canonical_field=name, source_path=fm["path"],
                                      transforms=fm.get("transforms") or [],
                                      confidence=m.get("confidence"), rationale=m.get("rationale")))
    s.add(mv)
    s.flush()
    s.add(MappingAction(mapping_version_id=mv.id, action="create", from_state="", to_state=state,
                        actor_user_id=user_id, actor_kind=actor_kind, comment=comment))
    audit.record(s, actor_kind=actor_kind, actor=actor, action="mapping.create",
                 resource=f"mapping/{oem_key}/v{mv.version}",
                 detail={"source": source, "spec_hash": mv.spec_hash, "fields": len(clean["fields"])})
    if state in LIVE_STATES:
        bump_epoch(s)
    return mv


def get_version(s: Session, oem_key: str, version: int) -> MappingVersion:
    oem = get_oem(s, oem_key)
    mv = s.execute(select(MappingVersion).where(MappingVersion.oem_id == oem.id,
                                                MappingVersion.version == version)).scalar_one_or_none()
    if mv is None:
        raise RegistryError(f"{oem_key} v{version} does not exist", "not_found")
    return mv


def golden_cases(s: Session, oem_key: str, label: str | None = None) -> list[tuple[bytes, dict[str, Any]]]:
    oem = get_oem(s, oem_key)
    q = select(GoldenCase.payload, GoldenCase.expected).where(GoldenCase.oem_id == oem.id)
    if label:
        q = q.where(GoldenCase.label == label)
    return [(bytes(p), e) for p, e in s.execute(q.order_by(GoldenCase.id))]


def golden_labels(s: Session, oem_key: str) -> list[str]:
    oem = get_oem(s, oem_key)
    return [r[0] for r in s.execute(select(GoldenCase.label).where(GoldenCase.oem_id == oem.id).distinct())]


def validate_version(s: Session, oem_key: str, version: int, *, actor: str, actor_kind: str = "user",
                     user_id: int | None = None, label: str | None = None) -> tuple[MappingVersion, GoldenReport]:
    """Run the golden set. Moves draft -> validated only when the pass rate is high enough."""
    mv = get_version(s, oem_key, version)
    if mv.state not in TRANSITIONS["validate"][0]:
        raise RegistryError(f"cannot validate a mapping in state {mv.state!r}")
    cases = golden_cases(s, oem_key, label)
    if not cases:
        raise RegistryError(f"no golden cases for {oem_key!r}" + (f" with label {label!r}" if label else ""), "invalid")
    rep = run_golden(oem_key, mv.spec, cases)
    s.add(ValidationRun(mapping_version_id=mv.id, total=rep.total, passed=rep.passed,
                        field_accuracy=rep.field_accuracy(), failures=rep.failures[:10]))
    need = get_settings().golden_pass_rate
    ok = rep.pass_rate >= need and not rep.error
    before = mv.state
    if ok:
        mv.state = ST_VALIDATED
    s.add(MappingAction(mapping_version_id=mv.id, action="validate", from_state=before, to_state=mv.state,
                        actor_user_id=user_id, actor_kind=actor_kind,
                        comment=f"golden {rep.passed}/{rep.total} (need {need:.0%})"))
    audit.record(s, actor_kind=actor_kind, actor=actor, action="mapping.validate",
                 resource=f"mapping/{oem_key}/v{version}", outcome="ok" if ok else "failed",
                 detail={"passed": rep.passed, "total": rep.total, "label": label or ""})
    s.flush()
    return mv, rep


def transition(s: Session, oem_key: str, version: int, action: str, *, actor: str, actor_kind: str = "user",
               user_id: int | None = None, comment: str = "", canary_pct: int | None = None) -> MappingVersion:
    """approve / promote / rollback / retire / reject. Human only."""
    if action not in TRANSITIONS or action == "validate":
        raise RegistryError(f"unknown action {action!r}", "invalid")
    if actor_kind not in ALLOWED_ACTORS[action]:
        audit.record(s, actor_kind=actor_kind, actor=actor, action=f"mapping.{action}",
                     resource=f"mapping/{oem_key}/v{version}", outcome="denied",
                     detail={"why": "only a human may change what runs in production"})
        s.commit()
        raise RegistryError(f"{actor_kind} actors may not {action} a mapping", "forbidden")
    mv = get_version(s, oem_key, version)
    allowed, target = TRANSITIONS[action]
    if mv.state not in allowed:
        raise RegistryError(f"cannot {action} a mapping in state {mv.state!r}")
    before = mv.state
    if action == "approve":
        pct = 100 if canary_pct is None else int(canary_pct)
        if not 1 <= pct <= 100:
            raise RegistryError("canary_pct must be between 1 and 100", "invalid")
        # one canary per OEM: an older canary is retired when a new one is approved
        s.execute(update(MappingVersion)
                  .where(MappingVersion.oem_id == mv.oem_id, MappingVersion.state == ST_CANARY,
                         MappingVersion.id != mv.id)
                  .values(state=ST_RETIRED))
        mv.canary_pct = pct
    if action == "promote":
        mv.canary_pct = 0
        mv.activated_at = datetime.now(UTC)
        oem = s.get(Oem, mv.oem_id)
        if oem is not None and oem.status != "live":
            oem.status = "live"
    if action in ("rollback", "retire"):
        mv.canary_pct = 0
    mv.state = target
    s.add(MappingAction(mapping_version_id=mv.id, action=action, from_state=before, to_state=target,
                        actor_user_id=user_id, actor_kind=actor_kind, comment=comment))
    audit.record(s, actor_kind=actor_kind, actor=actor, action=f"mapping.{action}",
                 resource=f"mapping/{oem_key}/v{version}",
                 detail={"from": before, "to": target, "canary_pct": mv.canary_pct, "comment": comment})
    epoch = bump_epoch(s)
    s.flush()
    mv.__dict__["_epoch"] = epoch
    return mv
