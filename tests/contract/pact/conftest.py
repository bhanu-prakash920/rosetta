"""Shared set-up for the Pact contract tests.

Pact files are written to tests/contract/pacts/ and committed, so a reviewer can
read every contract without running anything. Consumer tests (re)write them;
provider tests verify the committed files. No Pact Broker is involved.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PACT_DIR = ROOT / "tests" / "contract" / "pacts"

# Participant names, used by both sides. A typo here would make a provider
# silently verify nothing, so they live in one place.
NORMALIZER = "rosetta-normalizer"
PROCESSOR = "rosetta-processor"
DLQ_WORKER = "rosetta-dlq-worker"
ALERT_SUBSCRIBER = "fleet-alerts-subscriber"
WEB_CONSOLE = "rosetta-web-console"
API = "rosetta-api"

# A small seeded world: 60 vehicles, the six sources, the demo users.
SEED_VEHICLES = 60
SEED_VALUE = 7


def seeded_environment() -> Path:
    """A fresh data directory with a seeded SQLite database and the file-backed broker."""
    from rosetta.db.session import init_db, session_scope
    from rosetta.services import seed
    from tests.support import fresh_environment

    d = Path(tempfile.mkdtemp(prefix="rosetta-pact-"))
    fresh_environment(d)
    init_db()
    with session_scope() as s:
        seed.seed(s, vehicles=SEED_VEHICLES, seed_value=SEED_VALUE, fleets=5, tenants=3,
                  golden_per_dialect=5, log=lambda *a: None)
    return d


@pytest.fixture(scope="module")
def seeded():
    return seeded_environment()


def pact_file(consumer: str, provider: str) -> Path:
    return PACT_DIR / f"{consumer}-{provider}.json"


def as_bytes(body: object) -> bytes:
    """A message body as the broker would carry it (Pact hands it over as str or a buffer)."""
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode()
    return bytes(body)  # type: ignore[call-overload]
