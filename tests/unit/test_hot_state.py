"""rosetta.adapters.hot_state: latest state per vehicle, memory-mapped file or Redis."""
from __future__ import annotations

import math
from typing import Any

import fakeredis
import numpy as np
import pytest

from rosetta.adapters.hot_state import EVT_INDEX, EVT_NAME, ROW, MmapHotState, RedisHotState, row_to_dict
from rosetta.domain.canonical import EVENT_TYPES

VINS = ["1HGCM82633A004352", "11111111111111111", "7NVEV1A20TA000000", "5PAGT4B70TA000001"]
OEMS = ("nordvik", "pacifica", "stellaris")
T0 = 1_790_000_000_000


def batch(idx: list[int], ts: list[int], **cols: Any) -> dict[str, np.ndarray]:
    """Columns for update_batch. Anything not given gets a plain default."""
    n = len(idx)
    defaults: dict[str, Any] = {
        "rx_ts": [t + 20 for t in ts], "seen_ts": [t + 50 for t in ts], "seq": list(range(1, n + 1)),
        "lat": [12.9716] * n, "lon": [77.5946] * n, "speed_kmh": [54.25] * n, "heading_deg": [181.0] * n,
        "odo_km": [18234.5] * n, "soc_pct": [76.5] * n, "fuel_pct": [math.nan] * n, "ambient_c": [27.5] * n,
        "ignition": [1] * n, "oem": [0] * n, "evt": [0] * n, "dtc_n": [0] * n, "map_v": [1] * n,
    }
    defaults.update(cols)
    out = {"idx": np.array(idx, dtype=np.int64), "ts": np.array(ts, dtype=np.int64)}
    for name, values in defaults.items():
        out[name] = np.array(values).astype(ROW[name])
    return out


@pytest.fixture(params=["mmap", "redis"])
def store(request, tmp_path):
    if request.param == "mmap":
        s = MmapHotState(tmp_path / "hot" / "state.bin", VINS, OEMS, writable=True)
    else:
        s = RedisHotState("redis://unused", VINS, OEMS, client=fakeredis.FakeRedis())
    yield s
    s.close()


# ------------------------------------------------------------- both adapters
def test_vehicle_that_never_reported_has_no_state(store):
    assert store.get(VINS[0]) is None


def test_unknown_vin_has_no_state(store):
    store.update_batch(batch([0], [T0]))
    assert store.get("WHX00000000000000") is None


def test_update_then_get(store):
    n = store.update_batch(batch([1], [T0], seq=[42], oem=[1], evt=[EVT_INDEX["SPEEDING"]], dtc_n=[2], map_v=[3]))
    assert n == 1
    assert store.get(VINS[1]) == {
        "vin": VINS[1], "ts": T0, "rx_ts": T0 + 20, "seen_ts": T0 + 50, "seq": 42,
        "lat": 12.9716, "lon": 77.5946, "speed_kmh": 54.25, "heading_deg": 181.0, "odo_km": 18234.5,
        "soc_pct": 76.5, "fuel_pct": None, "ambient_c": 27.5, "ignition": True, "oem": "pacifica",
        "evt": "SPEEDING", "dtc_count": 2, "map_v": 3, "events": 1,
    }
    assert store.get(VINS[0]) is None, "other vehicles are untouched"


def test_empty_batch(store):
    assert store.update_batch(batch([], [])) == 0


def test_missing_measurements_come_back_as_none(store):
    store.update_batch(batch([0], [T0], soc_pct=[math.nan], fuel_pct=[math.nan], heading_deg=[math.nan]))
    state = store.get(VINS[0])
    assert state["soc_pct"] is None and state["fuel_pct"] is None and state["heading_deg"] is None
    assert state["speed_kmh"] == 54.25


def test_no_event_and_ignition_off(store):
    store.update_batch(batch([0], [T0], evt=[0], ignition=[0]))
    state = store.get(VINS[0])
    assert state["evt"] is None and state["ignition"] is False


