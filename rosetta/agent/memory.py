"""Mapping memory: a vector store of fields the platform has already learned.

Every approved mapping teaches the platform something: "a field called
odometer_mi with these statistics was an odometer in miles". We store one
vector per mapped field. When a new format arrives, the agent looks up the
nearest known fields. After a firmware update most fields are unchanged, so
retrieval answers them immediately and the classifier only has to work on what
is actually new.

The vector is the same feature vector the classifier uses (name n-grams,
value statistics, physics), L2-normalised, so cosine similarity compares
fields by what they look like and how they behave, not only by their name.

Two adapters: NumpyMemory scans all vectors (exact, fine up to ~100K fields),
PgVectorMemory pushes the search into PostgreSQL with an HNSW index.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from ..db.models import Embedding
from ..ml.profile import N_DENSE, NAME_DIM, FieldProfile, name_vector

DIM = N_DENSE + NAME_DIM
KIND = "field"
NAME_WEIGHT = 0.6     # names matter, but a renamed field must still be found by its values


def embed(profile: FieldProfile) -> np.ndarray:
    dense = np.nan_to_num(profile.features.astype(np.float32))
    dn = float(np.linalg.norm(dense))
    if dn:
        dense = dense / dn
    v = np.concatenate([dense * (1.0 - NAME_WEIGHT), name_vector(profile.path) * NAME_WEIGHT])
    n = float(np.linalg.norm(v))
    return (v / n if n else v).astype(np.float32)


@dataclass
class Neighbour:
    ref: str
    similarity: float
    oem: str
    version: int
    path: str
    label: str
    canonical: str


class NumpyMemory:
    def __init__(self, s: Session) -> None:
        self.s = s

    def add(self, oem: str, version: int, path: str, label: str, canonical: str, vector: np.ndarray,
            text_: str = "") -> None:
        ref = f"{oem}/v{version}/{canonical}"
        self.s.execute(delete(Embedding).where(Embedding.kind == KIND, Embedding.ref == ref))
        self.s.add(Embedding(kind=KIND, ref=ref, text=text_ or f"{path} -> {label}",
                             meta={"oem": oem, "version": version, "path": path, "label": label,
                                   "canonical": canonical},
                             vector=vector.astype("<f4").tobytes()))

    def search(self, vector: np.ndarray, k: int = 3, allowed: set[tuple[str, int]] | None = None) -> list[Neighbour]:
        rows = self.s.execute(select(Embedding.ref, Embedding.meta, Embedding.vector)
                              .where(Embedding.kind == KIND)).all()
        if allowed is not None:
            rows = [r for r in rows if (r[1]["oem"], r[1]["version"]) in allowed]
        if not rows:
            return []
        M = np.frombuffer(b"".join(bytes(r[2]) for r in rows), dtype="<f4").reshape(len(rows), -1)
        if M.shape[1] != vector.size:
            return []
        sims = M @ vector.astype(np.float32)
        order = np.argsort(-sims)[:k]
        return [Neighbour(rows[i][0], float(sims[i]), rows[i][1]["oem"], rows[i][1]["version"],
                          rows[i][1]["path"], rows[i][1]["label"], rows[i][1]["canonical"]) for i in order]

    def count(self) -> int:
        return len(self.s.execute(select(Embedding.id).where(Embedding.kind == KIND)).all())


class PgVectorMemory(NumpyMemory):
    """Same interface, search done by PostgreSQL: `ORDER BY vec <=> query LIMIT k`.

    Needs the `vec vector(N)` column and HNSW index from infra/sql/postgres_extras.sql.
    """

    def add(self, oem: str, version: int, path: str, label: str, canonical: str, vector: np.ndarray,
            text_: str = "") -> None:
        super().add(oem, version, path, label, canonical, vector, text_)
        self.s.flush()
        lit = "[" + ",".join(f"{x:.6f}" for x in vector.tolist()) + "]"
        self.s.execute(text("UPDATE embedding SET vec = CAST(:v AS vector) WHERE kind = :k AND ref = :r"),
                       {"v": lit, "k": KIND, "r": f"{oem}/v{version}/{canonical}"})

    def search(self, vector: np.ndarray, k: int = 3, allowed: set[tuple[str, int]] | None = None) -> list[Neighbour]:
        lit = "[" + ",".join(f"{x:.6f}" for x in vector.tolist()) + "]"
        rows = self.s.execute(text(
            "SELECT ref, meta, 1 - (vec <=> CAST(:v AS vector)) AS sim FROM embedding "
            "WHERE kind = :k AND vec IS NOT NULL ORDER BY vec <=> CAST(:v AS vector) LIMIT :n"),
            {"v": lit, "k": KIND, "n": k * 4}).all()
        out = []
        for ref, meta, sim in rows:
            if allowed is not None and (meta["oem"], meta["version"]) not in allowed:
                continue
            out.append(Neighbour(ref, float(sim), meta["oem"], meta["version"], meta["path"],
                                 meta["label"], meta["canonical"]))
        return out[:k]


_has_vec: dict[str, bool] = {}


def make_memory(s: Session) -> Any:
    """pgvector when the database has it (the `vec` column from postgres_extras.sql),
    otherwise the exact scan, which works on every database."""
    bind = s.get_bind()
    if bind.dialect.name != "postgresql":
        return NumpyMemory(s)
    key = str(bind.url)
    if key not in _has_vec:
        _has_vec[key] = bool(s.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'embedding' AND column_name = 'vec'"
        )).first())
    return PgVectorMemory(s) if _has_vec[key] else NumpyMemory(s)
