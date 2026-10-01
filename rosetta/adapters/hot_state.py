"""Hot state adapters.

MmapHotState   one fixed-size row per vehicle in a memory-mapped file. The
               stream processor writes, the API reads, no server in between.
               A read is an array lookup: O(1). Used by the local mode.
RedisHotState  one hash per vehicle plus a geo index. Used in production, where
               API replicas and processors run on different machines.

Both apply last-write-wins by event time, which makes the update idempotent:
replaying an old event can never overwrite a newer state.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from ..domain.canonical import EVENT_TYPES

ROW = np.dtype([
    ("ts", "<i8"), ("rx_ts", "<i8"), ("seen_ts", "<i8"), ("seq", "<i8"),
    ("lat", "<f8"), ("lon", "<f8"), ("speed_kmh", "<f4"), ("heading_deg", "<f4"),
    ("odo_km", "<f8"), ("soc_pct", "<f4"), ("fuel_pct", "<f4"), ("ambient_c", "<f4"),
    ("ignition", "i1"), ("oem", "i1"), ("evt", "i1"), ("dtc_n", "i1"), ("map_v", "<i4"),
    ("events", "<i8"),
])
EVT_INDEX = {name: i + 1 for i, name in enumerate(EVENT_TYPES)}
EVT_NAME = {i + 1: name for i, name in enumerate(EVENT_TYPES)}
_F32 = ("speed_kmh", "heading_deg", "soc_pct", "fuel_pct", "ambient_c")


class MmapHotState:
    def __init__(self, path: str | os.PathLike, vins: Iterable[str], oem_keys: tuple[str, ...],
                 writable: bool = False) -> None:
        self.path = Path(path)
        self.vins = list(vins)
        self.index = {v: i for i, v in enumerate(self.vins)}
        self.oem_keys = oem_keys
        self.oem_index = {k: i for i, k in enumerate(oem_keys)}
        n = len(self.vins)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size != n * ROW.itemsize
        if fresh and not writable:
            # Reader started first: create an empty file so both sides can map it.
            np.memmap(self.path, dtype=ROW, mode="w+", shape=(n,)).flush()
            fresh = False
        self.rows = np.memmap(self.path, dtype=ROW, mode="w+" if fresh else ("r+" if writable else "r"), shape=(n,))
        if fresh:
            for f in _F32:
                self.rows[f] = np.nan
            self.rows["oem"] = -1

    def update_batch(self, cols: dict[str, np.ndarray]) -> int:
        """Apply a batch of canonical events. Keeps the newest event per vehicle."""
        idx = cols["idx"]
        if idx.size == 0:
            return 0
        ts = cols["ts"]
        # within the batch keep only the newest event of each vehicle
        order = np.lexsort((ts, idx))
        idx_s = idx[order]
        last = np.ones(idx_s.size, dtype=bool)
        last[:-1] = idx_s[1:] != idx_s[:-1]
        counts = np.bincount(idx, minlength=len(self.rows))
        touched = np.flatnonzero(counts)
        self.rows["events"][touched] += counts[touched]
        sel = order[last]
        tgt = idx[sel]
        newer = ts[sel] >= self.rows["ts"][tgt]
        sel, tgt = sel[newer], tgt[newer]
        for name in ("ts", "rx_ts", "seen_ts", "seq", "lat", "lon", "odo_km", "ignition", "oem",
                     "evt", "dtc_n", "map_v") + _F32:
            self.rows[name][tgt] = cols[name][sel]
        return int(tgt.size)

    def get(self, vin: str) -> dict[str, Any] | None:
        i = self.index.get(vin)
        if i is None:
            return None
        r = self.rows[i]
        if r["ts"] == 0:
            return None
        return row_to_dict(r, vin, self.oem_keys)

    def snapshot(self) -> dict[str, np.ndarray]:
        rows = np.array(self.rows)  # one consistent copy
        return {name: rows[name] for name in ROW.names}

    def flush(self) -> None:
        if isinstance(self.rows, np.memmap):
            self.rows.flush()

    def close(self) -> None:
        self.flush()


def _f(v: Any) -> float | None:
    v = float(v)
    return None if v != v else round(v, 3)


def row_to_dict(r: Any, vin: str, oem_keys: tuple[str, ...]) -> dict[str, Any]:
    o = int(r["oem"])
    return {
        "vin": vin, "ts": int(r["ts"]), "rx_ts": int(r["rx_ts"]), "seen_ts": int(r["seen_ts"]),
        "seq": int(r["seq"]), "lat": round(float(r["lat"]), 6), "lon": round(float(r["lon"]), 6),
        "speed_kmh": _f(r["speed_kmh"]), "heading_deg": _f(r["heading_deg"]),
        "odo_km": round(float(r["odo_km"]), 3), "soc_pct": _f(r["soc_pct"]), "fuel_pct": _f(r["fuel_pct"]),
        "ambient_c": _f(r["ambient_c"]), "ignition": bool(r["ignition"]),
        "oem": oem_keys[o] if 0 <= o < len(oem_keys) else None,
        "evt": EVT_NAME.get(int(r["evt"])), "dtc_count": int(r["dtc_n"]), "map_v": int(r["map_v"]),
        "events": int(r["events"]),
    }


class RedisHotState:
    """Production adapter: the same fixed-size rows, stored in one Redis string.

    Redis strings are byte-addressable (SETRANGE / GETRANGE), so the whole fleet
    is one value of n * 92 bytes, about 9.2 MB for 100,000 vehicles:

      write     one SETRANGE per updated vehicle, sent in a pipeline
      read one  GETRANGE of 92 bytes: O(1)
      read all  one GET, decoded with numpy without copying

    Each vehicle is written by exactly one processor, the one that owns its
    partition, so last-write-wins is decided by that writer and needs no lock.
    """

    KEY = "rosetta:hot:rows"

    def __init__(self, url: str, vins: Iterable[str], oem_keys: tuple[str, ...], client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(url)
        self.r = client
        self.vins = list(vins)
        self.index = {v: i for i, v in enumerate(self.vins)}
        self.oem_keys = oem_keys
        self.n = len(self.vins)
        self.size = ROW.itemsize
        self._ts = np.zeros(self.n, dtype=np.int64)       # writer-side copy for last-write-wins
        self._events = np.zeros(self.n, dtype=np.int64)
        if self.r.strlen(self.KEY) != self.n * self.size:
            blank = np.zeros(self.n, dtype=ROW)
            for f in _F32:
                blank[f] = np.nan
            blank["oem"] = -1
            self.r.set(self.KEY, blank.tobytes())
        else:
            rows = self._all()
            self._ts[:] = rows["ts"]
            self._events[:] = rows["events"]

    def _all(self) -> np.ndarray:
        raw = self.r.get(self.KEY) or b""
        if len(raw) != self.n * self.size:
            return np.zeros(self.n, dtype=ROW)
        return np.frombuffer(raw, dtype=ROW)

    def update_batch(self, cols: dict[str, np.ndarray]) -> int:
        idx = cols["idx"]
        if idx.size == 0:
            return 0
        ts = cols["ts"]
        order = np.lexsort((ts, idx))
        idx_s = idx[order]
        last = np.ones(idx_s.size, dtype=bool)
        last[:-1] = idx_s[1:] != idx_s[:-1]
        counts = np.bincount(idx, minlength=self.n)
        self._events += counts
        sel = order[last]
        tgt = idx[sel]
        newer = ts[sel] >= self._ts[tgt]
        stale = tgt[~newer]
        sel, tgt = sel[newer], tgt[newer]
        if stale.size:                      # an older event still counts as received
            off = ROW.fields["events"][1]
            pipe = self.r.pipeline(transaction=False)
            for i in stale.tolist():
                pipe.setrange(self.KEY, i * self.size + off, np.int64(self._events[i]).tobytes())
            pipe.execute()
        if not tgt.size:
            return 0
        rows = np.zeros(tgt.size, dtype=ROW)
        for name in ("ts", "rx_ts", "seen_ts", "seq", "lat", "lon", "odo_km", "ignition", "oem",
                     "evt", "dtc_n", "map_v") + _F32:
            rows[name] = cols[name][sel]
        rows["events"] = self._events[tgt]
        self._ts[tgt] = ts[sel]
        raw = rows.tobytes()
        size = self.size
        pipe = self.r.pipeline(transaction=False)
        for k, i in enumerate(tgt.tolist()):
            pipe.setrange(self.KEY, i * size, raw[k * size:(k + 1) * size])
        pipe.execute()
        return int(tgt.size)

    def get(self, vin: str) -> dict[str, Any] | None:
        i = self.index.get(vin)
        if i is None:
            return None
        raw = self.r.getrange(self.KEY, i * self.size, (i + 1) * self.size - 1)
        if len(raw) != self.size:
            return None
        r = np.frombuffer(raw, dtype=ROW)[0]
        if r["ts"] == 0:
            return None
        return row_to_dict(r, vin, self.oem_keys)

    def snapshot(self) -> dict[str, np.ndarray]:
        rows = self._all()
        return {name: rows[name] for name in ROW.names}

    def flush(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.r.close()
        except Exception:
            pass
