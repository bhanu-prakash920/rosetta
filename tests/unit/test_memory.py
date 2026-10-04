"""rosetta.agent.memory: a vector store of fields the platform has already learned."""
from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from rosetta.agent import memory as M
from rosetta.agent.memory import DIM, Neighbour, NumpyMemory, embed, make_memory
from rosetta.db.models import Base, Embedding
from rosetta.ml.profile import N_DENSE, NAME_DIM, FieldProfile


def profile(path: str, **features: float) -> FieldProfile:
    from rosetta.ml.profile import FEATURE_NAMES

    f = np.zeros(N_DENSE, dtype=np.float32)
    for name, value in features.items():
        f[FEATURE_NAMES.index(name)] = value
    return FieldProfile(path, [], features=f)


SPEED_MPH = dict(frac_num=1.0, in_0_100=0.9, speed_ratio_log=-0.052, speed_ratio_fit=0.99)
ODOMETER = dict(frac_num=1.0, mono_up=1.0, log_abs_median=0.2, dist_ratio_log=-0.05, dist_ratio_fit=0.98)
VIN = dict(frac_str=1.0, frac_vin=1.0, str_len=0.425)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Embedding.__table__])
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture()
def memory(session) -> NumpyMemory:
    mem = NumpyMemory(session)
    mem.add("pacifica", 1, "speed_mph", "speed|mph", "speed_kmh", embed(profile("speed_mph", **SPEED_MPH)))
    mem.add("pacifica", 1, "odometer_mi", "odo|mi", "odo_km", embed(profile("odometer_mi", **ODOMETER)))
    mem.add("pacifica", 1, "VIN", "vin", "vin", embed(profile("VIN", **VIN)))
    mem.add("nordvik", 2, "motion.speedKmh", "speed|kmh", "speed_kmh",
            embed(profile("motion.speedKmh", frac_num=1.0, in_0_100=0.9, speed_ratio_log=0.0, speed_ratio_fit=0.99)))
    return mem


# ---------------------------------------------------------------------- embed
def test_embedding_is_a_unit_vector_of_the_model_dimension():
    v = embed(profile("speed_mph", **SPEED_MPH))
    assert v.shape == (DIM,) == (N_DENSE + NAME_DIM,)
    assert v.dtype == np.float32
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-5)


def test_name_and_values_are_weighted():
    v = embed(profile("speed_mph", **SPEED_MPH))
    dense, name = v[:N_DENSE], v[N_DENSE:]
    assert float(np.linalg.norm(name)) / float(np.linalg.norm(dense)) == pytest.approx(M.NAME_WEIGHT / (1 - M.NAME_WEIGHT),
                                                                                       rel=1e-4)


def test_embedding_without_value_features_is_the_name_alone():
    v = embed(profile("speed_mph"))
    assert not v[:N_DENSE].any()
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-5)


def test_embedding_of_nothing_is_the_zero_vector():
    assert not embed(profile("")).any()


def test_nan_features_do_not_poison_the_embedding():
    p = profile("x", frac_num=1.0)
    p.features[3] = np.nan
    assert np.isfinite(embed(p)).all()


def test_same_field_gives_the_same_embedding():
    assert np.array_equal(embed(profile("speed_mph", **SPEED_MPH)), embed(profile("speed_mph", **SPEED_MPH)))


def test_renamed_field_is_still_close_to_the_original():
    original = embed(profile("speed_mph", **SPEED_MPH))
    renamed = embed(profile("geschwindigkeit", **SPEED_MPH))
    other = embed(profile("geschwindigkeit", **VIN))
    assert float(original @ renamed) > 0.3
    assert float(original @ renamed) > float(original @ other) + 0.2


# --------------------------------------------------------------------- memory
def test_empty_memory(session):
    mem = NumpyMemory(session)
    assert mem.count() == 0
    assert mem.search(embed(profile("speed_mph", **SPEED_MPH))) == []


def test_count(memory):
    assert memory.count() == 4


def test_exact_match_is_found_first_with_similarity_one(memory):
    hits = memory.search(embed(profile("speed_mph", **SPEED_MPH)), k=3)
    assert len(hits) == 3
    best = hits[0]
    assert isinstance(best, Neighbour)
    assert (best.ref, best.oem, best.version, best.path, best.label, best.canonical) == \
        ("pacifica/v1/speed_kmh", "pacifica", 1, "speed_mph", "speed|mph", "speed_kmh")
    assert best.similarity == pytest.approx(1.0, abs=1e-4)
    assert [h.similarity for h in hits] == sorted((h.similarity for h in hits), reverse=True)


def test_renamed_field_is_found_by_how_it_behaves(memory):
    hits = memory.search(embed(profile("kilometerstand", **ODOMETER)), k=4)
    assert hits[0].path == "odometer_mi" and hits[0].label == "odo|mi"
    assert 0.2 < hits[0].similarity < 1.0
    assert hits[0].similarity > hits[1].similarity + 0.05


def test_field_with_a_known_name_is_found_by_its_name(memory):
    hits = memory.search(embed(profile("vin")), k=1)
    assert hits[0].path == "VIN"


def test_k_limits_the_number_of_neighbours(memory):
    q = embed(profile("speed_mph", **SPEED_MPH))
    assert len(memory.search(q, k=1)) == 1
    assert len(memory.search(q, k=10)) == 4
    assert len(memory.search(q)) == 3, "three by default"


def test_allowed_restricts_the_search_to_some_mapping_versions(memory):
    q = embed(profile("speed_mph", **SPEED_MPH))
    hits = memory.search(q, k=5, allowed={("nordvik", 2)})
    assert [(h.oem, h.version) for h in hits] == [("nordvik", 2)]
    assert memory.search(q, allowed={("helix", 1)}) == []
    assert memory.search(q, allowed=set()) == []
    assert len(memory.search(q, k=5, allowed={("nordvik", 2), ("pacifica", 1)})) == 4


