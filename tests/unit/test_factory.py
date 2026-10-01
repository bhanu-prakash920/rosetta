"""rosetta.factory: the one place that picks an adapter for each port."""
from __future__ import annotations

import pytest

from rosetta import config, factory
from rosetta.adapters.archive import ParquetArchive
from rosetta.adapters.hot_state import MmapHotState
from rosetta.adapters.log_broker import LogBroker
from rosetta.engine.dedup import MemoryExactStore
from rosetta.ports.broker import T_ALERTS, T_CANONICAL, T_DLQ, T_METRICS, T_RAW
from rosetta.simulator.fleet import OEM_KEYS


@pytest.fixture()
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ROSETTA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROSETTA_RAW_PARTITIONS", "3")
    monkeypatch.setenv("ROSETTA_LOG_RETENTION_MB", "10")
    monkeypatch.setenv("ROSETTA_DLQ_RETENTION_MB", "20")
    monkeypatch.setenv("ROSETTA_ARCHIVE_RETENTION_MB", "5")
    config.reset_settings()
    return config.get_settings()


def test_local_broker_is_the_file_log_with_the_topic_layout(settings):
    b = factory.make_broker(settings)
    assert isinstance(b, LogBroker)
    assert b.root == settings.log_dir
    assert (b.partitions(T_RAW), b.partitions(T_CANONICAL)) == (3, 3)
    assert (b.partitions(T_DLQ), b.partitions(T_ALERTS), b.partitions(T_METRICS), b.partitions(factory.T_CONTROL)) == \
        (2, 1, 1, 1)
    assert b.retention_bytes == 10 << 20
    assert b.topic_retention_bytes[T_DLQ] == 20 << 20, "dead letters outlive the raw log"


def test_broker_is_built_from_the_cached_settings_by_default(settings):
    assert isinstance(factory.make_broker(), LogBroker)


def test_flush_broker_calls_flush_when_there_is_one():
    calls = []
    factory.flush_broker(type("B", (), {"flush": lambda self: calls.append(1)})())
    factory.flush_broker(object())
    assert calls == [1]


def test_local_hot_state_is_memory_mapped(settings, tmp_path):
    vins = ["1HGCM82633A004352", "11111111111111111"]
    hs = factory.make_hot_state(vins, writable=True, settings=settings)
    try:
        assert isinstance(hs, MmapHotState)
        assert hs.path == settings.data_dir / "hot_state.bin"
        assert hs.oem_keys == OEM_KEYS and hs.vins == vins
    finally:
        hs.close()


def test_local_archive_is_parquet_on_disk(settings):
    a = factory.make_archive("w7", settings=settings)
    assert isinstance(a, ParquetArchive)
    assert a.root == str(settings.archive_dir) and a.writer_id == "w7"
    assert a.retention_bytes == 5 << 20


def test_local_exact_store_is_in_memory(settings):
    assert isinstance(factory.make_exact_store(settings), MemoryExactStore)


def test_redis_exact_store_when_configured(monkeypatch, settings):
    monkeypatch.setenv("ROSETTA_STATE_STORE", "redis")
    monkeypatch.setenv("ROSETTA_REDIS_URL", "redis://cache.internal:6380/2")
    config.reset_settings()
    from rosetta.adapters.redis_store import RedisExactStore

    store = factory.make_exact_store(config.get_settings())
    assert isinstance(store, RedisExactStore)
    kwargs = store.r.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("cache.internal", 6380, 2)


def test_no_time_series_sink_on_sqlite(settings):
    assert factory.make_telemetry_sink(settings) is None


def test_no_time_series_sink_unless_asked_for(monkeypatch, settings):
    monkeypatch.setenv("ROSETTA_DATABASE_URL", "postgresql://db/rosetta")
    monkeypatch.setenv("ROSETTA_TELEMETRY_STORE", "parquet")
    config.reset_settings()
    assert factory.make_telemetry_sink(config.get_settings()) is None
