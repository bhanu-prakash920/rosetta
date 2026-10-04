"""Build adapters from settings. The only module that knows which adapter is in use."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .config import Settings, get_settings
from .simulator.fleet import OEM_KEYS

T_CONTROL = "ops.control"


def make_broker(settings: Settings | None = None) -> Any:
    st = settings or get_settings()
    if st.broker == "kafka":
        from .adapters.kafka_broker import KafkaBroker

        return KafkaBroker(st.kafka_bootstrap, replication=st.kafka_replication)
    from .adapters.log_broker import LogBroker
    from .ports.broker import T_ALERTS, T_CANONICAL, T_DLQ, T_METRICS, T_RAW

    return LogBroker(st.log_dir, partitions=st.raw_partitions,
                     retention_bytes=st.log_retention_mb << 20,
                     topic_partitions={T_RAW: st.raw_partitions, T_CANONICAL: st.raw_partitions,
                                       T_DLQ: 2, T_ALERTS: 1, T_METRICS: 1, T_CONTROL: 1},
                     topic_retention_bytes={T_DLQ: st.dlq_retention_mb << 20, T_ALERTS: 64 << 20,
                                            T_METRICS: 64 << 20, T_CONTROL: 32 << 20})


def flush_broker(broker: Any) -> None:
    f = getattr(broker, "flush", None)
    if f is not None:
        f()


def load_vehicle_index() -> tuple[list[str], list[int]]:
    """VINs and ids ordered by id. Row i of the hot state belongs to vins[i]."""
    from .db.models import Vehicle
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.execute(select(Vehicle.vin, Vehicle.id).order_by(Vehicle.id)).all()
    return [r[0] for r in rows], [r[1] for r in rows]


def make_hot_state(vins: list[str], writable: bool, settings: Settings | None = None) -> Any:
    from .adapters.hot_state import MmapHotState, RedisHotState

    st = settings or get_settings()
    if st.state_store == "redis":
        return RedisHotState(st.redis_url, vins, OEM_KEYS)
    return MmapHotState(st.data_dir / "hot_state.bin", vins, OEM_KEYS, writable=writable)


def make_archive(writer_id: str = "0", settings: Settings | None = None) -> Any:
    from .adapters.archive import ParquetArchive

    st = settings or get_settings()
    if st.archive == "s3":
        return ParquetArchive.s3(st.s3_bucket, st.s3_endpoint, st.s3_access_key, st.s3_secret_key,
                                 writer_id=writer_id)
    return ParquetArchive(str(st.archive_dir), writer_id=writer_id,
                          retention_bytes=st.archive_retention_mb << 20)


def make_exact_store(settings: Settings | None = None) -> Any:
    """Exact membership store for late-event de-duplication."""
    from .engine.dedup import MemoryExactStore

    st = settings or get_settings()
    if st.state_store == "redis":
        from .adapters.redis_store import RedisExactStore

        return RedisExactStore(st.redis_url)
    return MemoryExactStore()


def make_telemetry_sink(settings: Settings | None = None) -> Any:
    """Optional time-series sink. None means the Parquet archive is the only history."""
    st = settings or get_settings()
    if st.telemetry_store == "timescale" and not st.is_sqlite:
        from .adapters.timescale import TimescaleSink

        return TimescaleSink(st.db_url)
    return None
