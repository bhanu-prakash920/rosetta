"""The deterministic engine: the same tools, called in a fixed order by code.

This is what runs when no language model is configured, and it is the
reference the model-driven engine is compared with. It is a plain state
machine, so every run is reproducible.

    sample -> profile -> memory -> suggest -> learn event codes
           -> validate (dev) -> repair (at most 3 rounds) -> submit

Repair is coordinate descent: for each canonical field whose accuracy on the
development golden cases is too low, try the other encodings and the other
source fields the classifier considered, and keep the one that scores best.
A field that cannot be made right is left out rather than mapped wrongly,
unless it is required.
"""
from __future__ import annotations

from typing import Any

from ..domain import canonical as C
from ..ml import model as mlmodel
from .toolbox import ENCODINGS, Toolbox

GOOD = 0.99
MAX_ROUNDS = 3


def _rationale(f: dict[str, Any]) -> str:
    parts = [f"classifier {f.get('confidence', 0):.2f} for {f['encoding']}"]
    ph = f.get("physics") or {}
    if ph.get("speed_ratio_fit", 0) > 0.85 and f["canonical"] == "speed_kmh":
        parts.append(f"value is {10 ** (ph['speed_ratio_log'] * 4):.3f} x GPS speed (fit {ph['speed_ratio_fit']:.2f})")
    if ph.get("dist_ratio_fit", 0) > 0.85 and f["canonical"] == "odo_km":
        parts.append(f"grows {10 ** (ph['dist_ratio_log'] * 4):.4g} units per km travelled (fit {ph['dist_ratio_fit']:.2f})")
    if ph.get("coord_lat_score") or ph.get("coord_lon_score"):
        parts.append("movement bearing agrees with the reported heading")
    if ph.get("heading_fit", 0) > 0.5:
        parts.append(f"agrees with GPS bearing (cos {ph['heading_fit']:.2f})")
    m = f.get("memory")
    if m and m["similarity"] >= 0.8:
        parts.append(f"resembles {m['known_as']} ({m['similarity']:.2f})")
    return "; ".join(parts)


def run(tb: Toolbox) -> dict[str, Any]:
    s = tb.call("sample_dead_letters")
    if "error" in s:
        return {"status": "failed", "reason": s["error"]}
    p = tb.call("profile_fields")
    if "error" in p:
        return {"status": "failed", "reason": p["error"]}
    sug = tb.call("suggest_mapping")
    if "error" in sug:
        return {"status": "failed", "reason": sug["error"]}
    fields = [{"canonical": f["canonical"], "path": f["path"], "encoding": f["encoding"],
               "confidence": f["confidence"], "rationale": _rationale(f)} for f in sug["fields"]]
    first = [dict(f) for f in fields]

    evt = next((f for f in fields if f["canonical"] == "evt"), None)
    if evt is not None and tb.golden_dev:
        tb.call("learn_event_codes", path=evt["path"])

    if not tb.golden_dev:
        out = tb.call("submit_draft", fields=fields, summary="no golden cases: submitted unvalidated")
        return {"status": "submitted", "result": out, "first_proposal": first, "rounds": 0}

    rep = tb.call("validate_mapping", fields=fields)
    rounds = 0
    mapper = mlmodel.load()
    while rounds < MAX_ROUNDS and rep.get("pass_rate", 0.0) < GOOD:
        if "error" in rep and not _missing_required(fields):
            break       # rejected for a reason repair cannot fix
        rounds += 1
        changed = _repair(tb, fields, rep, mapper)
        if not changed:
            break
        rep = tb.call("validate_mapping", fields=fields)

    summary = (f"{len(fields)} fields mapped; development golden pass rate "
               f"{rep.get('pass_rate', 0):.1%} after {rounds} repair round(s)")
    out = tb.call("submit_draft", fields=fields, summary=summary)
    return {"status": "submitted" if "error" not in out else "failed", "result": out,
            "first_proposal": first, "final": fields, "rounds": rounds, "summary": summary}


