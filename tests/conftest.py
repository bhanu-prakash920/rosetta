from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support import PASSWORD, World  # noqa: E402


@pytest.fixture(scope="module")
def world():
    """A seeded world shared by the tests of one module. The pipeline has not run yet."""
    w = World(vehicles=3000)
    yield w
    w.close()


@pytest.fixture()
def fresh_world():
    """A world of its own, for tests that change mappings or state."""
    w = World(vehicles=2000)
    yield w
    w.close()


@pytest.fixture(scope="module")
def api(world):
    """The real FastAPI application on top of `world`, with traffic already processed."""
    from fastapi.testclient import TestClient

    from rosetta.api.main import create_app

    world.sim.s.enabled = ["nordvik", "pacifica", "stellaris", "kaizen", "voltaic", "helix"]
    world.sim.s.mode = "realistic"
    with TestClient(create_app()) as client:
        for _ in range(30):
            world.tick()
            world.pump(rounds=1)
        world.release_delayed()
        world.pump()
        world.metrics()
        client.world = world
        yield client


def login(client, email: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def engineer(api):
    return login(api, "engineer@rosetta.example")


@pytest.fixture(scope="module")
def analyst(api):
    return login(api, "analyst@rosetta.example")


@pytest.fixture(scope="module")
def manager(api):
    return login(api, "manager@northwind.example")


@pytest.fixture(scope="module")
def other_manager(api):
    return login(api, "manager@bluepeak.example")


@pytest.fixture(scope="module")
def admin(api):
    return login(api, "admin@rosetta.example")
