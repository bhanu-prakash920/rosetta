"""The production adapters against real servers, started with Testcontainers.

Marked `infra`: they need Docker. Without it they are skipped, not failed, and the
skip is visible in the report. CI runs them on every push.

    pytest -m infra tests/integration/test_infra.py
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.infra
ROOT = Path(__file__).resolve().parents[2]


def docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


if not docker_available():
    pytest.skip("Docker is not running: infrastructure tests skipped", allow_module_level=True)

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


def start(image: str, port: int, env: dict | None = None, command: str | None = None):
    """Start a container and return (container, host, mapped port).

    Readiness is checked by the caller by connecting from the host. The exec-based
    checks of the ready-made container classes hang on some Docker Desktop versions.
    """
    from testcontainers.core.container import DockerContainer

    c = DockerContainer(image).with_exposed_ports(port)
    for k, v in (env or {}).items():
        c = c.with_env(k, v)
    if command:
        c = c.with_command(command)
    c.start()
    return c, c.get_container_host_ip(), int(c.get_exposed_port(port))


def wait_for(check, seconds: float = 60.0, what: str = "service"):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            return check()
        except Exception as e:      # not ready yet
            last = e
            time.sleep(0.5)
    raise TimeoutError(f"{what} was not ready after {seconds}s: {last}")


# ------------------------------------------------------------------ PostgreSQL
@pytest.fixture(scope="module")
def postgres():
    import psycopg2

    c, host, port = start("postgres:16-alpine", 5432, {"POSTGRES_USER": "rosetta", "POSTGRES_PASSWORD": "test-only",
                                                       "POSTGRES_DB": "rosetta"})
    try:
        dsn = f"postgresql://rosetta:test-only@{host}:{port}/rosetta"
        wait_for(lambda: psycopg2.connect(dsn, connect_timeout=2).close(), what="PostgreSQL")
        yield f"postgresql+psycopg2://rosetta:test-only@{host}:{port}/rosetta"
    finally:
        c.stop()


@pytest.fixture(scope="module")
def pg_world(postgres, tmp_path_factory):
    """The relational core on PostgreSQL, seeded."""
    from rosetta import config
    from rosetta.db import session
    from tests.support import fresh_environment

    fresh_environment(tmp_path_factory.mktemp("pg"))
    os.environ["ROSETTA_DATABASE_URL"] = postgres
    config.reset_settings()
    session.reset_engine()
    from rosetta.db.session import init_db, session_scope
    from rosetta.services import agent_service, seed

    init_db()
    with session_scope() as s:
        out = seed.seed(s, vehicles=5000, log=lambda *a: None)
    with session_scope() as s:
        agent_service.seed_memory(s, vehicles=600, ticks=20, log=lambda *a: None)
    yield out
    session.reset_engine()
    os.environ.pop("ROSETTA_DATABASE_URL", None)
    config.reset_settings()


def test_schema_and_seed_on_postgres(pg_world):
    from sqlalchemy import func, select, text

    from rosetta.db.models import Base, Vehicle
    from rosetta.db.session import get_engine, session_scope

    assert get_engine().dialect.name == "postgresql"
    assert pg_world["vehicles"] == 5000
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(Vehicle)).scalar() == 5000
        tables = {r[0] for r in s.execute(text("select tablename from pg_tables where schemaname = 'public'"))}
        assert tables >= set(Base.metadata.tables)
        fks = s.execute(text("select count(*) from information_schema.table_constraints "
                             "where constraint_type = 'FOREIGN KEY'")).scalar()
        assert fks >= 25


def test_constraints_are_enforced_by_the_database(pg_world):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from rosetta.db.session import session_scope

    for sql in (
        "insert into vehicle (vin, device_id, oem_id, fleet_id, powertrain, model_year, created_at) "
        "select vin, 'dup-device', oem_id, fleet_id, powertrain, model_year, now() from vehicle limit 1",   # unique VIN
        "insert into vehicle (vin, device_id, oem_id, fleet_id, powertrain, model_year, created_at) "
        "values ('SHORT', 'd-x', 1, 1, 'ICE', 2024, now())",                                                  # VIN length
        "insert into vehicle (vin, device_id, oem_id, fleet_id, powertrain, model_year, created_at) "
        "values ('1HGCM82633A004352', 'd-y', 999999, 1, 'ICE', 2024, now())",                                 # foreign key
        "update mapping_version set canary_pct = 250",                                                        # check
        "insert into subscription (tenant_id, plan, status, vehicle_quota, price_cents, valid_from) "
        "values (1, 'x', 'active', 1, -5, now())",                                                            # check
    ):
        with pytest.raises(IntegrityError):
            with session_scope() as s:
                s.execute(text(sql))


def test_registry_life_cycle_on_postgres(pg_world):
    from rosetta.db.session import session_scope
    from rosetta.services import audit, registry
    from rosetta.simulator.dialects import SPEC_HELIX

    with session_scope() as s:
        mv = registry.create_version(s, "helix", SPEC_HELIX, source="human", actor="e@rosetta.example")
        mv, rep = registry.validate_version(s, "helix", mv.version, actor="e", label="helix")
        assert mv.state == "validated" and rep.passed == rep.total
        registry.transition(s, "helix", mv.version, "approve", actor="e")
        registry.transition(s, "helix", mv.version, "promote", actor="e")
    with session_scope() as s:
        assert [(r.oem, r.state) for r in registry.live_rows(s) if r.oem == "helix"] == [("helix", "active")]
        assert audit.verify_chain(s)["valid"]


def test_audit_chain_survives_concurrent_writers(pg_world):
    """Twelve threads append at once. The advisory lock must keep the chain linear."""
    from sqlalchemy import func, select

    from rosetta.db.models import AuditLog
    from rosetta.db.session import session_scope
    from rosetta.services import audit

    with session_scope() as s:
        before = s.execute(select(func.count()).select_from(AuditLog)).scalar()
    errors: list[Exception] = []

    def writer(k: int) -> None:
        try:
            for i in range(25):
                with session_scope() as s:
                    audit.record(s, actor_kind="user", actor=f"u{k}@rosetta.example", action="test.concurrent",
                                 resource=f"thing/{k}/{i}")
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(AuditLog)).scalar() == before + 300
        prevs = [p for (p,) in s.execute(select(AuditLog.prev_hash))]
        assert len(prevs) == len(set(prevs))              # no two entries share a parent: no fork
        res = audit.verify_chain(s)
        assert res["valid"] and res["checked"] == before + 300


def test_alert_insert_is_idempotent_on_postgres(pg_world):
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from rosetta.db.models import Alert
    from rosetta.db.session import get_engine, session_scope

    rows = [{"vehicle_id": 1, "kind": "HARSH_BRAKE", "severity": "warning", "ts": 1000 + i, "detected_ts": 2000,
             "detail": {}} for i in range(50)]
    for _ in range(3):
        with get_engine().begin() as c:
            c.execute(pg_insert(Alert).on_conflict_do_nothing(constraint="uq_alert_idempotent"), rows)
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(Alert)).scalar() == 50


def test_api_on_postgres(pg_world):
    from fastapi.testclient import TestClient

    from rosetta.api.main import create_app
    from tests.support import PASSWORD

    with TestClient(create_app()) as c:
        assert c.get("/api/v1/system/health").json()["checks"]["database"] == "ok"
        tok = c.post("/api/v1/auth/token", data={"username": "manager@northwind.example", "password": PASSWORD})
        h = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        seen, cur = [], None
        for _ in range(4):
            j = c.get("/api/v1/vehicles?limit=20" + (f"&cursor={cur}" if cur else ""), headers=h).json()
            seen += [v["id"] for v in j["items"]]
            cur = j["next_cursor"]
            if not cur:
                break
        assert seen == sorted(set(seen)) and len(seen) >= 5
        d = c.get("/api/v1/drivers?limit=1", headers=h).json()["items"][0]
        e = c.post("/api/v1/compliance/erasure", json={"driver_id": d["id"]}, headers=h).json()
        assert e["status"] == "completed" and e["evidence"]["verified"]


# ----------------------------------------------------------------------- Redis
@pytest.fixture(scope="module")
def redis_url():
    import redis

    c, host, port = start("redis:7-alpine", 6379)
    try:
        url = f"redis://{host}:{port}/0"
        wait_for(lambda: redis.Redis.from_url(url, socket_connect_timeout=2).ping(), what="Redis")
        yield url
    finally:
        c.stop()


def _cols(idx, ts, speed=40.0):
    n = len(idx)
    f = lambda v, d: np.full(n, v, dtype=d)  # noqa: E731
    return {"idx": np.asarray(idx), "ts": np.asarray(ts), "rx_ts": np.asarray(ts), "seen_ts": np.asarray(ts),
            "seq": f(1, np.int64), "lat": f(12.9, np.float64), "lon": f(77.5, np.float64),
            "speed_kmh": f(speed, np.float32), "heading_deg": f(1, np.float32), "odo_km": f(5, np.float64),
            "soc_pct": f(np.nan, np.float32), "fuel_pct": f(50, np.float32), "ambient_c": f(30, np.float32),
            "ignition": f(1, np.int8), "oem": f(2, np.int8), "evt": f(0, np.int8), "dtc_n": f(0, np.int8),
            "map_v": f(1, np.int32)}


def test_redis_hot_state_on_a_real_server(redis_url):
    from rosetta.adapters.hot_state import ROW, RedisHotState
    from rosetta.simulator.fleet import OEM_KEYS

    n = 100_000
    vins = [f"V{i:016d}" for i in range(n)]
    h = RedisHotState(redis_url, vins, OEM_KEYS)
    assert h.r.strlen(h.KEY) == n * ROW.itemsize
    idx = np.arange(0, n, 2)
    t0 = time.perf_counter()
    assert h.update_batch(_cols(idx, np.full(idx.size, 5000))) == idx.size
    write_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    snap = h.snapshot()
    read_s = time.perf_counter() - t0
    assert int((snap["ts"] > 0).sum()) == idx.size
    assert h.get(vins[2])["speed_kmh"] == 40.0 and h.get(vins[3]) is None
    # last write wins by event time, also after the writer restarts
    assert h.update_batch(_cols([2], [4000], speed=99.0)) == 0
    h2 = RedisHotState(redis_url, vins, OEM_KEYS)
    assert h2.update_batch(_cols([2], [4000], speed=99.0)) == 0 and h2.get(vins[2])["speed_kmh"] == 40.0
    assert h2.update_batch(_cols([2], [6000], speed=12.0)) == 1 and h2.get(vins[2])["speed_kmh"] == 12.0
    print(f"\\nredis hot state: wrote {idx.size:,} rows in {write_s:.2f}s, read all {n:,} in {read_s * 1000:.0f} ms")
    assert read_s < 1.0


def test_redis_exact_store_is_atomic(redis_url):
    from rosetta.adapters.redis_store import RedisExactStore

    store = RedisExactStore(redis_url, ttl_s=60)
    wins = []

    def claim():
        wins.append(store.add_if_absent("VIN123:77"))

    threads = [threading.Thread(target=claim) for _ in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1 and wins.count(False) == 29
    assert 0 < store.r.ttl("rosetta:late:VIN123:77") <= 60


# ----------------------------------------------------------------------- Kafka
KAFKA_PORT = 19092


@pytest.fixture(scope="module")
def kafka():
    """A single-node Kafka in KRaft mode (no ZooKeeper)."""
    from confluent_kafka.admin import AdminClient
    from testcontainers.core.container import DockerContainer

    c = (DockerContainer("apache/kafka:3.9.1").with_bind_ports(9092, KAFKA_PORT)
         .with_env("KAFKA_NODE_ID", "1").with_env("KAFKA_PROCESS_ROLES", "broker,controller")
         .with_env("KAFKA_LISTENERS", "PLAINTEXT://:9092,CONTROLLER://:9093")
         .with_env("KAFKA_ADVERTISED_LISTENERS", f"PLAINTEXT://localhost:{KAFKA_PORT}")
         .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
         .with_env("KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT")
         .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
         .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
         .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
         .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
         .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0"))
    c.start()
    try:
        boot = f"localhost:{KAFKA_PORT}"
        wait_for(lambda: AdminClient({"bootstrap.servers": boot}).list_topics(timeout=3), seconds=90, what="Kafka")
        yield boot
    finally:
        c.stop()


def drain(consumer, want: int, seconds: float = 30.0):
    out, deadline = [], time.time() + seconds
    while len(out) < want and time.time() < deadline:
        out += consumer.poll(5000, 1.0)
    return out


def test_kafka_topics_ordering_and_headers(kafka):
    from confluent_kafka.admin import AdminClient, NewTopic

    from rosetta.adapters.kafka_broker import DEFAULT_PARTITIONS, KafkaBroker
    from rosetta.ports.broker import T_CANONICAL, T_DLQ, T_RAW, Record

    b = KafkaBroker(kafka)
    assert b.partitions(T_RAW) == DEFAULT_PARTITIONS[T_RAW] == 32
    assert b.partitions(T_CANONICAL) == 32 and b.partitions(T_DLQ) == 6
    admin = AdminClient({"bootstrap.servers": kafka})      # keep a reference until the futures resolve
    for f in admin.create_topics([NewTopic("test.order", num_partitions=8, replication_factor=1)]).values():
        f.result(timeout=15)
    recs = [Record(key=f"dev-{i % 50}".encode(), value=f"{i}".encode(),
                   headers={"oem": "nordvik", "rx": 1_790_000_000_000 + i, "replay": 1 if i % 7 == 0 else 0})
            for i in range(5000)]
    b.produce("test.order", recs)
    assert b.flush() == 0
    got = drain(b.consumer("test.order", "g-order"), 5000)
    assert len(got) == 5000
    per_key: dict[bytes, list[int]] = {}
    for r in got:
        per_key.setdefault(r.key, []).append(int(r.value))
        assert r.headers["oem"] == "nordvik" and isinstance(r.headers["rx"], int)
    assert len(per_key) == 50 and all(v == sorted(v) for v in per_key.values())       # order per device
    assert len({r.partition for r in got}) > 1                                        # and spread over partitions


def test_kafka_consumer_group_resumes_after_commit(kafka):
    from rosetta.adapters.kafka_broker import KafkaBroker
    from rosetta.ports.broker import Record

    b = KafkaBroker(kafka)
    b.produce("test.resume", [Record(key=b"k", value=str(i).encode()) for i in range(300)])
    b.flush()
    c1 = b.consumer("test.resume", "g-resume")
    first = drain(c1, 100, 20)[:100] if False else c1.poll(100, 10.0)
    assert 0 < len(first) <= 100
    c1.commit()
    c1.close()                                     # the process goes away
    c2 = b.consumer("test.resume", "g-resume")     # its replacement joins the same group
    rest = drain(c2, 300 - len(first))
    values = [int(r.value) for r in first + rest]
    assert values == list(range(300))              # nothing skipped, nothing repeated
    c3 = b.consumer("test.resume", "g-replay", start="beginning")
    assert len(drain(c3, 300)) == 300              # another group can replay everything


def test_pipeline_over_kafka_including_onboarding(kafka, tmp_path_factory):
    """The same workers, the same code, Kafka instead of the local log."""

    from rosetta import config
    from rosetta.db import session
    from tests.support import ALL_SOURCES, fresh_environment

    fresh_environment(tmp_path_factory.mktemp("kafka"))
    os.environ.update({"ROSETTA_BROKER": "kafka", "ROSETTA_KAFKA_BOOTSTRAP": kafka})
    config.reset_settings()
    session.reset_engine()
    try:
        from rosetta.db.session import init_db, session_scope
        from rosetta.factory import flush_broker, load_vehicle_index, make_broker, make_hot_state
        from rosetta.pipeline.dlq import DlqWorker, queue_replay
        from rosetta.pipeline.normalizer_worker import NormalizerWorker
        from rosetta.pipeline.processor import Processor
        from rosetta.services import registry, seed
        from rosetta.simulator.dialects import SPEC_HELIX
        from rosetta.simulator.fleet import Fleet
        from rosetta.simulator.run import SimSettings, SimulatorRunner

        init_db()
        with session_scope() as s:
            seed.seed(s, vehicles=3000, log=lambda *a: None)
        vins, _ = load_vehicle_index()
        make_hot_state(vins, writable=True).close()
        broker = make_broker()
        assert type(broker).__name__ == "KafkaBroker"
        st = SimSettings(mode="all", enabled=list(ALL_SOURCES))
        st.dup_pct = st.reorder_pct = st.malformed_pct = 0.0
        sim = SimulatorRunner(fleet=Fleet(3000, seed=7), broker=broker, listen_control=False, settings=st)
        norm, proc, dlq = NormalizerWorker(broker=broker), Processor(broker=broker), DlqWorker(broker=broker)

        def pump(seconds=25.0, until=lambda: False):
            quiet, deadline = 0, time.time() + seconds
            while time.time() < deadline and quiet < 4 and not until():
                dlq.run_queued_jobs()
                k = norm.step(0.3) + dlq.step(0.3)
                flush_broker(broker)
                k += proc.step(0.3)
                quiet = 0 if k else quiet + 1
            proc.flush_archive()
            proc.consumer.commit()

        sent = 0
        for k in range(4):
            sent += sim.tick(now_ms=1_790_300_000_000 + k * 1000, dt=1.0)
        flush_broker(broker)
        pump(40, until=lambda: norm.processed >= sent)
        assert norm.processed == sent == 12_000
        helix = sum(1 for o in sim.fleet.oem if o == 5) * 4
        t = proc.archive.dataset().to_table(columns=["vin", "oem"])
        assert t.num_rows == sent - helix
        assert "helix" not in set(t["oem"].to_pylist())

        with session_scope() as s:
            mv = registry.create_version(s, "helix", SPEC_HELIX, source="human", actor="e@rosetta.example")
            registry.validate_version(s, "helix", mv.version, actor="e", label="helix")
            registry.transition(s, "helix", mv.version, "approve", actor="e")
            registry.transition(s, "helix", mv.version, "promote", actor="e")
            queue_replay(s, "helix", trigger="test")
        assert norm.maybe_reload(force=True)
        pump(40, until=lambda: norm.processed >= sent + helix)
        pump(8)
        t = proc.archive.dataset().to_table(columns=["vin", "seq", "oem", "replayed"])
        assert t.num_rows == sent                                     # every parked message came back
        back = [r for r in t.to_pylist() if r["oem"] == "helix"]
        assert len(back) == helix and all(r["replayed"] for r in back)
        assert len({(r["vin"], r["seq"]) for r in t.to_pylist()}) == sent
        hot = proc.hot.get(vins[0])
        assert hot is not None and hot["events"] == 4
        norm.consumer.close(), proc.consumer.close(), dlq.consumer.close()
    finally:
        session.reset_engine()
        for k in ("ROSETTA_BROKER", "ROSETTA_KAFKA_BOOTSTRAP"):
            os.environ.pop(k, None)
        config.reset_settings()


# -------------------------------------------------------------------- pgvector
@pytest.fixture(scope="module")
def pgvector_url():
    import psycopg2

    c, host, port = start("pgvector/pgvector:pg16", 5432, {"POSTGRES_USER": "rosetta", "POSTGRES_PASSWORD": "test-only",
                                                           "POSTGRES_DB": "rosetta"})
    try:
        dsn = f"postgresql://rosetta:test-only@{host}:{port}/rosetta"
        wait_for(lambda: psycopg2.connect(dsn, connect_timeout=2).close(), what="PostgreSQL with pgvector")
        yield f"postgresql+psycopg2://rosetta:test-only@{host}:{port}/rosetta"
    finally:
        c.stop()


def test_mapping_memory_on_pgvector(pgvector_url):
    """The vector search runs inside PostgreSQL with an HNSW index, and returns the same
    neighbours as the exact scan."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from rosetta.agent import memory
    from rosetta.db.models import Base

    eng = create_engine(pgvector_url)
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        c.execute(text(f"ALTER TABLE embedding ADD COLUMN IF NOT EXISTS vec vector({memory.DIM})"))
        c.execute(text("CREATE INDEX IF NOT EXISTS embedding_vec_hnsw ON embedding USING hnsw (vec vector_cosine_ops)"))
    memory._has_vec.clear()
    rng = np.random.default_rng(3)
    vecs = rng.normal(size=(400, memory.DIM)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    with Session(eng) as s:
        mem = memory.make_memory(s)
        assert isinstance(mem, memory.PgVectorMemory)
        for i, v in enumerate(vecs):
            mem.add(f"oem{i % 8}", 1 + i // 100, f"field_{i}", "speed|mph", f"f{i}", v)
        s.commit()
        assert mem.count() == 400
        exact = memory.NumpyMemory(s)
        agree = 0
        for q in vecs[:40] + rng.normal(scale=0.05, size=(40, memory.DIM)).astype(np.float32):
            q = q / np.linalg.norm(q)
            a = [n.ref for n in mem.search(q, k=3)]
            b = [n.ref for n in exact.search(q, k=3)]
            agree += a[0] == b[0]
            assert abs(mem.search(q, k=1)[0].similarity - exact.search(q, k=1)[0].similarity) < 1e-4 or a[0] != b[0]
        assert agree >= 38                                   # HNSW is approximate: allow a rare miss
        only = mem.search(vecs[5], k=3, allowed={("oem5", 1)})
        assert only and all(n.oem == "oem5" and n.version == 1 for n in only)
        # With 400 rows the planner rightly prefers a scan. Forbid scans to prove the
        # HNSW index can serve the query at all; at production size the planner picks it.
        s.execute(text("SET LOCAL enable_seqscan = off"))
        s.execute(text("SET LOCAL enable_bitmapscan = off"))
        plan = "\\n".join(r[0] for r in s.execute(text(
            "EXPLAIN SELECT ref FROM embedding ORDER BY vec <=> CAST(:v AS vector) LIMIT 3"),
            {"v": "[" + ",".join("0.1" for _ in range(memory.DIM)) + "]"}))
        assert "embedding_vec_hnsw" in plan, plan
    memory._has_vec.clear()


# ------------------------------------------------------------------ TimescaleDB
TIMESCALE_IMAGE = "timescale/timescaledb-ha:pg16.15-ts2.30.1"     # the image of docker-compose.yml


@pytest.fixture(scope="module")
def timescale_url():
    """TimescaleDB with the relational schema and infra/sql/postgres_extras.sql applied,
    by psql inside the container, exactly as the db-extras job of Compose does."""
    import psycopg2
    from sqlalchemy import create_engine
    from testcontainers.core.container import DockerContainer

    from rosetta.db.models import Base

    c = (DockerContainer(TIMESCALE_IMAGE).with_exposed_ports(5432)
         .with_env("POSTGRES_USER", "rosetta").with_env("POSTGRES_PASSWORD", "test-only")
         .with_env("POSTGRES_DB", "rosetta")
         .with_volume_mapping(str(ROOT / "infra/sql/postgres_extras.sql"), "/extras.sql", "ro"))
    c.start()
    try:
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(5432))
        dsn = f"postgresql://rosetta:test-only@{host}:{port}/rosetta"
        wait_for(lambda: psycopg2.connect(dsn, connect_timeout=2).close(), 120, what="TimescaleDB")
        url = f"postgresql+psycopg2://rosetta:test-only@{host}:{port}/rosetta"
        Base.metadata.create_all(create_engine(url))
        # The docker CLI, not the SDK's exec: the SDK's exec hangs on some Docker
        # Desktop versions (see start()).
        cid = c.get_wrapped_container().id
        for _ in range(2):                                   # the script must be idempotent
            r = subprocess.run(["docker", "exec", cid, "psql", "-U", "rosetta", "-d", "rosetta", "-v",
                                "ON_ERROR_STOP=1", "-q", "-f", "/extras.sql"],
                               capture_output=True, text=True, timeout=180)
            assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
        yield url
    finally:
        c.stop()


def test_telemetry_sink_writes_a_hypertable_idempotently(timescale_url):
    """The processor's TimescaleDB sink: COPY into the hypertable, a replayed batch
    adds nothing, and a failed batch does not poison the connection."""
    import pyarrow as pa
    import pytest as _pytest

    from rosetta.adapters.timescale import TimescaleSink

    now = int(time.time() * 1000)
    rows = {
        "ts": pa.array([now - 2000, now - 1000], pa.int64()), "vin": ["1HGCM82633A004352"] * 2,
        "seq": pa.array([7, 8], pa.int64()), "oem": ["nordvik"] * 2, "map_v": pa.array([1, 1], pa.int32()),
        "lat": [21.1702, 21.1703], "lon": [72.8311, 72.8312], "speed_kmh": [64.2, 12.5],
        "heading_deg": [90.0, 91.0], "odo_km": [18234.7, 18234.8], "soc_pct": [41.0, None],
        "fuel_pct": [None, None], "ambient_c": [30.5, 30.5], "ignition": [True, True],
        "evt": ["HARSH_BRAKE", None], "dtc": pa.array([["P0301"], []], pa.list_(pa.string())),
        "rx_ts": pa.array([now - 1990, now - 990], pa.int64()), "geohash5": ["tsj2t", "tsj2t"],
    }
    table = pa.table(rows)
    sink = TimescaleSink(timescale_url)
    try:
        assert sink.write(table) == 2
        assert sink.write(table) == 0                         # replay: ON CONFLICT DO NOTHING
        with _pytest.raises(Exception):
            sink.write(table.set_column(1, "vin", pa.array([None, None], pa.string())))  # NOT NULL violation
        more = (table.set_column(2, "seq", pa.array([9, 10], pa.int64()))
                .set_column(0, "ts", pa.array([now - 500, now], pa.int64())))
        assert sink.write(more) == 2                          # the connection recovered
        hist = sink.history("1HGCM82633A004352")
        assert [h["seq"] for h in hist] == [10, 9, 8, 7]
        assert hist[-1]["dtc"] == ["P0301"] and hist[-1]["evt"] == "HARSH_BRAKE"
        assert abs(hist[-1]["ts"] - (now - 2000)) <= 1        # millisecond precision survives the CSV
    finally:
        sink.close()
