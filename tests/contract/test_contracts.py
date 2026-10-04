"""Contract tests: what one part promises and another part relies on.

Three contracts, each checked from the consumer's side:
  1. normaliser -> every reader of the canonical topic   the published JSON Schema
  2. API -> web console                                  every path and method the UI calls
  3. API -> external clients                             the committed OpenAPI document

They are the same idea as Pact (the consumer states what it needs, the provider
is verified against it) without a broker, because producer and consumers live in
one repository.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import orjson
import pytest

from rosetta.domain import canonical
from rosetta.engine.compiler import compile_spec
from rosetta.simulator.dialects import DIALECTS
from rosetta.simulator.fleet import OEM_INDEX, Fleet

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "docs" / "api" / "openapi.json"


@pytest.fixture(scope="module")
def app_spec():
    import tempfile

    from tests.support import fresh_environment

    fresh_environment(Path(tempfile.mkdtemp(prefix="rosetta-contract-")))
    from rosetta.api.main import create_app

    return create_app().openapi()


# ------------------------------------------------ 1. the canonical event schema
@pytest.mark.parametrize("key", list(DIALECTS))
def test_every_dialect_produces_schema_valid_events(key):
    d = DIALECTS[key]
    fleet = Fleet(1500, seed=21)
    for k in range(3):
        fleet.step(1.0, 1_790_000_000_000 + k * 1000)
    idx = fleet.emitting("all")
    sel = idx[fleet.oem[idx] == OEM_INDEX[d.oem]]
    fleet.evt[sel[:9]] = range(1, 10)
    adapter = compile_spec(d.oem, 1, d.spec)
    validator = jsonschema.Draft202012Validator(canonical.json_schema())
    n = 0
    for payload in d.encode(fleet.truth(sel)):
        ev = adapter.normalize(payload)
        ev.update({"rx_ts": 1, "norm_ts": 2})
        wire = orjson.loads(orjson.dumps(ev))            # what a consumer receives
        errors = sorted(validator.iter_errors(wire), key=str)
        assert not errors, errors[0].message
        n += 1
    assert n > 50


def test_processor_reads_only_what_the_schema_defines():
    from rosetta.pipeline.processor import CANON

    lineage = {"oem", "map_v", "rx_ts", "norm_ts", "replayed"}
    declared = set(canonical.json_schema()["properties"]) | lineage
    assert set(CANON.names) <= declared
    assert set(canonical.REQUIRED) <= set(CANON.names)


def test_archive_and_timescale_agree_on_columns():
    from rosetta.adapters.archive import SCHEMA
    from rosetta.adapters.timescale import COLUMNS

    assert set(COLUMNS) <= set(SCHEMA.names)


def test_schema_is_backward_compatible_with_v1_consumers():
    """A consumer written against version 1 needs these fields with these types, forever.
    New optional fields may be added. These may not change."""
    s = canonical.json_schema()
    v1 = {"vin": "string", "ts": "integer", "seq": "integer", "lat": "number", "lon": "number",
          "speed_kmh": "number", "odo_km": "number"}
    assert s["required"] == list(v1)
    assert {k: s["properties"][k]["type"] for k in v1} == v1
    assert s["additionalProperties"] is True          # consumers must tolerate fields they do not know


# ------------------------------------------------------ 2. what the console calls
CALL = re.compile(r"""(?:api(?:<[^>]*>)?|useApi(?:<[^(]*>)?|stream)\(\s*(?:[a-zA-Z_.]+\s*\?\s*)?[`"]([^`"]+)[`"]""")
METHOD = re.compile(r"""api(?:<[^>]*>)?\(\s*[`"]([^`"]+)[`"]\s*,\s*\{\s*method:\s*"(\w+)\"""")


def _normalise(path: str) -> str:
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{x}", path)
    return re.sub(r"\{[^}]*\}", "{x}", path)


def console_calls() -> set[tuple[str, str]]:
    calls = set()
    for f in (ROOT / "web" / "src").rglob("*.ts*"):
        src = f.read_text()
        posts = {_normalise(p): m.lower() for p, m in METHOD.findall(src)}
        for p in CALL.findall(src):
            if p.startswith("/"):
                n = _normalise(p)
                calls.add((posts.get(n, "get"), n))
    calls.add(("post", "/auth/token"))
    return calls


def test_console_only_calls_operations_the_api_has(app_spec):
    have = {(m, _normalise(p.removeprefix("/api/v1"))) for p, ops in app_spec["paths"].items() for m in ops}
    calls = console_calls()
    assert len(calls) >= 25, "the scan found too few calls, the pattern is probably out of date"
    missing = sorted(c for c in calls if c not in have)
    assert not missing, f"the console calls operations the API does not offer: {missing}"


# ---------------------------------------------------- 3. the published API document
def surface(spec) -> dict[str, list[str]]:
    out = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            params = sorted(p["name"] + ("*" if p.get("required") else "") for p in op.get("parameters", []))
            out[f"{method.upper()} {path}"] = params
    return dict(sorted(out.items()))


def test_api_matches_the_committed_document(app_spec):
    assert SNAPSHOT.exists(), "run: python scripts/export_openapi.py"
    committed = surface(json.loads(SNAPSHOT.read_text()))
    live = surface(app_spec)
    removed = sorted(set(committed) - set(live))
    assert not removed, f"operations were removed, which breaks clients: {removed}"
    for op, params in committed.items():
        newly_required = {p for p in live[op] if p.endswith("*")} - {p for p in params if p.endswith("*")}
        assert not newly_required, f"{op} has new required parameters: {newly_required}"
        gone = {p.rstrip("*") for p in params} - {p.rstrip("*") for p in live[op]}
        assert not gone, f"{op} lost parameters: {gone}"
    added = sorted(set(live) - set(committed))
    assert not added, f"new operations are not documented yet, run scripts/export_openapi.py: {added}"


def test_every_operation_documents_itself(app_spec):
    for path, ops in app_spec["paths"].items():
        for method, op in ops.items():
            assert op.get("summary"), f"{method.upper()} {path} has no summary"
            assert op.get("tags"), f"{method.upper()} {path} has no tag"
