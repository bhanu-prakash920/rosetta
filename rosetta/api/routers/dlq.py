"""Dead letters: grouped view, samples, replay, and shadow runs of a draft on real traffic."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...algorithms.unionfind import cluster_shapes
from ...db.models import DlqGroup, DlqSample, ReplayJob
from ...domain.errors import NormalizeError, SpecError
from ...engine.compiler import compile_spec
from ...pipeline.dlq import FORMAT_REASONS, queue_replay, shape_of
from ...ports.broker import T_DLQ
from ...services import audit, registry
from ..security import OPERATORS, STAFF, Principal, require
from ..state import read_db, state, write_db

router = APIRouter(tags=["dead letters"])


def _text(b: bytes | None, n: int = 1200) -> dict[str, Any]:
    if not b:
        return {"text": "", "binary": False, "bytes": 0}
    try:
        t = bytes(b).decode("utf-8")
        if t.isprintable() or "\n" in t or "\t" in t:
            return {"text": t[:n], "binary": False, "bytes": len(b)}
    except UnicodeDecodeError:
        pass
    return {"text": bytes(b)[:96].hex(" "), "binary": True, "bytes": len(b)}


@router.get("/dlq/groups", summary="Dead letters grouped by source, reason, field and payload shape")
def groups(oem: str | None = Query(None, max_length=32), open_only: bool = False,
           _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        q = select(DlqGroup).order_by(DlqGroup.last_seen.desc()).limit(500)
        if oem:
            q = q.where(DlqGroup.oem_key == oem)
        rows = s.execute(q).scalars().all()
        items = []
        sigs: dict[str, dict[frozenset, list[int]]] = {}
        for g in rows:
            open_ = g.count - g.replayed
            if open_only and open_ <= 0:
                continue
            if g.reason in FORMAT_REASONS:
                _shape, keys = shape_of(bytes(g.sample or b""), g.content_type)
                sigs.setdefault(g.oem_key, {}).setdefault(keys, []).append(g.id)
            items.append({"id": g.id, "oem": g.oem_key, "reason": g.reason, "field": g.field, "shape": g.shape,
                          "count": g.count, "replayed": g.replayed, "open": max(0, open_),
                          "first_seen": g.first_seen, "last_seen": g.last_seen, "detail": g.detail,
                          "sample": _text(g.sample, 600)})
        # Payloads that differ only by optional fields belong to one format family.
        family: dict[int, str] = {}
        for o, by_sig in sigs.items():
            for n, grp in enumerate(cluster_shapes(by_sig.keys(), threshold=0.6)):
                for sig in grp:
                    for gid in by_sig[sig]:
                        family[gid] = f"{o}-{chr(65 + n % 26)}"
        for it in items:
            # Damaged messages (truncated, bad checksum) have random shapes. They are one
            # family per source: no mapping will ever read them.
            it["family"] = family.get(it["id"]) or f"{it['oem']}-damaged"
            it["mappable"] = it["reason"] in FORMAT_REASONS
        fams: dict[str, dict[str, Any]] = {}
        for it in items:
            f = fams.setdefault(it["family"], {"family": it["family"], "oem": it["oem"], "groups": 0,
                                               "count": 0, "open": 0, "reasons": {},
                                               "mappable": it["mappable"]})
            f["groups"] += 1
            f["count"] += it["count"]
            f["open"] += it["open"]
            f["reasons"][it["reason"]] = f["reasons"].get(it["reason"], 0) + it["count"]
        return {"items": items,
                "families": sorted(fams.values(), key=lambda f: (not f["mappable"], -f["open"])),
                "totals": {"count": sum(i["count"] for i in items), "open": sum(i["open"] for i in items)}}


@router.get("/dlq/groups/{group_id}/samples", summary="Sample payloads of one group")
def samples(group_id: int, limit: int = Query(10, ge=1, le=50), _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        g = s.get(DlqGroup, group_id)
        if g is None:
            raise HTTPException(404, "no such group")
        rows = s.execute(select(DlqSample).where(DlqSample.group_id == group_id)
                         .order_by(DlqSample.id.desc()).limit(limit)).scalars().all()
        return {"group": {"id": g.id, "oem": g.oem_key, "reason": g.reason, "field": g.field, "detail": g.detail},
                "items": [{"device": r.device, "ts": r.ts, **_text(r.payload)} for r in rows]}


class ReplayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    oem: str = Field(max_length=32, pattern=r"^[a-z0-9_]+$")


@router.post("/dlq/replay", status_code=202, summary="Send parked messages of one source through the pipeline again")
def replay(body: ReplayBody, p: Principal = Depends(require(*OPERATORS))):
    with write_db() as s:
        try:
            registry.get_oem(s, body.oem)
        except registry.RegistryError as e:
            raise HTTPException(404, str(e)) from None
        job = queue_replay(s, body.oem, p.user_id)
        audit.record(s, actor_kind="user", actor=p.email, action="dlq.replay", resource=f"replay_job/{job.id}",
                     detail={"oem": body.oem})
        return {"job": job.id, "status": job.status}


@router.get("/dlq/replay", summary="Replay jobs, newest first")
def replay_jobs(_: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        rows = s.execute(select(ReplayJob).order_by(ReplayJob.id.desc()).limit(30)).scalars().all()
        return {"items": [{"id": j.id, "oem": j.oem_key, "status": j.status, "trigger": j.trigger,
                           "scanned": j.scanned, "republished": j.republished, "skipped": j.skipped,
                           "created_at": j.created_at.isoformat(),
                           "finished_at": j.finished_at.isoformat() if j.finished_at else None} for j in rows]}


class ShadowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_records: int = Field(20_000, ge=100, le=200_000)


@router.post("/mappings/{oem}/{version}/shadow",
             summary="Dry run: translate parked real traffic with this version, write nothing")
def shadow(oem: str, version: int, body: ShadowBody, p: Principal = Depends(require(*OPERATORS))):
    with read_db() as s:
        try:
            mv = registry.get_version(s, oem, version)
        except registry.RegistryError as e:
            raise HTTPException(404, str(e)) from None
        spec = mv.spec
    try:
        ad = compile_spec(oem, version, spec)
    except SpecError as e:
        raise HTTPException(422, f"spec does not compile: {e}") from None
    cons = state.broker.consumer(T_DLQ, f"shadow-{oem}-{version}-{time.time_ns()}", start="beginning")
    seen = ok = damaged = damaged_ok = 0
    reasons: dict[str, int] = {}
    fields: dict[str, int] = {}
    first_error = None
    try:
        while seen < body.max_records:
            recs = cons.poll(5000, 0.2)
            if not recs:
                break
            for r in recs:
                if r.headers.get("oem") != oem:
                    continue
                # Messages parked because the format was unknown are what a new mapping is for.
                # Messages damaged in transit (truncated, bad checksum) are reported apart:
                # no mapping can or should read them.
                is_format = r.headers.get("reason") in FORMAT_REASONS
                try:
                    ad.normalize(r.value)
                    good = True
                except NormalizeError as e:
                    good = False
                    if is_format:
                        reasons[e.reason] = reasons.get(e.reason, 0) + 1
                        if e.field:
                            fields[e.field] = fields.get(e.field, 0) + 1
                        first_error = first_error or {"reason": e.reason, "field": e.field, "detail": e.detail}
                if is_format:
                    seen += 1
                    ok += good
                else:
                    damaged += 1
                    damaged_ok += good
                if seen >= body.max_records:
                    break
    finally:
        cons.close()
    with write_db() as s:
        audit.record(s, actor_kind="user", actor=p.email, action="mapping.shadow",
                     resource=f"mapping/{oem}/v{version}", detail={"seen": seen, "ok": ok})
    return {"oem": oem, "version": version, "parked_messages_read": seen, "would_succeed": ok,
            "would_fail": seen - ok, "success_rate": round(ok / seen, 4) if seen else None,
            "failures_by_reason": reasons, "failures_by_field": fields, "first_error": first_error,
            "damaged_in_transit": damaged, "damaged_but_readable": damaged_ok,
            "note": "nothing was written: this only shows what a replay would do. Counts cover messages parked "
                    "because their format was not understood; damaged messages are counted apart"}