def test_newest_event_of_a_batch_wins(store):
    n = store.update_batch(batch([2, 2, 2], [T0 + 2000, T0 + 3000, T0 + 1000], seq=[2, 3, 1],
                                 speed_kmh=[20.0, 30.0, 10.0]))
    assert n == 1
    state = store.get(VINS[2])
    assert (state["ts"], state["seq"], state["speed_kmh"], state["events"]) == (T0 + 3000, 3, 30.0, 3)


def test_newer_event_replaces_the_state(store):
    store.update_batch(batch([0], [T0], seq=[1], odo_km=[100.0]))
    assert store.update_batch(batch([0], [T0 + 1000], seq=[2], odo_km=[100.2])) == 1
    state = store.get(VINS[0])
    assert (state["seq"], state["odo_km"], state["events"]) == (2, 100.2, 2)


def test_older_event_never_overwrites_a_newer_state(store):
    store.update_batch(batch([0], [T0 + 5000], seq=[9], speed_kmh=[90.0]))
    assert store.update_batch(batch([0], [T0], seq=[1], speed_kmh=[10.0])) == 0
    state = store.get(VINS[0])
    assert (state["ts"], state["seq"], state["speed_kmh"]) == (T0 + 5000, 9, 90.0)


def test_replaying_the_same_event_is_idempotent(store):
    store.update_batch(batch([0], [T0], seq=[5]))
    before = {k: v for k, v in store.get(VINS[0]).items() if k != "events"}
    store.update_batch(batch([0], [T0], seq=[5]))
    assert {k: v for k, v in store.get(VINS[0]).items() if k != "events"} == before


def test_batch_with_several_vehicles(store):
    n = store.update_batch(batch([3, 0, 3, 1], [T0, T0, T0 + 1000, T0], seq=[1, 7, 2, 4], oem=[2, 0, 2, 1]))
    assert n == 3
    assert [store.get(v)["seq"] for v in (VINS[0], VINS[1], VINS[3])] == [7, 4, 2]
    assert [store.get(v)["oem"] for v in (VINS[0], VINS[1], VINS[3])] == ["nordvik", "pacifica", "stellaris"]
    assert [store.get(v)["events"] for v in (VINS[0], VINS[1], VINS[3])] == [1, 1, 2]
    assert store.get(VINS[2]) is None


def test_every_event_is_counted(store):
    for k in range(5):
        store.update_batch(batch([0, 0], [T0 + k * 1000, T0 + k * 1000 + 500]))
    assert store.get(VINS[0])["events"] == 10


def test_unknown_source_index_is_reported_as_none(store):
    store.update_batch(batch([0], [T0], oem=[7]))
    assert store.get(VINS[0])["oem"] is None


def test_values_are_rounded_for_the_api(store):
    store.update_batch(batch([0], [T0], lat=[12.97160049], lon=[77.59460051], odo_km=[18234.56789],
                             speed_kmh=[54.123456]))
    state = store.get(VINS[0])
    assert state["lat"] == 12.971600 and state["lon"] == 77.594601
    assert state["odo_km"] == 18234.568
    assert state["speed_kmh"] == pytest.approx(54.123, abs=1e-3)


def test_snapshot_has_one_row_per_vehicle_and_every_column(store):
    store.update_batch(batch([1, 3], [T0, T0 + 1], seq=[5, 6]))
    snap = store.snapshot()
    assert set(snap) == set(ROW.names)
    assert all(len(col) == len(VINS) for col in snap.values())
    assert snap["ts"].tolist() == [0, T0, 0, T0 + 1]
    assert snap["seq"].tolist() == [0, 5, 0, 6]
    assert snap["events"].tolist() == [0, 1, 0, 1]
    assert snap["oem"].tolist() == [-1, 0, -1, 0], "vehicles that never reported have no source"
    assert np.isnan(snap["soc_pct"][0]) and snap["soc_pct"][1] == 76.5


