"""rosetta.adapters.archive: Parquet micro-batches partitioned by date and hour."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rosetta.adapters.archive import COLUMNS, SCHEMA, ParquetArchive, events_to_table

VIN_A, VIN_B, VIN_C = "1HGCM82633A004352", "11111111111111111", "7NVEV1A20TA000000"
TS = int(datetime(2026, 9, 25, 10, 30, tzinfo=UTC).timestamp() * 1000)


def event(vin: str = VIN_A, ts: int = TS, seq: int = 1, **extra: Any) -> dict[str, Any]:
    ev = {"vin": vin, "ts": ts, "seq": seq, "oem": "nordvik", "map_v": 1, "lat": 12.9716, "lon": 77.5946,
          "speed_kmh": 54.25, "odo_km": 18234.5, "rx_ts": ts + 20, "norm_ts": ts + 30}
    ev.update(extra)
    return ev


@pytest.fixture()
def archive(tmp_path) -> ParquetArchive:
    return ParquetArchive(str(tmp_path / "archive"), writer_id="w1")


# ------------------------------------------------------------ events_to_table
def test_table_has_the_archive_schema():
    t = events_to_table([event()])
    assert t.schema == SCHEMA
    assert tuple(t.column_names) == COLUMNS
    assert t.num_rows == 1


def test_values_are_kept():
    t = events_to_table([event(heading_deg=181.0, soc_pct=76.5, ignition=True, dtc=["P0301", "U0100"],
                               evt="SPEEDING", replayed=True)], geohash5=["tdr1v"])
    row = t.to_pylist()[0]
    assert row["vin"] == VIN_A and row["ts"] == TS and row["lat"] == 12.9716
    assert row["speed_kmh"] == 54.25 and row["soc_pct"] == 76.5
    assert row["dtc"] == ["P0301", "U0100"] and row["evt"] == "SPEEDING"
    assert row["ignition"] is True and row["replayed"] is True and row["geohash5"] == "tdr1v"


def test_absent_fields_become_nulls():
    row = events_to_table([event()]).to_pylist()[0]
    for name in ("heading_deg", "soc_pct", "fuel_pct", "ambient_c", "ignition", "dtc", "evt", "replayed", "geohash5"):
        assert row[name] is None, name


def test_unknown_event_keys_are_ignored():
    assert events_to_table([event(rssi=-70, firmware="1.2")]).column_names == list(COLUMNS)


def test_empty_list_of_events():
    t = events_to_table([])
    assert t.num_rows == 0 and t.schema == SCHEMA


def test_value_of_the_wrong_type_is_rejected():
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        events_to_table([event(lat="north")])


# ---------------------------------------------------------------------- write
def test_root_directory_is_created(tmp_path):
    ParquetArchive(str(tmp_path / "a" / "b" / "archive"))
    assert (tmp_path / "a" / "b" / "archive").is_dir()


def test_trailing_slash_in_the_root_is_dropped(tmp_path):
    assert ParquetArchive(str(tmp_path / "archive") + "/").root == str(tmp_path / "archive")


def test_write_uses_date_and_hour_partitions(archive):
    path = archive.write(events_to_table([event()]), TS)
    assert path == f"{archive.root}/dt=2026-09-25/hour=10/part-w1-{TS}.parquet"
    assert os.path.isfile(path)


def test_partition_is_taken_from_utc(archive):
    just_before_midnight = int(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    path = archive.write(events_to_table([event(ts=just_before_midnight)]), just_before_midnight)
    assert "/dt=2026-12-31/hour=23/" in path


def test_written_file_is_sorted_by_vin_then_time(archive):
    rows = [event(VIN_B, TS + 2000, 3), event(VIN_A, TS + 5000, 9), event(VIN_C, TS, 1), event(VIN_A, TS + 1000, 8),
            event(VIN_B, TS, 2)]
    path = archive.write(events_to_table(rows), TS)
    stored = pq.ParquetFile(path).read()
    assert [(r["vin"], r["ts"]) for r in stored.to_pylist()] == sorted((r["vin"], r["ts"]) for r in rows)
    assert stored.schema.equals(SCHEMA, check_metadata=False)


def test_file_carries_statistics_for_row_group_pruning(archive):
    path = archive.write(events_to_table([event(VIN_A), event(VIN_C)]), TS)
    meta = pq.ParquetFile(path).metadata
    vin_col = meta.row_group(0).column(0)
    assert vin_col.statistics.min == VIN_A and vin_col.statistics.max == VIN_C
    assert vin_col.compression == "ZSTD"


def test_no_temporary_file_is_left_behind(archive):
    archive.write(events_to_table([event()]), TS)
    leftovers = [p for p in Path(archive.root).rglob("*") if p.is_file() and p.suffix != ".parquet"]
    assert leftovers == []


def test_empty_table_writes_nothing(archive):
    assert archive.write(events_to_table([]), TS) == ""
    assert archive.files() == []
    assert archive.rows_written == 0


def test_counters(archive):
    archive.write(events_to_table([event(seq=i) for i in range(10)]), TS)
    archive.write(events_to_table([event(seq=i) for i in range(10, 15)]), TS + 1000)
    assert archive.rows_written == 15
    assert archive.bytes_written == archive.size_bytes() > 0


def test_two_writers_do_not_collide(tmp_path):
    a = ParquetArchive(str(tmp_path / "archive"), writer_id="p0")
    b = ParquetArchive(str(tmp_path / "archive"), writer_id="p1")
    pa_, pb = a.write(events_to_table([event(VIN_A)]), TS), b.write(events_to_table([event(VIN_B)]), TS)
    assert pa_ != pb
    assert a.files() == b.files() == sorted([pa_, pb])


# ----------------------------------------------------------------------- read
def test_empty_archive(archive):
    assert archive.files() == []
    assert archive.size_bytes() == 0
    assert archive.dataset() is None
    history = archive.vehicle_history(VIN_A)
    assert history.num_rows == 0 and history.schema == SCHEMA


def test_files_are_listed_sorted_across_partitions(archive):
    hour = 3600 * 1000
    paths = [archive.write(events_to_table([event(ts=TS + k * hour)]), TS + k * hour) for k in (2, 0, 1)]
    assert archive.files() == sorted(paths)
    assert len({os.path.dirname(p) for p in paths}) == 3


def test_files_ignores_other_files(archive):
    path = archive.write(events_to_table([event()]), TS)
    (Path(archive.root) / "notes.txt").write_text("not data")
    assert archive.files() == [path]


def test_dataset_reads_every_file(archive):
    archive.write(events_to_table([event(VIN_A, seq=1), event(VIN_B, seq=2)]), TS)
    archive.write(events_to_table([event(VIN_C, seq=3)]), TS + 3_600_000)
    table = archive.dataset().to_table()
    assert table.num_rows == 3
    assert sorted(table.column("vin").to_pylist()) == sorted([VIN_A, VIN_B, VIN_C])


def test_vehicle_history_returns_one_vehicle_newest_first(archive):
    archive.write(events_to_table([event(VIN_A, TS + k * 1000, k) for k in range(5)] +
                                  [event(VIN_B, TS + k * 1000, k) for k in range(5)]), TS)
    archive.write(events_to_table([event(VIN_A, TS + 3_600_000 + k, 10 + k) for k in range(3)]), TS + 3_600_000)
    history = archive.vehicle_history(VIN_A).to_pylist()
    assert {r["vin"] for r in history} == {VIN_A}
    assert [r["seq"] for r in history] == [12, 11, 10, 4, 3, 2, 1, 0]


def test_vehicle_history_limit(archive):
    archive.write(events_to_table([event(VIN_A, TS + k * 1000, k) for k in range(20)]), TS)
    assert [r["seq"] for r in archive.vehicle_history(VIN_A, limit=3).to_pylist()] == [19, 18, 17]


def test_vehicle_history_since(archive):
    archive.write(events_to_table([event(VIN_A, TS + k * 1000, k) for k in range(10)]), TS)
    got = archive.vehicle_history(VIN_A, since_ms=TS + 7000).to_pylist()
    assert [r["seq"] for r in got] == [9, 8, 7], "the bound is inclusive"


def test_vehicle_history_of_an_unknown_vehicle(archive):
    archive.write(events_to_table([event(VIN_A)]), TS)
    assert archive.vehicle_history("WHX00000000000000").num_rows == 0


# ------------------------------------------------------------------ retention
def test_retention_deletes_the_oldest_files(tmp_path):
    probe = ParquetArchive(str(tmp_path / "probe"))
    probe.write(events_to_table([event(seq=i) for i in range(50)]), TS)
    one_file = probe.size_bytes()

    archive = ParquetArchive(str(tmp_path / "archive"), retention_bytes=int(one_file * 2.5))
    paths = []
    for k in range(6):
        path = archive.write(events_to_table([event(seq=i) for i in range(50)]), TS + k * 1000)
        os.utime(path, (1_790_000_000 + k, 1_790_000_000 + k))      # make the file ages unambiguous
        paths.append(path)
    archive.write(events_to_table([event(seq=i) for i in range(50)]), TS + 6000)

    left = archive.files()
    assert len(left) <= 3
    assert archive.size_bytes() <= int(one_file * 2.5) + one_file
    assert paths[0] not in left and paths[1] not in left, "the oldest go first"
    assert f"{archive.root}/dt=2026-09-25/hour=10/part-0-{TS + 6000}.parquet" in left


def test_retention_always_keeps_the_newest_file(tmp_path):
    archive = ParquetArchive(str(tmp_path / "archive"), retention_bytes=1)
    for k in range(3):
        path = archive.write(events_to_table([event(seq=k)]), TS + k * 1000)
        os.utime(path, (1_790_000_000 + k, 1_790_000_000 + k))
    assert len(archive.files()) == 1
    assert archive.vehicle_history(VIN_A).num_rows == 1


def test_no_retention_by_default(archive):
    for k in range(5):
        archive.write(events_to_table([event(seq=k)]), TS + k * 1000)
    assert archive.retention_bytes == 0
    assert len(archive.files()) == 5


def test_close_is_harmless(archive):
    archive.write(events_to_table([event()]), TS)
    archive.close()
    assert len(archive.files()) == 1


# -------------------------------------------------------------------- offsets
def test_offsets_are_stored_inside_the_file(archive):
    path = archive.write(events_to_table([event()]), TS, offsets={0: 1200, 3: 88})
    meta = pq.read_metadata(path).metadata
    assert meta[b"rosetta.offsets"] == b'{"0":1200,"3":88}'
    assert archive.last_offsets() == {0: 1200, 3: 88}


def test_offsets_do_not_change_the_data(archive):
    plain = archive.write(events_to_table([event(VIN_B), event(VIN_A)]), TS)
    marked = archive.write(events_to_table([event(VIN_B), event(VIN_A)]), TS + 1000, offsets={0: 5})
    assert pq.ParquetFile(plain).read().to_pylist() == pq.ParquetFile(marked).read().to_pylist()
    assert archive.dataset().to_table().num_rows == 4


def test_last_offsets_come_from_the_newest_file_of_this_writer(tmp_path):
    mine = ParquetArchive(str(tmp_path / "archive"), writer_id="p0")
    other = ParquetArchive(str(tmp_path / "archive"), writer_id="p1")
    mine.write(events_to_table([event()]), TS + 1000, offsets={0: 100})
    mine.write(events_to_table([event()]), TS + 3_600_000, offsets={0: 300})       # another hour, another folder
    mine.write(events_to_table([event()]), TS + 2000, offsets={0: 200})
    other.write(events_to_table([event()]), TS + 9_000_000, offsets={1: 999})
    assert mine.last_offsets() == {0: 300}
    assert other.last_offsets() == {1: 999}


def test_last_offsets_of_a_new_writer_are_empty(archive):
    assert archive.last_offsets() == {}
    archive.write(events_to_table([event()]), TS)                    # a file without positions
    assert archive.last_offsets() == {}


def test_last_offsets_survive_a_restart(tmp_path):
    ParquetArchive(str(tmp_path / "archive"), writer_id="p0").write(events_to_table([event()]), TS, offsets={2: 42})
    assert ParquetArchive(str(tmp_path / "archive"), writer_id="p0").last_offsets() == {2: 42}


def test_last_offsets_with_an_unreadable_newest_file(archive):
    archive.write(events_to_table([event()]), TS, offsets={0: 1})
    broken = Path(archive.root) / "dt=2026-09-25" / "hour=10" / f"part-w1-{TS + 5000}.parquet"
    broken.write_bytes(b"this is not parquet")
    assert archive.last_offsets() == {}


def test_empty_table_with_offsets_writes_nothing(archive):
    assert archive.write(events_to_table([]), TS, offsets={0: 10}) == ""
    assert archive.last_offsets() == {}


def test_two_writers_trim_only_their_own_files_and_keep_their_newest(tmp_path):
    """Found by the soak test: two processors trimming one directory raced on the same file."""
    import pyarrow as pa

    from rosetta.adapters.archive import SCHEMA, ParquetArchive

    row = {name: [None] for name in SCHEMA.names}
    row.update(vin=["V1"], ts=[1], seq=[1], oem=["nordvik"], map_v=[1], lat=[1.0], lon=[1.0], speed_kmh=[1.0],
               odo_km=[1.0], rx_ts=[1], norm_ts=[1], dtc=[[]])
    table = pa.table(row, schema=SCHEMA)
    a = ParquetArchive(str(tmp_path), writer_id="p0", retention_bytes=1)
    b = ParquetArchive(str(tmp_path), writer_id="p1", retention_bytes=1)
    for i in range(6):
        a.write(table, 1_790_000_000_000 + i * 1000, offsets={0: i})
        b.write(table, 1_790_000_000_500 + i * 1000, offsets={1: i})
    names = sorted(f.rsplit("/", 1)[-1] for f in a.files())
    assert [n.split("-")[1] for n in names] == ["p0", "p1"]        # one file each: its newest
    assert a.last_offsets() == {0: 5} and b.last_offsets() == {1: 5}
