"""TimescaleDB sink: canonical events into a hypertable, idempotently.

Rows are loaded with COPY into a temporary table and then inserted with
ON CONFLICT DO NOTHING on (vin, ts, seq). COPY is the fastest way into
PostgreSQL, and the conflict clause makes a replayed batch harmless.

Schema: infra/sql/postgres_extras.sql (hypertable, compression, retention,
continuous aggregate).
"""
from __future__ import annotations

import io
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

COLUMNS = ("ts", "vin", "seq", "oem", "map_v", "lat", "lon", "speed_kmh", "heading_deg", "odo_km",
           "soc_pct", "fuel_pct", "ambient_c", "ignition", "evt", "dtc", "rx_ts", "geohash5")


def to_csv(table: pa.Table) -> bytes:
    """Arrow table -> CSV bytes in COLUMNS order. Timestamps are written by the CSV
    writer itself as `2026-09-30 12:41:29.930000Z`, which PostgreSQL reads as UTC.
    (strftime is not used: Arrow has no %f, and a literal "%f" reached the database.)
    DTC lists become PostgreSQL array literals."""
    ts = pc.cast(pc.cast(table["ts"], pa.timestamp("ms", tz="UTC")), pa.timestamp("us", tz="UTC"))
    dtc = pa.array(["{" + ",".join(x or []) + "}" for x in table["dtc"].to_pylist()], pa.string())
    out = {"ts": ts, "dtc": dtc}
    for c in COLUMNS:
        if c not in out:
            out[c] = table[c]
    buf = io.BytesIO()
    pacsv.write_csv(pa.table({c: out[c] for c in COLUMNS}), buf,
                    write_options=pacsv.WriteOptions(include_header=False))
    return buf.getvalue()


class TimescaleSink:
    def __init__(self, url: str) -> None:
        import psycopg2

        dsn = url.replace("postgresql+psycopg2://", "postgresql://")
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False
        self.rows_written = 0

    def write(self, table: Any) -> int:
        if table.num_rows == 0:
            return 0
        data = to_csv(table)
        try:
            n = self._load(data)
        except Exception:
            # Without this the connection stays in an aborted transaction and every
            # later batch fails with InFailedSqlTransaction.
            self.conn.rollback()
            raise
        self.rows_written += max(0, n)
        return max(0, n)

    def _load(self, data: bytes) -> int:
        cols = ", ".join(COLUMNS)
        with self.conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS telemetry_stage (LIKE telemetry INCLUDING DEFAULTS) "
                        "ON COMMIT DELETE ROWS")
            cur.copy_expert(f"COPY telemetry_stage ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')", io.BytesIO(data))
            cur.execute(f"INSERT INTO telemetry ({cols}) SELECT {cols} FROM telemetry_stage "  # noqa: S608 # nosec B608 - cols is the constant COLUMNS
                        "ON CONFLICT (vin, ts, seq) DO NOTHING")
            n = cur.rowcount
        self.conn.commit()
        return n

    def history(self, vin: str, limit: int = 300) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT (extract(epoch from ts) * 1000)::bigint, seq, lat, lon, speed_kmh, heading_deg, "
                        "odo_km, soc_pct, fuel_pct, ignition, dtc, evt, map_v, oem FROM telemetry "
                        "WHERE vin = %s ORDER BY ts DESC LIMIT %s", (vin, limit))
            names = ("ts", "seq", "lat", "lon", "speed_kmh", "heading_deg", "odo_km", "soc_pct", "fuel_pct",
                     "ignition", "dtc", "evt", "map_v", "oem")
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        self.conn.rollback()
        return rows

    def close(self) -> None:
        self.conn.close()
