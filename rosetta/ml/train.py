"""Train and evaluate the field mapper.

    python -m rosetta.ml.train                 # train, evaluate, save
    python -m rosetta.ml.train --dialects 300  # smaller, faster

Writes the model to rosetta/ml/artifacts/ and the evaluation to
docs/evidence/ml_field_mapper.json.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.metrics import f1_score

from . import baseline
from .labels import IGNORE, field_of
from .model import ARTIFACT, FieldMapper
from .profile import FEATURE_NAMES, NAME_DIM, VALUE_FEATURES, build_profiles, feature_matrix, make_samples
from .synth import SynthDialect, demo_tokens, make_corpus


def featurise(corpus: list[SynthDialect], log=print) -> tuple[np.ndarray, list[str], list[str], list[int]]:
    X, y, paths, group = [], [], [], []
    for g, d in enumerate(corpus):
        prof = build_profiles(make_samples(d.messages, d.devices))
        p, M = feature_matrix(prof)
        for i, name in enumerate(p):
            if name not in d.labels:
                continue
            X.append(M[i])
            y.append(d.labels[name])
            paths.append(name)
            group.append(g)
        if log and (g + 1) % 100 == 0:
            log(f"  profiled {g + 1}/{len(corpus)} dialects")
    return np.asarray(X, dtype=np.float32), y, paths, group


def scores(y: list[str], pred: list[str]) -> dict[str, float]:
    y_f, p_f = [field_of(a) for a in y], [field_of(b) for b in pred]
    exact = float(np.mean([a == b for a, b in zip(y, pred)]))
    fld = float(np.mean([a == b for a, b in zip(y_f, p_f)]))
    unit_rows = [(a, b) for a, b in zip(y, pred) if "|" in a and field_of(a) == field_of(b)]
    unit = float(np.mean([a == b for a, b in unit_rows])) if unit_rows else 0.0
    real = [(a, b) for a, b in zip(y, pred) if a != IGNORE]
    recall = float(np.mean([a == b for a, b in real])) if real else 0.0
    ign = [(a, b) for a, b in zip(y, pred) if a == IGNORE]
    false_map = float(np.mean([b != IGNORE for _, b in ign])) if ign else 0.0
    return {"exact_label_accuracy": round(exact, 4), "field_accuracy": round(fld, 4),
            "unit_accuracy_when_field_right": round(unit, 4),
            "recall_on_real_fields": round(recall, 4), "distractors_wrongly_mapped": round(false_map, 4),
            "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4), "n": len(y)}


def per_field(y: list[str], pred: list[str]) -> dict[str, dict[str, float]]:
    tot: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for a, b in zip(y, pred):
        f = field_of(a)
        tot[f][0] += 1
        tot[f][1] += int(a == b)
    return {f: {"n": n, "exact": round(k / n, 4)} for f, (n, k) in sorted(tot.items())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialects", type=int, default=700)
    ap.add_argument("--test-dialects", type=int, default=160)
    ap.add_argument("--trees", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(ARTIFACT))
    ap.add_argument("--report", default="docs/evidence/ml_field_mapper.json")
    a = ap.parse_args()

    t0 = time.time()
    print(f"generating {a.dialects} training dialects")
    train = make_corpus(a.dialects, a.seed, test=False, log=print)
    Xtr, ytr, _, _ = featurise(train)
    print(f"generating {a.test_dialects} test dialects (names never seen in training)")
    test = make_corpus(a.test_dialects, a.seed + 999, test=True, log=print)
    Xte, yte, pte, gte = featurise(test)
    print(f"train rows {len(ytr):,}  test rows {len(yte):,}  features {Xtr.shape[1]}")

    clf = ExtraTreesClassifier(n_estimators=a.trees, min_samples_leaf=1, max_features=0.25,
                               class_weight="balanced_subsample", n_jobs=-1, random_state=a.seed)
    clf.fit(Xtr, ytr)
    classes = list(clf.classes_)
    pred = list(clf.predict(Xte))
    base = [baseline.predict(p) for p in pte]

    # ablations: which feature family carries the result
    nd, nv = len(FEATURE_NAMES), len(VALUE_FEATURES)
    abl = {}
    for name, cols in (("names_only", list(range(nd, nd + NAME_DIM))),
                       ("values_only", list(range(nv))),
                       ("values_and_physics", list(range(nd))),
                       ("names_and_values", list(range(nv)) + list(range(nd, nd + NAME_DIM)))):
        c2 = ExtraTreesClassifier(n_estimators=120, max_features=0.25, class_weight="balanced_subsample",
                                  n_jobs=-1, random_state=a.seed).fit(Xtr[:, cols], ytr)
        abl[name] = scores(yte, list(c2.predict(Xte[:, cols])))

    # whole-dialect result: after joint assignment, is every mapped field right?
    mapper = FieldMapper(clf, classes)
    whole_ok = whole_base = 0
    fields_right = fields_total = 0
    for d in test:
        prof = build_profiles(make_samples(d.messages, d.devices))
        want = {field_of(lab): (p, lab) for p, lab in d.labels.items() if lab != IGNORE}
        got = {c.field: (c.path, c.label) for c in mapper.assign(prof)}
        right = sum(1 for f, v in want.items() if got.get(f) == v)
        fields_right += right
        fields_total += len(want)
        whole_ok += int(right == len(want) and set(got) <= set(want))
        bgot: dict[str, tuple[str, str]] = {}
        for p in d.labels:
            lab = baseline.predict(p)
            if lab != IGNORE:
                bgot.setdefault(field_of(lab), (p, lab))
        whole_base += int(all(bgot.get(f) == v for f, v in want.items()) and set(bgot) <= set(want))

    imp = clf.feature_importances_
    dense_imp = sorted(zip(FEATURE_NAMES, imp[:nd]), key=lambda kv: -kv[1])[:12]
    report: dict[str, Any] = {
        "task": "map a source telemetry field to a canonical field and unit",
        "model": f"ExtraTreesClassifier({a.trees} trees)",
        "data": {"train_dialects": a.dialects, "test_dialects": a.test_dialects,
                 "train_rows": len(ytr), "test_rows": len(yte), "labels": len(classes),
                 "features": int(Xtr.shape[1]),
                 "leakage_controls": [
                     "test dialects use field names that never occur in training",
                     "tokens of the demo dialects are removed from the training vocabulary: "
                     + ", ".join(sorted(demo_tokens())),
                     "train and test fleets use different seeds and regions",
                 ]},
        "model_scores": scores(yte, pred),
        "baseline_scores": scores(yte, base),
        "baseline": "name similarity (difflib) against canonical names and aliases, unit from name suffix",
        "ablations": abl,
        "whole_dialect": {
            "dialects": len(test),
            "model_fully_correct": round(whole_ok / len(test), 4),
            "baseline_fully_correct": round(whole_base / len(test), 4),
            "model_fields_correct_after_assignment": round(fields_right / fields_total, 4),
        },
        "per_field_model": per_field(yte, pred),
        "per_field_baseline": per_field(yte, base),
        "share_of_importance": {"physics": round(float(imp[nv:nd].sum()), 4),
                                "values": round(float(imp[:nv].sum()), 4),
                                "names": round(float(imp[nd:].sum()), 4)},
        "top_dense_features": [[k, round(float(v), 4)] for k, v in dense_imp],
        "seconds": round(time.time() - t0, 1),
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"clf": clf, "classes": classes,
                 "meta": {"trained_at": int(time.time()), "scores": report["model_scores"],
                          "baseline": report["baseline_scores"], "trees": a.trees}}, a.out, compress=3)
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("model_scores", "baseline_scores", "ablations", "whole_dialect",
                                              "share_of_importance")}, indent=2))
    print(f"model: {a.out} ({Path(a.out).stat().st_size / 1e6:.1f} MB)   report: {a.report}")


if __name__ == "__main__":
    main()
