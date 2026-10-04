"""Operations: live metrics, workers, simulator scenarios, chaos."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from ...db.models import MetricMinute
from ...services import audit
from ...simulator.fleet import OEM_KEYS
from ...simulator.run import send_control
from ..security import EVERYONE, OPERATORS, Principal, require
from ..state import read_db, state, write_db

router = APIRouter(tags=["operations"])


@router.get("/overview", summary="Live pipeline numbers: throughput, latency, lag, per-OEM health")
def overview(window: int = Query(10, ge=1, le=120), _: Principal = Depends(require(*EVERYONE))):
    def build():
        ov = state.agg.overview(window)
        ov["processes"] = state.supervisor.status() if state.supervisor else []
        return ov

    return state.rendered(f"overview:{window}", 0.5, build)


@router.get("/series", summary="Per-second counts for charts")
def series(oem: str = Query("_all", max_length=32), seconds: int = Query(120, ge=10, le=300),
           _: Principal = Depends(require(*EVERYONE))):
    return state.agg.series(oem, seconds)


@router.get("/series/all", summary="Per-second counts for every source at once")
def series_all(seconds: int = Query(120, ge=10, le=300), _: Principal = Depends(require(*EVERYONE))):
    return state.rendered(f"series_all:{seconds}", 0.5, lambda: state.agg.series_all(seconds))


@router.get("/history", summary="Per-minute rollup from the metric_minute table")
def history(oem: str | None = Query(None, max_length=32), minutes: int = Query(60, ge=1, le=1440),
            _: Principal = Depends(require(*EVERYONE))):
    since = int(time.time()) // 60 - minutes
    with read_db() as s:
        q = select(MetricMinute).where(MetricMinute.minute >= since).order_by(MetricMinute.minute)
        if oem:
            q = q.where(MetricMinute.oem_key == oem)
        rows = s.execute(q).scalars().all()
        return {"items": [{"oem": r.oem_key, "minute": r.minute, "ok": r.ok, "failed": r.failed,
                           "duplicates": r.duplicates} for r in rows]}


@router.get("/stream", summary="Server-sent events: the overview, once per second")
async def stream(_: Principal = Depends(require(*EVERYONE))):
    async def gen():
        yield b"retry: 2000\n\n"
        while not state.stop.is_set():
            ov = state.agg.overview(5)
            ov["processes"] = state.supervisor.status() if state.supervisor else []
            yield b"event: overview\ndata: " + orjson.dumps(ov) + b"\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/system/health", summary="Readiness: 200 when the database and broker answer, else 503")
def health(response: Response):
    checks: dict[str, Any] = {}
    try:
        with read_db() as s:
            s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {type(e).__name__}"
    try:
        state.broker.partitions("telemetry.raw")
        checks["broker"] = "ok"
    except Exception as e:
        checks["broker"] = f"error: {type(e).__name__}"
    now = time.time() * 1000
    alive = [w for w in state.agg.workers.values() if now - w["ts"] < 15_000]
    checks["workers_reporting"] = len(alive)
    ok = checks["database"] == "ok" and checks["broker"] == "ok"
    if not ok:
        response.status_code = 503          # take this replica out of the load balancer
    return {"status": "ok" if ok else "degraded", "checks": checks, "uptime_s": int(time.time() - state.started),
            "vehicles": len(state.vins)}


@router.get("/system/live", summary="Liveness: 200 while the process can answer at all")
def live():
    return {"status": "alive", "uptime_s": int(time.time() - state.started)}


@router.get("/system/workers", summary="Supervised processes and their restart counts")
def workers(_: Principal = Depends(require(*OPERATORS))):
    return {"processes": state.supervisor.status() if state.supervisor else [],
            "reporting": sorted(state.agg.workers.values(), key=lambda w: (w["svc"], str(w["id"])))}


class KillBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=40, pattern=r"^[a-z]+-\d+$")


@router.post("/system/chaos/kill", summary="Chaos test: kill one pipeline process with SIGKILL")
def chaos_kill(body: KillBody, p: Principal = Depends(require(*OPERATORS))):
    if state.supervisor is None:
        raise HTTPException(409, "the pipeline is not supervised by this API process")
    pid = state.supervisor.kill(body.name)
    if pid is None:
        raise HTTPException(404, f"no process named {body.name!r}")
    with write_db() as s:
        audit.record(s, actor_kind="user", actor=p.email, action="chaos.kill", resource=f"process/{body.name}",
                     detail={"pid": pid})
    return {"killed": body.name, "pid": pid, "note": "the supervisor restarts it within a second"}


# ------------------------------------------------------------------ simulator
class SimPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = Field(None, pattern="^(realistic|all)$")
    hz: float | None = Field(None, ge=0.1, le=10)
    max_eps: int | None = Field(None, ge=0, le=2_000_000)
    enabled: list[str] | None = Field(None, max_length=len(OEM_KEYS))
    drift_pct: int | None = Field(None, ge=0, le=100)
    dup_pct: float | None = Field(None, ge=0, le=50)
    reorder_pct: float | None = Field(None, ge=0, le=50)
    malformed_pct: float | None = Field(None, ge=0, le=50)
    outage_pct: int | None = Field(None, ge=0, le=100)
    burst: float | None = Field(None, ge=0, le=50)
    paused: bool | None = None


SCENARIOS: dict[str, dict[str, Any]] = {
    "launch_helix": {"_enable": "helix"},
    "stop_helix": {"_disable": "helix"},
    "ota_drift": {"drift_pct": 35},
    "ota_reset": {"drift_pct": 0},
    "outage": {"outage_pct": 20},
    "recover": {"outage_pct": 0},
    "shift_start": {"burst": 30.0},
    "calm": {"burst": 1.0},
    "pause": {"paused": True},
    "resume": {"paused": False},
    # the whole demo back to its opening state, including dropping the new maker again.
    # Each scenario also has its own narrow off switch, so ending one never ends another.
    "reset": {"drift_pct": 0, "outage_pct": 0, "burst": 1.0, "paused": False, "dup_pct": 1.5,
              "reorder_pct": 2.0, "malformed_pct": 0.05, "mode": "realistic", "hz": 1.0,
              "enabled": [k for k in OEM_KEYS if k != "helix"]},
}


def _current() -> dict[str, Any]:
    sims = list(state.agg.sim.values())
    return dict(sims[0].get("settings", {})) if sims else {}


def _apply(patch: dict[str, Any], who: Principal, label: str) -> dict[str, Any]:
    en, dis = patch.pop("_enable", None), patch.pop("_disable", None)
    if en or dis:
        cur = _current().get("enabled") or [k for k in OEM_KEYS if k != "helix"]
        wanted = set(cur)
        if en:
            wanted.add(en)
        if dis:
            wanted.discard(dis)
        if not wanted:
            raise HTTPException(422, "that would leave the fleet with no sources")
        patch["enabled"] = sorted(wanted, key=OEM_KEYS.index)
    if "enabled" in patch:
        bad = [k for k in patch["enabled"] if k not in OEM_KEYS]
        if bad:
            raise HTTPException(422, f"unknown sources: {bad}")
    send_control(state.broker, patch)
    with write_db() as s:
        audit.record(s, actor_kind="user", actor=who.email, action="simulator.control",
                     resource=f"simulator/{label}", detail=patch)
    return {"applied": patch, "note": "takes effect on the next simulator tick"}


@router.get("/simulator", summary="Current simulator settings and scenarios")
def sim_get(_: Principal = Depends(require(*EVERYONE))):
    return {"settings": _current(), "scenarios": sorted(SCENARIOS), "sources": list(OEM_KEYS),
            "running": bool(state.agg.sim)}


@router.post("/simulator", summary="Change simulator settings")
def sim_patch(body: SimPatch, p: Principal = Depends(require(*OPERATORS))):
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(422, "nothing to change")
    return _apply(patch, p, "settings")


@router.post("/simulator/scenario/{name}", summary="Run a named scenario")
def sim_scenario(name: str, p: Principal = Depends(require(*OPERATORS))):
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario. Available: {sorted(SCENARIOS)}")
    return _apply(dict(SCENARIOS[name]), p, name)


def prometheus() -> PlainTextResponse:
    return PlainTextResponse(state.agg.prometheus(), media_type="text/plain; version=0.0.4")