def test_adding_the_same_field_again_replaces_it(memory, session):
    memory.add("pacifica", 1, "speed", "speed|kmh", "speed_kmh", embed(profile("speed", **SPEED_MPH)))
    assert memory.count() == 4
    rows = session.query(Embedding).filter_by(ref="pacifica/v1/speed_kmh").all()
    assert len(rows) == 1
    assert rows[0].meta["path"] == "speed" and rows[0].meta["label"] == "speed|kmh"


def test_a_new_version_is_stored_next_to_the_old_one(memory):
    memory.add("pacifica", 2, "speed", "speed|kmh", "speed_kmh", embed(profile("speed", **SPEED_MPH)))
    assert memory.count() == 5
    hits = memory.search(embed(profile("speed", **SPEED_MPH)), k=1)
    assert (hits[0].oem, hits[0].version) == ("pacifica", 2)


def test_stored_row(memory, session):
    row = session.query(Embedding).filter_by(ref="pacifica/v1/odo_km").one()
    assert row.kind == "field"
    assert row.text == "odometer_mi -> odo|mi"
    assert row.meta == {"oem": "pacifica", "version": 1, "path": "odometer_mi", "label": "odo|mi",
                        "canonical": "odo_km"}
    stored = np.frombuffer(row.vector, dtype="<f4")
    assert np.allclose(stored, embed(profile("odometer_mi", **ODOMETER)))


def test_custom_text(session):
    mem = NumpyMemory(session)
    mem.add("helix", 1, "fahrt.v", "speed|mps", "speed_kmh", embed(profile("fahrt.v")), text_="speed in m/s")
    assert session.query(Embedding).one().text == "speed in m/s"


def test_other_kinds_of_embeddings_are_ignored(memory, session):
    session.add(Embedding(kind="incident", ref="inc-1", text="x", meta={"oem": "pacifica", "version": 1},
                          vector=embed(profile("speed_mph", **SPEED_MPH)).tobytes()))
    session.flush()
    assert memory.count() == 4
    assert all(h.ref != "inc-1" for h in memory.search(embed(profile("speed_mph", **SPEED_MPH)), k=10))


def test_query_of_another_dimension_finds_nothing(memory):
    assert memory.search(np.ones(DIM - 1, dtype=np.float32)) == []


def test_vector_of_a_wider_type_is_stored_as_float32(session):
    mem = NumpyMemory(session)
    mem.add("helix", 1, "x", "seq", "seq", embed(profile("x", frac_num=1.0)).astype(np.float64))
    assert len(session.query(Embedding).one().vector) == DIM * 4
    assert mem.search(embed(profile("x", frac_num=1.0)))[0].similarity == pytest.approx(1.0, abs=1e-4)


def test_make_memory_picks_the_adapter_for_the_database(session):
    assert type(make_memory(session)) is NumpyMemory


class FakePostgres:
    """A session bound to PostgreSQL, with or without the pgvector column."""

    def __init__(self, has_vec: bool, url: str = "postgresql://db/rosetta") -> None:
        self.has_vec = has_vec
        self.url = url
        self.queries: list[str] = []

    def get_bind(self):
        return type("Bind", (), {"dialect": type("D", (), {"name": "postgresql"})(), "url": self.url})()

    def execute(self, stmt, params=None):
        self.queries.append(str(stmt))
        found = (1,) if self.has_vec else None
        return type("Result", (), {"first": lambda _self: found})()


def test_make_memory_uses_pgvector_when_the_column_exists(monkeypatch):
    monkeypatch.setattr(M, "_has_vec", {})
    s = FakePostgres(has_vec=True)
    assert type(make_memory(s)) is M.PgVectorMemory
    assert "information_schema.columns" in s.queries[0] and "vec" in s.queries[0]


def test_make_memory_falls_back_to_the_scan_without_the_column(monkeypatch):
    monkeypatch.setattr(M, "_has_vec", {})
    assert type(make_memory(FakePostgres(has_vec=False))) is NumpyMemory


def test_make_memory_asks_each_database_only_once(monkeypatch):
    monkeypatch.setattr(M, "_has_vec", {})
    first, second = FakePostgres(True), FakePostgres(True)
    other = FakePostgres(False, url="postgresql://other/rosetta")
    make_memory(first)
    make_memory(second)
    assert len(first.queries) == 1 and second.queries == []
    assert type(make_memory(other)) is NumpyMemory and len(other.queries) == 1


def test_pgvector_memory_sends_the_search_to_the_database():
    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class FakeSession:
        def __init__(self):
            self.statements = []

        def execute(self, stmt, params=None):
            self.statements.append((str(stmt), params))
            meta = lambda oem, v: {"oem": oem, "version": v, "path": "p", "label": "seq", "canonical": "seq"}  # noqa: E731
            return Result([("a/v1/seq", meta("a", 1), 0.9), ("b/v1/seq", meta("b", 1), 0.8),
                           ("a/v2/seq", meta("a", 2), 0.7)])

    s = FakeSession()
    mem = M.PgVectorMemory(s)
    q = np.array([0.5, 0.25], dtype=np.float32)
    hits = mem.search(q, k=2, allowed={("a", 1), ("a", 2)})
    assert [(h.ref, h.similarity) for h in hits] == [("a/v1/seq", 0.9), ("a/v2/seq", 0.7)]
    sql, params = s.statements[0]
    assert "<=>" in sql and "LIMIT" in sql
    assert params == {"v": "[0.500000,0.250000]", "k": "field", "n": 8}
    assert len(mem.search(q, k=2)) == 2
