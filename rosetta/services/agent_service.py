"""Run the mapping agent and keep the record of what it did."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..agent import memory, workflow
from ..agent.toolbox import Toolbox, label_for
from ..config import get_settings
from ..db.models import AgentRun
from ..ml.profile import build_profiles, make_samples
from . import audit, registry


def choose_engine(requested: str = "auto") -> str:
    """'claude' when credentials are configured, otherwise the deterministic workflow."""
    st = get_settings()
    want = requested if requested != "auto" else st.llm_provider
    if want in ("none", "workflow"):
        return "workflow"
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if want in ("anthropic", "claude"):
        return "claude"
    return "claude" if has_key else "workflow"


def run_agent(s: Session, oem_key: str, *, user_id: int | None = None, requested_by: str = "system",
              engine: str = "auto", label: str | None = None, trigger: str = "manual",
              client: Any = None) -> AgentRun:
    oem = registry.get_oem(s, oem_key)
    eng = choose_engine(engine)
    run = AgentRun(oem_id=oem.id, status="running", trigger=trigger, requested_by=user_id,
                   engine="workflow (deterministic)" if eng == "workflow" else f"claude ({get_settings().llm_model})")
    s.add(run)
    s.flush()
    audit.record(s, actor_kind="user" if user_id else "service", actor=requested_by, action="agent.run.start",
                 resource=f"agent_run/{run.id}", detail={"oem": oem_key, "engine": run.engine, "trigger": trigger})
    tb = Toolbox(s, oem_key, run, label=label)
    try:
        if eng == "claude":
            from ..agent.claude_agent import run as run_claude

            result = run_claude(tb, client=client)
            if result.get("status") == "unavailable":
                # The model could not be reached. Fall back, and say so in the record.
                run.engine = f"workflow (deterministic, after {result.get('reason', 'model unavailable')})"
                tb.steps = 0 if not tb.steps else tb.steps
                result = workflow.run(tb)
        else:
            result = workflow.run(tb)
    except Exception as e:  # the record of a failed run matters as much as a good one
        result = {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
    run.status = {"submitted": "awaiting_approval"}.get(result.get("status", ""), "failed")
    if tb.submitted is not None and tb.submitted.state != "validated":
        run.status = "needs_review"
    run.summary = (result.get("summary") or result.get("reason") or "")[:2000]
    run.tokens_in = int(result.get("tokens_in", 0))
    run.tokens_out = int(result.get("tokens_out", 0))
    run.finished_at = datetime.now(UTC)
    audit.record(s, actor_kind="agent", actor=tb.actor, action="agent.run.finish",
                 resource=f"agent_run/{run.id}", outcome="ok" if run.status != "failed" else "failed",
                 detail={"oem": oem_key, "status": run.status, "steps": tb.steps,
                         "version": tb.submitted.version if tb.submitted else None})
    s.flush()
    run.__dict__["_result"] = result
    return run


def seed_memory(s: Session, vehicles: int = 1500, ticks: int = 30, log=print) -> int:
    """Teach the memory the dialects the platform already knows, from simulated samples."""
    import numpy as np

    from ..engine.decoders import build_decoder
    from ..simulator.dialects import DIALECTS
    from ..simulator.fleet import DRIVING, OEM_INDEX, Fleet

    mem = memory.make_memory(s)
    if mem.count():
        return 0
    f = Fleet(vehicles, seed=99, fleets=4)
    for k in range(10):
        f.step(1.0, 1_785_000_000_000 + k * 1000)
    n = 0
    picks = {}
    for key, d in DIALECTS.items():
        if not d.seeded:
            continue
        idx = np.flatnonzero(f.oem == OEM_INDEX[d.oem])
        mv = idx[f.state[idx] == DRIVING][:20]
        picks[key] = np.concatenate([mv, idx[f.state[idx] != DRIVING][:6]])
    msgs: dict[str, tuple[list, list]] = {k: ([], []) for k in picks}
    for k in range(ticks):
        f.step(1.0, 1_785_000_010_000 + k * 1000)
        for key, sel in picks.items():
            f.seq[sel] += 1
            t = f.truth(sel)
            dec = build_decoder(DIALECTS[key].spec["decoder"])
            for dev, p in zip(t["device_id"], DIALECTS[key].encode(t)):
                o = dec(p)
                piv = DIALECTS[key].spec["decoder"].get("pivot")
                if piv:
                    o = {a: b for a, b in o.items() if a != piv["path"]}
                msgs[key][0].append(o)
                msgs[key][1].append(dev)
    for key, (objs, devs) in msgs.items():
        d = DIALECTS[key]
        prof = build_profiles(make_samples(objs, devs))
        for canon, fm in d.spec["fields"].items():
            p = prof.get(fm["path"])
            if p is None:
                continue
            mem.add(d.oem, 1, fm["path"], label_for(canon, fm.get("transforms") or []), canon, memory.embed(p))
            n += 1
    s.flush()
    log(f"  mapping memory: {n} fields from {len(msgs)} known dialects")
    return n