def test_snapshot_is_a_copy(store):
    store.update_batch(batch([0], [T0]))
    snap = store.snapshot()
    store.update_batch(batch([0], [T0 + 1000], seq=[99]))
    assert snap["ts"][0] == T0


def test_out_of_order_events_are_counted_by_both_adapters(tmp_path):
    mmap = MmapHotState(tmp_path / "state.bin", VINS, OEMS, writable=True)
    redis = RedisHotState("redis://unused", VINS, OEMS, client=fakeredis.FakeRedis())
    for s in (mmap, redis):
        s.update_batch(batch([0], [T0 + 5000]))
        s.update_batch(batch([0], [T0]))                 # arrives late
    assert mmap.get(VINS[0])["events"] == 2
    assert redis.get(VINS[0])["events"] == mmap.get(VINS[0])["events"]


# ------------------------------------------------------------------ mmap only
def test_mmap_file_has_one_fixed_size_row_per_vehicle(tmp_path):
    path = tmp_path / "deep" / "dir" / "state.bin"
    MmapHotState(path, VINS, OEMS, writable=True).close()
    assert path.stat().st_size == len(VINS) * ROW.itemsize


def test_mmap_reader_sees_what_the_writer_wrote(tmp_path):
    path = tmp_path / "state.bin"
    writer = MmapHotState(path, VINS, OEMS, writable=True)
    reader = MmapHotState(path, VINS, OEMS, writable=False)
    assert reader.get(VINS[0]) is None
    writer.update_batch(batch([0], [T0], seq=[11]))
    writer.flush()
    assert reader.get(VINS[0])["seq"] == 11


def test_mmap_reader_cannot_write(tmp_path):
    path = tmp_path / "state.bin"
    MmapHotState(path, VINS, OEMS, writable=True).close()
    reader = MmapHotState(path, VINS, OEMS, writable=False)
    with pytest.raises(ValueError):
        reader.update_batch(batch([0], [T0]))


def test_mmap_reader_may_start_before_the_writer(tmp_path):
    path = tmp_path / "state.bin"
    reader = MmapHotState(path, VINS, OEMS, writable=False)
    assert path.stat().st_size == len(VINS) * ROW.itemsize
    assert reader.get(VINS[0]) is None
    writer = MmapHotState(path, VINS, OEMS, writable=True)
    writer.update_batch(batch([2], [T0], seq=[3]))
    writer.flush()
    assert reader.get(VINS[2])["seq"] == 3


def test_mmap_state_survives_a_restart(tmp_path):
    path = tmp_path / "state.bin"
    first = MmapHotState(path, VINS, OEMS, writable=True)
    first.update_batch(batch([1], [T0 + 9000], seq=[77]))
    first.close()

    second = MmapHotState(path, VINS, OEMS, writable=True)
    assert second.get(VINS[1])["seq"] == 77
    assert second.update_batch(batch([1], [T0], seq=[1])) == 0, "last-write-wins holds across restarts"
    assert second.get(VINS[1])["events"] == 2


def test_mmap_file_of_the_wrong_size_is_replaced(tmp_path):
    path = tmp_path / "state.bin"
    first = MmapHotState(path, VINS, OEMS, writable=True)
    first.update_batch(batch([0], [T0]))
    first.close()

    grown = MmapHotState(path, VINS + ["8VTEV1A20TA000009"], OEMS, writable=True)
    assert path.stat().st_size == 5 * ROW.itemsize
    assert grown.get(VINS[0]) is None, "the fleet changed, so the old rows no longer line up"


def test_mmap_fresh_rows_are_blank(tmp_path):
    s = MmapHotState(tmp_path / "state.bin", VINS, OEMS, writable=True)
    assert (s.rows["ts"] == 0).all() and (s.rows["oem"] == -1).all() and (s.rows["events"] == 0).all()
    for name in ("speed_kmh", "heading_deg", "soc_pct", "fuel_pct", "ambient_c"):
        assert np.isnan(s.rows[name]).all()


