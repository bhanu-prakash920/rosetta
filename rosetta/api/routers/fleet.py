"""Fleet data: vehicles, live map, history, trips, alerts.

Two rules apply to every endpoint here.
  Tenancy   a caller with a tenant only ever sees that tenant's vehicles. A
            vehicle of another tenant answers 404, not 403, so its existence
            is not revealed.
  Masking   callers without the right to precise location get positions
            snapped to a geohash cell (about 5 km), and a shortened VIN.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from ...adapters.hot_state import EVT_NAME
from ...algorithms import geohash
from ...algorithms.trip_segmentation import segment_trips, threshold_baseline, to_segments
from ...config import get_settings
from ...db.models import Alert, Fleet, Oem, Tenant, Vehicle
from ...simulator.fleet import OEM_KEYS
from ..security import EVERYONE, Principal, require
from ..state import read_db, state
from ..support import decode_cursor, encode_cursor, page

router = APIRouter(tags=["fleet"])
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def mask_vin(vin: str) -> str:
    return "•" * 11 + vin[-6:]


def mask_position(d: dict[str, Any], precision: int) -> dict[str, Any]:
    if d.get("lat") is None:
        return d
    la, lo, gh = geohash.mask(d["lat"], d["lon"], precision)
    return {**d, "lat": round(la, 4), "lon": round(lo, 4), "geohash": gh, "location_masked": True}


def _own(s: Any, vin: str, p: Principal) -> tuple[Vehicle, int]:
    """The vehicle, if the caller may see it. Otherwise 404."""
    if not VIN_RE.match(vin):
        raise HTTPException(404, "vehicle not found")
    row = s.execute(select(Vehicle, Fleet.tenant_id).join(Fleet, Fleet.id == Vehicle.fleet_id)
                    .where(Vehicle.vin == vin)).first()
    if row is None or (p.tenant_id is not None and row[1] != p.tenant_id):
        raise HTTPException(404, "vehicle not found")
    return row[0], row[1]


@router.get("/vehicles", summary="Vehicles, keyset paginated")
def vehicles(cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
             oem: str | None = Query(None, max_length=32), fleet_id: int | None = Query(None, ge=1),
             q: str | None = Query(None, min_length=3, max_length=17, pattern=r"^[A-Za-z0-9]+$"),
             p: Principal = Depends(require(*EVERYONE))):
    after = decode_cursor(cursor)
    with read_db() as s:
        # One query with joins: no per-row lookups (the ORM N+1 pattern) for fleet or OEM names.
        stmt = (select(Vehicle.id, Vehicle.vin, Vehicle.powertrain, Vehicle.model_year, Oem.key, Fleet.id,
                       Fleet.name, Fleet.tenant_id)
                .join(Fleet, Fleet.id == Vehicle.fleet_id).join(Oem, Oem.id == Vehicle.oem_id))
        if p.tenant_id is not None:
            stmt = stmt.where(Fleet.tenant_id == p.tenant_id)
        if oem:
            stmt = stmt.where(Oem.key == oem)
        if fleet_id:
            stmt = stmt.where(Vehicle.fleet_id == fleet_id)
        if q:
            stmt = stmt.where(Vehicle.vin.like(q.upper() + "%"))
        if after:
            stmt = stmt.where(Vehicle.id > int(after[0]))      # keyset: no OFFSET scan
        rows = s.execute(stmt.order_by(Vehicle.id).limit(limit + 1)).all()
    prec = get_settings().mask_precision
    items = []
    for vid, vin, pt, year, okey, fid, fname, tid in rows:
        d: dict[str, Any] = {"id": vid, "vin": vin if p.sees_precise_location else mask_vin(vin),
                             "vin_ref": vin if p.sees_precise_location else None, "oem": okey,
                             "powertrain": pt, "model_year": year, "fleet": {"id": fid, "name": fname},
                             "tenant_id": tid}
        live = state.hot.get(vin) if state.hot is not None else None
        if live:
            live.pop("vin", None)
            d["live"] = live if p.sees_precise_location else mask_position(live, prec)
        items.append(d)
    return page(items, limit, lambda it: encode_cursor(it["id"]))


@router.get("/vehicles/{vin}", summary="Latest known state of one vehicle")
def vehicle(vin: str, p: Principal = Depends(require(*EVERYONE))):
    if not p.sees_precise_location:
        raise HTTPException(403, "your role sees vehicles only through masked lists and the map")
    with read_db() as s:
        v, tid = _own(s, vin.upper(), p)
        oem = s.get(Oem, v.oem_id)
        fl = s.get(Fleet, v.fleet_id)
        out = {"vin": v.vin, "device_id": v.device_id, "oem": oem.key if oem else None,
               "powertrain": v.powertrain, "model_year": v.model_year,
               "fleet": {"id": fl.id, "name": fl.name} if fl else None, "tenant_id": tid}
    out["live"] = state.hot.get(v.vin) if state.hot is not None else None
    return out


@router.get("/vehicles/{vin}/history", summary="Recent canonical events of one vehicle, from the archive")
def history(vin: str, limit: int = Query(300, ge=1, le=2000), p: Principal = Depends(require(*EVERYONE))):
    if not p.sees_precise_location:
        raise HTTPException(403, "your role does not include vehicle history")
    with read_db() as s:
        v, _ = _own(s, vin.upper(), p)
    t = state.archive.vehicle_history(v.vin, limit)
    cols = ["ts", "seq", "lat", "lon", "speed_kmh", "heading_deg", "odo_km", "soc_pct", "fuel_pct",
            "ignition", "dtc", "evt", "map_v", "oem"]
    rows = t.select(cols).to_pylist()
    for r in rows:      # stored as float32: show the precision that was sent, not float noise
        for k, nd in (("speed_kmh", 2), ("heading_deg", 2), ("soc_pct", 2), ("fuel_pct", 2), ("lat", 6), ("lon", 6)):
            if isinstance(r.get(k), float):
                r[k] = round(r[k], nd)
    return {"vin": v.vin, "count": len(rows), "items": rows}


@router.get("/vehicles/{vin}/trips", summary="Trips and stops, segmented with dynamic programming")
def trips(vin: str, min_stop_s: int = Query(90, ge=10, le=1800), p: Principal = Depends(require(*EVERYONE))):
    if not p.sees_precise_location:
        raise HTTPException(403, "your role does not include trips")
    with read_db() as s:
        v, _ = _own(s, vin.upper(), p)
    t = state.archive.vehicle_history(v.vin, 2000)
    if t.num_rows < 3:
        return {"vin": v.vin, "points": t.num_rows, "segments": [], "baseline_segments": 0}
    ts = t["ts"].to_numpy()
    lat, lon = t["lat"].to_numpy(), t["lon"].to_numpy()
    spd = t["speed_kmh"].to_numpy().astype(np.float64)
    segs = segment_trips(ts, lat, lon, spd, float(min_stop_s))
    order = np.argsort(ts, kind="stable")
    base = to_segments(threshold_baseline(spd[order]), ts[order], lat[order], lon[order], spd[order])
    return {"vin": v.vin, "points": int(t.num_rows),
            "segments": [{"kind": g.kind, "start_ts": g.start_ts, "end_ts": g.end_ts,
                          "duration_s": round(g.duration_s, 1), "distance_km": round(g.distance_km, 3),
                          "max_speed_kmh": round(g.max_speed_kmh, 1), "points": g.end - g.start + 1} for g in segs],
            "baseline_segments": len(base),
            "note": "baseline is a plain speed threshold; the difference is the flicker the DP removes"}


def _visible(p: Principal, snap: dict[str, np.ndarray]) -> np.ndarray:
    ok = snap["ts"] > 0
    if p.tenant_id is not None and len(state.tenant_of) == len(ok):
        ok &= state.tenant_of == p.tenant_id
    return ok


@router.get("/map/points", summary="Vehicle positions inside a bounding box")
def map_points(south: float = Query(-90, ge=-90, le=90), north: float = Query(90, ge=-90, le=90),
               west: float = Query(-180, ge=-180, le=180), east: float = Query(180, ge=-180, le=180),
               limit: int = Query(4000, ge=1, le=20000), oem: str | None = Query(None, max_length=32),
               p: Principal = Depends(require(*EVERYONE))):
    # The same view for the same visibility is computed once per second, whoever asks.
    key = (f"points:{p.tenant_id}:{p.sees_precise_location}:{round(south, 2)}:{round(north, 2)}:"
           f"{round(west, 2)}:{round(east, 2)}:{limit}:{oem}")
    return state.rendered(key, 1.0, lambda: _map_points(south, north, west, east, limit, oem, p))


def _map_points(south: float = Query(-90, ge=-90, le=90), north: float = Query(90, ge=-90, le=90),
               west: float = Query(-180, ge=-180, le=180), east: float = Query(180, ge=-180, le=180),
               limit: int = Query(4000, ge=1, le=20000), oem: str | None = Query(None, max_length=32),
               p: Principal = Depends(require(*EVERYONE))):
    if state.hot is None:
        return {"count": 0, "total_in_view": 0, "points": []}
    snap = state.cached("snap", 0.5, state.hot.snapshot)
    ok = _visible(p, snap)
    ok &= (snap["lat"] >= south) & (snap["lat"] <= north) & (snap["lon"] >= west) & (snap["lon"] <= east)
    if oem in OEM_KEYS:
        ok &= snap["oem"] == OEM_KEYS.index(oem)
    idx = np.flatnonzero(ok)
    total = int(idx.size)
    if idx.size > limit:
        idx = idx[:: int(np.ceil(idx.size / limit))]
    lat, lon = snap["lat"][idx], snap["lon"][idx]
    masked = not p.sees_precise_location
    if masked:
        prec = get_settings().mask_precision
        cells = geohash.encode_many(lat, lon, prec)
        uniq, inv = np.unique(cells, return_inverse=True)
        centres = np.array([geohash.decode(c.decode()) for c in uniq]) if uniq.size else np.zeros((0, 2))
        lat, lon = centres[inv, 0], centres[inv, 1]
    pts = np.column_stack([np.round(lat, 5), np.round(lon, 5), np.round(snap["speed_kmh"][idx], 1),
                           snap["oem"][idx], snap["ignition"][idx], snap["evt"][idx], snap["dtc_n"][idx]])
    out: dict[str, Any] = {"count": int(idx.size), "total_in_view": total, "masked": masked,
                           "columns": ["lat", "lon", "speed_kmh", "oem", "ignition", "evt", "dtc_count"],
                           "oems": list(OEM_KEYS), "events": {str(k): v for k, v in EVT_NAME.items()},
                           "points": pts}          # numpy, serialised directly by orjson
    if not masked:
        out["vins"] = [state.vins[i] for i in idx.tolist()]
    return out


@router.get("/map/cells", summary="Vehicle density per geohash cell")
def map_cells(precision: int = Query(4, ge=2, le=6), p: Principal = Depends(require(*EVERYONE))):
    return state.rendered(f"cells:{p.tenant_id}:{precision}", 2.0, lambda: _map_cells(precision, p))


def _map_cells(precision: int, p: Principal) -> dict[str, Any]:
    if state.hot is None:
        return {"precision": precision, "cells": []}
    snap = state.cached("snap", 0.5, state.hot.snapshot)
    idx = np.flatnonzero(_visible(p, snap))
    if not idx.size:
        return {"precision": precision, "cells": [], "vehicles": 0}
    cells = geohash.encode_many(snap["lat"][idx], snap["lon"][idx], precision)
    uniq, inv, counts = np.unique(cells, return_inverse=True, return_counts=True)
    moving = np.bincount(inv, weights=(snap["ignition"][idx] > 0).astype(float), minlength=uniq.size)
    out = []
    for c, n, m in zip(uniq.tolist(), counts.tolist(), moving.tolist()):
        gh = c.decode()
        la, lo = geohash.decode(gh)
        out.append({"geohash": gh, "lat": round(la, 4), "lon": round(lo, 4), "vehicles": n, "moving": int(m)})
    out.sort(key=lambda c: -c["vehicles"])
    return {"precision": precision, "vehicles": int(idx.size), "cells": out[:3000],
            "cell_km": geohash.CELL_KM.get(precision)}


@router.get("/alerts", summary="Alerts, newest first, keyset paginated")
def alerts(cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
           severity: str | None = Query(None, pattern="^(info|warning|critical)$"),
           kind: str | None = Query(None, max_length=32), p: Principal = Depends(require(*EVERYONE))):
    after = decode_cursor(cursor, 2)
    with read_db() as s:
        stmt = (select(Alert.id, Alert.kind, Alert.severity, Alert.ts, Alert.detected_ts, Alert.detail,
                       Vehicle.vin, Oem.key)
                .join(Vehicle, Vehicle.id == Alert.vehicle_id).join(Oem, Oem.id == Vehicle.oem_id))
        if p.tenant_id is not None:
            stmt = stmt.join(Fleet, Fleet.id == Vehicle.fleet_id).where(Fleet.tenant_id == p.tenant_id)
        if severity:
            stmt = stmt.where(Alert.severity == severity)
        if kind:
            stmt = stmt.where(Alert.kind == kind)
        if after:
            ts, aid = int(after[0]), int(after[1])
            stmt = stmt.where((Alert.ts < ts) | ((Alert.ts == ts) & (Alert.id < aid)))
        rows = s.execute(stmt.order_by(Alert.ts.desc(), Alert.id.desc()).limit(limit + 1)).all()
    prec = get_settings().mask_precision
    items = []
    for aid, k, sev, ts, det_ts, detail, vin, okey in rows:
        d = dict(detail or {})
        if not p.sees_precise_location and "lat" in d:
            d = mask_position(d, prec)
        items.append({"id": aid, "kind": k, "severity": sev, "ts": ts, "detected_ts": det_ts,
                      "detection_ms": det_ts - ts, "oem": okey,
                      "vin": vin if p.sees_precise_location else mask_vin(vin), "detail": d})
    return page(items, limit, lambda it: encode_cursor(it["ts"], it["id"]))


@router.get("/fleets", summary="Fleets the caller may see")
def fleets(p: Principal = Depends(require(*EVERYONE))):
    with read_db() as s:
        stmt = select(Fleet.id, Fleet.name, Fleet.home_city, Tenant.id, Tenant.name).join(Tenant, Tenant.id == Fleet.tenant_id)
        if p.tenant_id is not None:
            stmt = stmt.where(Fleet.tenant_id == p.tenant_id)
        rows = s.execute(stmt.order_by(Fleet.id).limit(500)).all()
    return {"items": [{"id": a, "name": b, "home_city": c, "tenant": {"id": d, "name": e}} for a, b, c, d, e in rows]}
