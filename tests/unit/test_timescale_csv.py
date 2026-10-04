"""rosetta.adapters.timescale.to_csv: the text PostgreSQL's COPY reads."""
from __future__ import annotations

import csv
import io

import pyarrow as pa

from rosetta.adapters.timescale import COLUMNS, to_csv


def test_csv_has_iso_utc_timestamps_and_array_literals():
    n = 2
    t = pa.table({
        "ts": pa.array([1_790_772_089_930, 1_790_772_090_001], pa.int64()),
        **{c: pa.array([None] * n, pa.string()) for c in COLUMNS if c not in ("ts", "dtc")},
        "dtc": pa.array([["P0301", "P0420"], None], pa.list_(pa.string())),
    })
    rows = list(csv.reader(io.StringIO(to_csv(t).decode())))
    assert len(rows) == 2 and all(len(r) == len(COLUMNS) for r in rows)
    ts, dtc = COLUMNS.index("ts"), COLUMNS.index("dtc")
    # Regression: strftime with %f wrote "...930000.%f+00", which PostgreSQL rejects.
    assert rows[0][ts] == "2026-09-30 12:41:29.930000Z"
    assert rows[1][ts] == "2026-09-30 12:41:30.001000Z"
    assert rows[0][dtc] == "{P0301,P0420}" and rows[1][dtc] == "{}"