def test_mmap_with_an_empty_fleet(tmp_path):
    s = MmapHotState(tmp_path / "state.bin", [], OEMS, writable=True)
    assert s.get(VINS[0]) is None
    assert s.snapshot()["ts"].size == 0
    assert s.update_batch(batch([], [])) == 0


# ----------------------------------------------------------------- redis only
def test_redis_keeps_the_whole_fleet_in_one_value():
    client = fakeredis.FakeRedis()
    RedisHotState("redis://unused", VINS, OEMS, client=client)
    assert client.strlen(RedisHotState.KEY) == len(VINS) * ROW.itemsize
    assert client.keys("*") == [RedisHotState.KEY.encode()]


def test_redis_state_is_shared_between_instances():
    client = fakeredis.FakeRedis()
    writer = RedisHotState("redis://unused", VINS, OEMS, client=client)
    reader = RedisHotState("redis://unused", VINS, OEMS, client=client)
    writer.update_batch(batch([3], [T0], seq=[8]))
    assert reader.get(VINS[3])["seq"] == 8
    assert reader.snapshot()["seq"].tolist() == [0, 0, 0, 8]


def test_redis_writer_restart_keeps_last_write_wins_and_the_counter():
    client = fakeredis.FakeRedis()
    first = RedisHotState("redis://unused", VINS, OEMS, client=client)
    first.update_batch(batch([0, 0], [T0 + 8000, T0 + 9000], seq=[1, 2]))

    second = RedisHotState("redis://unused", VINS, OEMS, client=client)
    assert second.update_batch(batch([0], [T0], seq=[99])) == 0
    assert second.get(VINS[0])["seq"] == 2
    second.update_batch(batch([0], [T0 + 10_000], seq=[3]))
    assert second.get(VINS[0])["seq"] == 3
    assert second.get(VINS[0])["events"] >= 3


def test_redis_value_of_the_wrong_size_is_replaced():
    client = fakeredis.FakeRedis()
    client.set(RedisHotState.KEY, b"left over from another fleet")
    s = RedisHotState("redis://unused", VINS, OEMS, client=client)
    assert client.strlen(RedisHotState.KEY) == len(VINS) * ROW.itemsize
    assert s.get(VINS[0]) is None


def test_redis_get_survives_a_lost_key():
    client = fakeredis.FakeRedis()
    s = RedisHotState("redis://unused", VINS, OEMS, client=client)
    s.update_batch(batch([0], [T0]))
    client.delete(RedisHotState.KEY)
    assert s.get(VINS[0]) is None
    assert s.snapshot()["ts"].tolist() == [0, 0, 0, 0]


def test_redis_close_never_raises():
    class Broken:
        def strlen(self, _key):
            return 0

        def set(self, *_a):
            return True

        def close(self):
            raise RuntimeError("connection already gone")

    RedisHotState("redis://unused", VINS, OEMS, client=Broken()).close()
    RedisHotState("redis://unused", VINS, OEMS, client=fakeredis.FakeRedis()).flush()


# -------------------------------------------------------------------- helpers
def test_event_index_and_name_are_inverse():
    assert set(EVT_INDEX) == set(EVENT_TYPES)
    assert all(EVT_NAME[i] == name for name, i in EVT_INDEX.items())
    assert 0 not in EVT_NAME, "0 means no event"


def test_row_to_dict_on_a_plain_row():
    row = np.zeros(1, dtype=ROW)[0]
    row["ts"], row["oem"], row["evt"], row["soc_pct"] = T0, 2, 1, np.nan
    d = row_to_dict(row, VINS[0], OEMS)
    assert (d["vin"], d["ts"], d["oem"], d["evt"], d["soc_pct"]) == (VINS[0], T0, "stellaris", EVENT_TYPES[0], None)
    assert all(not isinstance(v, np.generic) for v in d.values()), "only plain Python values reach the API"
