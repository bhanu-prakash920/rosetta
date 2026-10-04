"""Golden-set validation: does a mapping produce the known-correct answers?

A golden case is a raw payload plus the canonical event it must become. It is
the objective test that stands between a proposed mapping (from a human or the
AI agent) and production traffic.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..domain.canonical import CANONICAL_FIELDS
from ..domain.errors import NormalizeError, SpecError
from ..engine.compiler import compile_spec

TOLERANCE = {"lat": 2e-6, "lon": 2e-6, "heading_deg": 0.06, "speed_kmh": 0.02, "odo_km": 0.06,
             "soc_pct": 0.06, "fuel_pct": 0.06, "ambient_c": 0.06}
CHECKED_FIELDS = tuple(f.name for f in CANONICAL_FIELDS)


def field_matches(name: str, got: Any, want: Any) -> bool:
    if want is None or want == []:
        return got is None or got == [] or got == want
    if got is None:
        return False
    tol = TOLERANCE.get(name)
    if tol is not None:
        try:
            return abs(float(got) - float(want)) <= tol
        except (TypeError, ValueError):
            return False
    return got == want


@dataclass
class GoldenReport:
    total: int = 0
    passed: int = 0
    field_total: dict[str, int] = field(default_factory=dict)
    field_ok: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def field_accuracy(self) -> dict[str, float]:
        return {k: round(self.field_ok.get(k, 0) / n, 4) for k, n in self.field_total.items() if n}

    def to_dict(self) -> dict[str, Any]:
        return {"total": self.total, "passed": self.passed, "pass_rate": round(self.pass_rate, 4),
                "field_accuracy": self.field_accuracy(), "failures": self.failures[:10], "error": self.error}


def run_golden(oem: str, spec: dict[str, Any], cases: Iterable[tuple[bytes, dict[str, Any]]],
               max_failures: int = 10) -> GoldenReport:
    rep = GoldenReport()
    try:
        adapter = compile_spec(oem, 0, spec)
    except SpecError as e:
        rep.error = f"spec rejected: {e}"
        return rep
    for payload, expected in cases:
        rep.total += 1
        try:
            ev = adapter.normalize(payload)
        except NormalizeError as e:
            if len(rep.failures) < max_failures:
                rep.failures.append({"case": rep.total, "reason": e.reason, "field": e.field, "detail": e.detail})
            for name in expected:
                rep.field_total[name] = rep.field_total.get(name, 0) + 1
            continue
        good = True
        for name in CHECKED_FIELDS:
            want = expected.get(name)
            got = ev.get(name)
            if want is None and got is None:
                continue
            rep.field_total[name] = rep.field_total.get(name, 0) + 1
            if field_matches(name, got, want):
                rep.field_ok[name] = rep.field_ok.get(name, 0) + 1
            else:
                good = False
                if len(rep.failures) < max_failures:
                    rep.failures.append({"case": rep.total, "reason": "WRONG_VALUE", "field": name,
                                         "got": got, "want": want})
        if good:
            rep.passed += 1
    return rep
