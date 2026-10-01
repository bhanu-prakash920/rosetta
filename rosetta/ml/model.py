"""The field mapper: a classifier over (name, values, physics) features."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from ..algorithms.hungarian import max_score_assignment
from .labels import CANONICAL_TARGETS, field_of, transforms_of
from .profile import FieldProfile, feature_matrix

ARTIFACT = Path(__file__).parent / "artifacts" / "field_mapper.joblib"
_model: FieldMapper | None = None


@dataclass
class Candidate:
    path: str
    label: str
    field: str
    prob: float


class FieldMapper:
    def __init__(self, clf: Any, classes: list[str], meta: dict[str, Any] | None = None) -> None:
        self.clf = clf
        self.classes = classes
        self.meta = meta or {}
        self._canon_cols = {
            c: [i for i, lab in enumerate(classes) if field_of(lab) == c] for c in CANONICAL_TARGETS
        }

    def predict_proba(self, profiles: dict[str, FieldProfile]) -> tuple[list[str], np.ndarray]:
        paths, X = feature_matrix(profiles)
        if not paths:
            return [], np.zeros((0, len(self.classes)))
        return paths, self.clf.predict_proba(X)

    def candidates(self, profiles: dict[str, FieldProfile], top: int = 4) -> dict[str, list[Candidate]]:
        paths, P = self.predict_proba(profiles)
        out = {}
        for i, p in enumerate(paths):
            order = np.argsort(-P[i])[:top]
            out[p] = [Candidate(p, self.classes[j], field_of(self.classes[j]), float(P[i, j])) for j in order]
        return out

    def assign(self, profiles: dict[str, FieldProfile], min_score: float = 0.12) -> list[Candidate]:
        """One source field per canonical field, chosen jointly.

        Scoring each source field on its own can map two of them to the same
        target. The Hungarian algorithm finds the one-to-one assignment with
        the highest total probability instead.
        """
        paths, P = self.predict_proba(profiles)
        if not paths:
            return []
        S = np.zeros((len(paths), len(CANONICAL_TARGETS)))
        best_label = {}
        for c_i, c in enumerate(CANONICAL_TARGETS):
            cols = self._canon_cols[c]
            if not cols:
                continue
            sub = P[:, cols]
            S[:, c_i] = sub.sum(axis=1)          # probability of the field, any unit
            arg = sub.argmax(axis=1)
            for r in range(len(paths)):
                best_label[(r, c_i)] = self.classes[cols[int(arg[r])]]
        out = []
        for r, c_i, val in max_score_assignment(S, min_score):
            out.append(Candidate(paths[r], best_label[(r, c_i)], CANONICAL_TARGETS[c_i], val))
        return out

    def alternatives(self, profiles: dict[str, FieldProfile], canonical: str, top: int = 6) -> list[Candidate]:
        """Other (source field, unit) choices for one canonical field, best first. Used by repair."""
        paths, P = self.predict_proba(profiles)
        cols = self._canon_cols.get(canonical, [])
        out = []
        for r, p in enumerate(paths):
            for j in cols:
                if P[r, j] > 0.005:
                    out.append(Candidate(p, self.classes[j], canonical, float(P[r, j])))
        out.sort(key=lambda c: -c.prob)
        return out[:top]


def load(path: Path | None = None) -> FieldMapper:
    global _model
    if _model is None or path is not None:
        blob = joblib.load(path or ARTIFACT)
        m = FieldMapper(blob["clf"], blob["classes"], blob.get("meta"))
        if path is not None:
            return m
        _model = m
    return _model


def to_field_spec(c: Candidate) -> dict[str, Any]:
    return {"path": c.path, "transforms": transforms_of(c.label)}
