"""Batch analytics over the archive."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...batch import reports
from ..security import STAFF, Principal, require
from ..state import state

router = APIRouter(prefix="/batch", tags=["batch analytics"])


@router.get("/quality", summary="Data quality per source, computed over the Parquet archive")
def quality(_: Principal = Depends(require(*STAFF))):
    return state.rendered("batch.quality", 15.0, lambda: reports.quality_report(state.archive), stale_ok=True)


@router.get("/events", summary="Driving events per source over the archive")
def events(_: Principal = Depends(require(*STAFF))):
    return state.rendered("batch.events", 15.0, lambda: reports.event_counts(state.archive), stale_ok=True)


@router.get("/hotspots", summary="Geohash cells with the most harsh-driving events")
def hotspots(_: Principal = Depends(require(*STAFF))):
    return state.rendered("batch.hotspots", 15.0, lambda: reports.hotspots(state.archive), stale_ok=True)