def _accuracy(tb: Toolbox, fields: list[dict[str, Any]], canonical: str) -> float:
    """Accuracy of one field on the development cases. Internal: not a recorded step."""
    from ..services.golden import run_golden

    evt = next((f for f in fields if f["canonical"] == "evt"), None)
    # A candidate event field is only fair to score with codes learned for that field.
    codes = tb.event_table(evt["path"])["codes"] if canonical == "evt" and evt and tb.golden_dev else None
    spec = tb.build_spec(fields, codes)
    rep = run_golden(tb.oem, {k: v for k, v in spec.items() if k != "_meta"}, tb.golden_dev, max_failures=0)
    if rep.error:
        return 0.0
    return rep.field_accuracy().get(canonical, 0.0)


def _missing_required(fields: list[dict[str, Any]]) -> list[str]:
    mapped = {f["canonical"] for f in fields}
    return [c for c in C.REQUIRED if c not in mapped]


def _repair(tb: Toolbox, fields: list[dict[str, Any]], rep: dict[str, Any], mapper: Any) -> bool:
    if "error" in rep:
        # Nothing can be scored until every required field is mapped: find those first.
        todo = _missing_required(fields)
    else:
        acc = rep.get("field_accuracy", {})
        expected = {k for _p, e in tb.golden_dev for k in e}
        todo = [f["canonical"] for f in fields if acc.get(f["canonical"], 0.0) < GOOD]
        todo += [c for c in C.FIELD_BY_NAME if c in expected and c not in {f["canonical"] for f in fields}]
    acc = rep.get("field_accuracy", {})
    changed = False
    tried = []
    for canon in dict.fromkeys(todo):
        cur = next((f for f in fields if f["canonical"] == canon), None)
        others = [f for f in fields if f["canonical"] != canon]
        taken = {f["path"] for f in others}
        options: list[tuple[str, str, float]] = []
        if cur is not None:
            options += [(cur["path"], enc, 0.0) for enc in ENCODINGS[canon] if enc != cur["encoding"]]
        options += [(a.path, a.label, a.prob) for a in mapper.alternatives(tb.profiles, canon, top=8)]
        for path in tb.profiles:       # last resort: any free source field, every encoding
            if path not in taken:
                options += [(path, enc, 0.0) for enc in ENCODINGS[canon]]
        best = (acc.get(canon, 0.0) if cur is not None else 0.0, cur)
        seen = set()
        for path, enc, prob in options:
            if (path, enc) in seen or path in taken:
                continue
            seen.add((path, enc))
            cand = {"canonical": canon, "path": path, "encoding": enc, "confidence": round(prob, 3),
                    "rationale": ""}
            try:
                a = _accuracy(tb, others + [cand], canon)
            except Exception:
                continue
            if a > best[0] + 1e-9:
                best = (a, cand)
            if a >= GOOD:
                break
        score, pick = best
        if pick is not None and pick is not cur:
            was = f"{cur['path']} as {cur['encoding']}" if cur else "unmapped"
            pick["rationale"] = (f"repaired: {was} scored {acc.get(canon, 0.0):.0%} on the development golden "
                                 f"cases, {pick['path']} as {pick['encoding']} scores {score:.0%}")
            pick["confidence"] = round(max(pick["confidence"], score * 0.9), 3)
            fields[:] = others + [pick]
            changed = True
            tried.append({"canonical": canon, "now": f"{pick['path']} / {pick['encoding']}", "accuracy": score})
            if canon == "evt":
                tb.call("learn_event_codes", path=pick["path"])
        elif cur is not None and score < 0.5 and not C.FIELD_BY_NAME[canon].required:
            fields[:] = others      # better absent than wrong
            changed = True
            tried.append({"canonical": canon, "now": "left unmapped", "accuracy": score})
    if tried:
        tb.s.flush()
    return changed
