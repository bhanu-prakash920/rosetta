"""The mapping agent: start a run, read its trace."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...db.models import AgentRun, AgentStep, MappingVersion, Oem
from ...ml import model as mlmodel
from ...services import agent_service, registry
from ..security import OPERATORS, STAFF, Principal, require
from ..state import read_db, write_db

router = APIRouter(prefix="/agent", tags=["agent"])


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    oem: str = Field(max_length=32, pattern=r"^[a-z0-9_]+$")
    engine: str = Field("auto", pattern="^(auto|workflow|model|anthropic|google)$")
    label: str | None = Field(None, max_length=64)


def _run(r: AgentRun, oem_key: str, s: Any, steps: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {"id": r.id, "oem": oem_key, "status": r.status, "engine": r.engine, "trigger": r.trigger,
                         "summary": r.summary, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                         "started_at": r.started_at.isoformat(),
                         "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                         "duration_ms": round((r.finished_at - r.started_at).total_seconds() * 1000)
                         if r.finished_at else None, "mapping": None}
    if r.mapping_version_id:
        mv = s.get(MappingVersion, r.mapping_version_id)
        if mv is not None:
            d["mapping"] = {"version": mv.version, "state": mv.state}
    if steps:
        d["steps"] = [{"seq": st.seq, "tool": st.tool, "ok": st.ok, "duration_ms": st.duration_ms,
                       "input": st.input, "output": st.output, "ts": st.ts.isoformat()}
                      for st in s.execute(select(AgentStep).where(AgentStep.run_id == r.id)
                                          .order_by(AgentStep.seq)).scalars()]
    return d


@router.post("/runs", status_code=201, summary="Ask the agent to work out a mapping for a source")
def start(body: RunBody, p: Principal = Depends(require(*OPERATORS))):
    with write_db() as s:
        try:
            run = agent_service.run_agent(s, body.oem, user_id=p.user_id, requested_by=p.email,
                                          engine=body.engine, label=body.label)
        except registry.RegistryError as e:
            raise HTTPException(404 if e.code == "not_found" else 409, str(e)) from None
        s.flush()
        return _run(run, body.oem, s, steps=True)


@router.get("/runs", summary="Agent runs, newest first")
def runs(oem: str | None = Query(None, max_length=32), _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        q = select(AgentRun, Oem.key).join(Oem, Oem.id == AgentRun.oem_id)
        if oem:
            q = q.where(Oem.key == oem)
        rows = s.execute(q.order_by(AgentRun.id.desc()).limit(50)).all()
        return {"items": [_run(r, k, s) for r, k in rows]}


@router.get("/runs/{run_id}", summary="One run with every tool call it made")
def run(run_id: int, _: Principal = Depends(require(*STAFF))):
    with read_db() as s:
        row = s.execute(select(AgentRun, Oem.key).join(Oem, Oem.id == AgentRun.oem_id)
                        .where(AgentRun.id == run_id)).first()
        if row is None:
            raise HTTPException(404, "no such run")
        return _run(row[0], row[1], s, steps=True)


@router.get("/model", summary="The field-mapping model and how it scored against the baseline")
def model(_: Principal = Depends(require(*STAFF))):
    import json
    from pathlib import Path

    rep = Path("docs/evidence/ml_field_mapper.json")
    try:
        m = mlmodel.load()
        meta = m.meta
    except Exception as e:
        return {"loaded": False, "error": type(e).__name__}
    return {"loaded": True, "meta": meta, "labels": len(m.classes),
            "engine": agent_service.choose_engine("auto"),
            "llm": agent_service.llm_model_name(),
            "evaluation": json.loads(rep.read_text()) if rep.exists() else None}
