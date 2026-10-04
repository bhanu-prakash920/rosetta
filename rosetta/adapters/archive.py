"""Telemetry archive: Parquet micro-batches, partitioned by date and hour.

Layout:  <root>/dt=2026-09-25/hour=10/part-<writer>-<ms>.parquet

Each file is sorted by VIN and written in row groups, so the per-row-group
min/max statistics let a "history of one vehicle" query skip almost every
group. The same code writes to the local disk or to any S3-compatible store
(MinIO, AWS S3, GCS and Azure through their S3 gateways): only the filesystem
object differs, which is what keeps the pipeline cloud-agnostic.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from pyarrow import fs as pafs

SCHEMA = pa.schema([
    ("vin", pa.string()), ("ts", pa.int64()), ("seq", pa.int64()), ("oem", pa.string()),
    ("map_v", pa.int32()), ("lat", pa.float64()), ("lon", pa.float64()),
    ("speed_kmh", pa.float32()), ("heading_deg", pa.float32()), ("odo_km", pa.float64()),
    ("soc_pct", pa.float32()), ("fuel_pct", pa.float32()), ("ambient_c", pa.float32()),
    ("ignition", pa.bool_()), ("dtc", pa.list_(pa.string())), ("evt", pa.string()),
    ("rx_ts", pa.int64()), ("norm_ts", pa.int64()), ("replayed", pa.bool_()), ("geohash5", pa.string()),
])
COLUMNS = tuple(SCHEMA.names)
OFFSETS_KEY = b"rosetta.offsets"


def events_to_table(events: list[dict[str, Any]], geohash5: list[str] | None = None) -> pa.Table:
    cols = {}
    for f in SCHEMA:
        if f.name == "geohash5":
            cols[f.name] = pa.array(geohash5 if geohash5 is not None else [None] * len(events), f.type)
        else:
            cols[f.name] = pa.array([e.get(f.name) for e in events], f.type)
    return pa.table(cols, schema=SCHEMA)


class ParquetArchive:
    def __init__(self, root: str, filesystem: pafs.FileSystem | None = None, writer_id: str = "0",
                 retention_bytes: int = 0) -> None:
        self.root = root.rstrip("/")
        self.fs = filesystem or pafs.LocalFileSystem()
        self.local = isinstance(self.fs, pafs.LocalFileSystem)
        self.writer_id = writer_id
        self.retention_bytes = retention_bytes
        self.fs.create_dir(self.root, recursive=True)
        self.rows_written = 0
        self.bytes_written = 0

    @classmethod
    def s3(cls, bucket: str, endpoint: str, access_key: str, secret_key: str, **kw: Any) -> ParquetArchive:
        opts: dict[str, Any] = {}
        if endpoint:                    # MinIO or another S3-compatible store; empty means AWS itself
            scheme, _, host = endpoint.partition("://")
            opts.update(endpoint_override=host or endpoint, scheme=scheme or "https")
        if access_key:                  # static keys only when given: otherwise the default
            opts.update(access_key=access_key, secret_key=secret_key)   # chain (IRSA, instance role)
        filesystem = pafs.S3FileSystem(**opts)
        return cls(f"{bucket}/telemetry", filesystem, **kw)

    def write(self, table: pa.Table, ts_ms: int, offsets: dict[int, int] | None = None) -> str:
        """Write one file. `offsets` are the consumer positions this file brings the
        archive up to. They are stored inside the file, so data and position become
        durable in one atomic rename. After a crash the writer resumes from the
        position in its newest file, which gives the archive exactly-once rows even
        though the broker only promises at-least-once."""
        if table.num_rows == 0:
            return ""
        if offsets is not None:
            meta = dict(table.schema.metadata or {})
            meta[OFFSETS_KEY] = orjson.dumps({str(k): int(v) for k, v in offsets.items()})
            table = table.replace_schema_metadata(meta)
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        folder = f"{self.root}/dt={dt:%Y-%m-%d}/hour={dt:%H}"
        self.fs.create_dir(folder, recursive=True)
        path = f"{folder}/part-{self.writer_id}-{ts_ms}.parquet"
        table = table.sort_by([("vin", "ascending"), ("ts", "ascending")])
        tmp = path + ".tmp"
        pq.write_table(table, tmp, filesystem=self.fs, compression="zstd", row_group_size=20_000,
                       write_statistics=True)
        self.fs.move(tmp, path)  # readers never see a half-written file
        self.rows_written += table.num_rows
        try:
            self.bytes_written += self.fs.get_file_info(path).size
        except Exception:
            pass
        if self.retention_bytes and self.local:
            self._enforce_retention()
        return path

    def _enforce_retention(self) -> None:
        """Keep the archive under its byte budget, oldest files first.

        Several writers share one directory. Each deletes only its own files, and never
        its newest one (it holds the offsets this writer resumes from), so two writers
        never race on the same file. Files that vanish while being listed are skipped.
        """
        def size_and_mtime(p: Path) -> tuple[int, float] | None:
            try:
                st = p.stat()
                return st.st_size, st.st_mtime
            except FileNotFoundError:
                return None

        entries = [(p, m) for p in Path(self.root).rglob("*.parquet") if (m := size_and_mtime(p)) is not None]
        total = sum(sz for _p, (sz, _t) in entries)
        mine = sorted(((p, st) for p, st in entries if p.name.startswith(f"part-{self.writer_id}-")),
                      key=lambda e: e[1][1])
        for p, (sz, _t) in mine[:-1]:
            if total <= self.retention_bytes:
                break
            p.unlink(missing_ok=True)
            total -= sz

    def last_offsets(self) -> dict[int, int]:
        """Positions recorded in the newest file of this writer, or {} when there is none."""
        mine = [f for f in self.files() if f.rsplit("/", 1)[-1].startswith(f"part-{self.writer_id}-")]
        if not mine:
            return {}
        newest = max(mine, key=lambda f: int(f.rsplit("-", 1)[-1].split(".")[0]))
        try:
            meta = pq.read_metadata(newest, filesystem=self.fs).metadata or {}
            raw = meta.get(OFFSETS_KEY)
            return {int(k): int(v) for k, v in orjson.loads(raw).items()} if raw else {}
        except Exception:
            return {}

    def files(self) -> list[str]:
        sel = pafs.FileSelector(self.root, recursive=True, allow_not_found=True)
        return sorted(i.path for i in self.fs.get_file_info(sel) if i.is_file and i.path.endswith(".parquet"))

    def size_bytes(self) -> int:
        sel = pafs.FileSelector(self.root, recursive=True, allow_not_found=True)
        return sum(i.size for i in self.fs.get_file_info(sel) if i.is_file and i.path.endswith(".parquet"))

    def dataset(self) -> ds.Dataset | None:
        files = self.files()
        if not files:
            return None
        return ds.dataset(files, schema=SCHEMA, format="parquet", filesystem=self.fs)

    def vehicle_history(self, vin: str, limit: int = 500, since_ms: int = 0, max_files: int = 120) -> pa.Table:
        """Newest events of one vehicle, newest first.

        Reads files newest first and stops once `limit` rows are found. Inside each file
        the rows are sorted by VIN, so the row-group statistics let Parquet skip every
        group that cannot contain this VIN. Cost: a few small reads, not a scan of the
        archive. Older history is a batch question (rosetta/batch)."""
        files = sorted(self.files(), key=lambda f: int(f.rsplit("-", 1)[-1].split(".")[0]), reverse=True)
        flt = pc.field("vin") == vin
        if since_ms:
            flt = flt & (pc.field("ts") >= since_ms)
        parts, found = [], 0
        for f in files[:max_files]:
            try:
                t = pq.read_table(f, filesystem=self.fs, filters=flt, schema=SCHEMA)
            except (FileNotFoundError, OSError):
                continue                      # trimmed by retention while we read
            if t.num_rows:
                parts.append(t)
                found += t.num_rows
                if found >= limit:
                    break
        if not parts:
            return SCHEMA.empty_table()
        t = pa.concat_tables(parts).sort_by([("ts", "descending")])
        return t.slice(0, limit)

    def close(self) -> None:
        pass
