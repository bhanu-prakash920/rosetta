"""Batch analytics over the Parquet archive.

The stream answers "what is happening now". These jobs answer questions over
history, scanning columnar files instead of loading events one by one. They run
on pyarrow, which reads only the columns a query needs and skips row groups by
their statistics. The same files are readable by Spark, DuckDB, Trino or
Snowflake external tables, because Parquet is the interchange format.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from ..adapters.archive import ParquetArchive
from ..algorithms.trip_segmentation import segment_trips

OPTIONAL = ("heading_deg", "soc_pct", "fuel_pct", "ambient_c", "evt")


def _scan(archive: ParquetArchive, max_files: int, attempts: int = 3, **kw: Any) -> tuple[list[str], pa.Table]:
    """Read the newest files. Retention may delete a file between listing and reading;
    then list again. Returns (files, table)."""
    import pyarrow.dataset as ds

    last: Exception | None = None
    for _ in range(attempts):
        files = archive.files()[-max_files:]
        if not files:
            return [], pa.table({})
        try:
            return files, ds.dataset(files, format="parquet", filesystem=archive.fs).to_table(**kw)
        except (FileNotFoundError, OSError) as e:
            last = e
    raise last  # type: ignore[misc]


def quality_report(archive: ParquetArchive, max_files: int = 400) -> dict[str, Any]:
    """Data quality per source: volume, completeness, latency, ordering."""
    t0 = time.perf_counter()
    cols = ["oem", "vin", "ts", "rx_ts", "norm_ts", "map_v", "speed_kmh", "replayed", *OPTIONAL]
    files, t = _scan(archive, max_files, columns=cols)
    if not files:
        return {"files": 0, "rows": 0, "sources": [], "seconds": 0.0}
    # Replayed events waited in the dead-letter queue. Their wait is not processing
    # time, so they count as events but not towards the latency columns.
    fresh = pc.invert(pc.fill_null(t["replayed"], False))
    null_i64 = pa.scalar(None, pa.int64())
    t = t.append_column("norm_ms", pc.if_else(fresh, pc.subtract(t["norm_ts"], t["rx_ts"]), null_i64))
    t = t.append_column("age_ms", pc.if_else(fresh, pc.subtract(t["rx_ts"], t["ts"]), null_i64))
    t = t.append_column("replayed_i", pc.cast(pc.fill_null(t["replayed"], False), pa.int64()))
    aggs = [("vin", "count"), ("vin", "count_distinct"), ("norm_ms", "mean"), ("norm_ms", "max"),
            ("age_ms", "mean"), ("speed_kmh", "mean"), ("speed_kmh", "max"), ("map_v", "max"),
            ("ts", "min"), ("ts", "max"), ("replayed_i", "sum")]
    aggs += [(c, "count") for c in OPTIONAL]
    g = t.group_by("oem").aggregate(aggs).to_pylist()
    out = []
    for r in sorted(g, key=lambda r: -r["vin_count"]):
        n = r["vin_count"]
        span = max(1.0, (r["ts_max"] - r["ts_min"]) / 1000.0)
        out.append({
            "oem": r["oem"], "rows": n, "vehicles": r["vin_count_distinct"],
            "events_per_vehicle": round(n / max(1, r["vin_count_distinct"]), 1),
            "events_per_second": round(n / span, 1),
            "normalize_ms_mean": round(r["norm_ms_mean"] or 0, 2), "normalize_ms_max": r["norm_ms_max"],
            "arrival_delay_ms_mean": round(r["age_ms_mean"] or 0, 1),
            "speed_kmh_mean": round(r["speed_kmh_mean"] or 0, 2), "speed_kmh_max": round(r["speed_kmh_max"] or 0, 1),
            "latest_mapping_version": r["map_v_max"], "replayed": r["replayed_i_sum"],
            "completeness": {c: round(r[f"{c}_count"] / n, 4) for c in OPTIONAL},
        })
    return {"files": len(files), "rows": t.num_rows, "bytes": archive.size_bytes(),
            "bytes_per_row": round(archive.size_bytes() / max(1, t.num_rows), 1),
            "from_ts": pc.min(t["ts"]).as_py(), "to_ts": pc.max(t["ts"]).as_py(),
            "sources": out, "seconds": round(time.perf_counter() - t0, 3)}


def event_counts(archive: ParquetArchive, max_files: int = 400) -> dict[str, Any]:
    files, t = _scan(archive, max_files, columns=["oem", "evt"], filter=pc.field("evt").is_valid())
    if not files:
        return {"items": []}
    rows = t.group_by(["oem", "evt"]).aggregate([("evt", "count")]).to_pylist()
    return {"items": sorted(({"oem": r["oem"], "event": r["evt"], "count": r["evt_count"]} for r in rows),
                            key=lambda r: -r["count"])}


def hotspots(archive: ParquetArchive, top: int = 20, max_files: int = 400) -> dict[str, Any]:
    """Where harsh driving concentrates: events per geohash cell."""
    files, t = _scan(archive, max_files, columns=["geohash5", "evt"],
                     filter=pc.field("evt").isin(["HARSH_BRAKE", "HARSH_ACCEL", "SPEEDING"]))
    if not files:
        return {"items": []}
    rows = t.group_by("geohash5").aggregate([("evt", "count")]).sort_by([("evt_count", "descending")])
    from ..algorithms import geohash

    out = []
    for r in rows.slice(0, top).to_pylist():
        la, lo = geohash.decode(r["geohash5"])
        out.append({"geohash": r["geohash5"], "lat": round(la, 4), "lon": round(lo, 4), "events": r["evt_count"]})
    return {"items": out}


def build_trips(archive: ParquetArchive, vins: list[str], min_stop_s: float = 90.0) -> list[dict[str, Any]]:
    """Segment the archived history of the given vehicles into trips (batch job)."""
    d = archive.dataset()
    if d is None or not vins:
        return []
    t = d.to_table(columns=["vin", "ts", "lat", "lon", "speed_kmh"], filter=pc.field("vin").isin(vins))
    t = t.sort_by([("vin", "ascending"), ("ts", "ascending")])
    vin = t["vin"].to_numpy(zero_copy_only=False)
    ts, lat, lon = t["ts"].to_numpy(), t["lat"].to_numpy(), t["lon"].to_numpy()
    spd = t["speed_kmh"].to_numpy().astype(np.float64)
    out = []
    if not len(vin):
        return out
    cuts = np.flatnonzero(vin[1:] != vin[:-1]) + 1
    for a, b in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(vin)]])):
        if b - a < 3:
            continue
        for seg in segment_trips(ts[a:b], lat[a:b], lon[a:b], spd[a:b], min_stop_s):
            if seg.kind == "trip" and seg.distance_km > 0.05:
                out.append({"vin": str(vin[a]), "start_ts": seg.start_ts, "end_ts": seg.end_ts,
                            "distance_km": round(seg.distance_km, 3), "max_speed_kmh": round(seg.max_speed_kmh, 1),
                            "start": (float(lat[a + seg.start]), float(lon[a + seg.start])),
                            "end": (float(lat[a + seg.end]), float(lon[a + seg.end]))})
    return out
