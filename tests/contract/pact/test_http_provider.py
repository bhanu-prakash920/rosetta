"""Provider side of the web console pact: the real API, served over HTTP.

The API (`rosetta.api.main:create_app`) runs under uvicorn on a free local port
(18765 by default, never 8000 or 8765), on top of a small but complete world:
seeded database, file-backed broker and the real pipeline workers. The Pact
verifier replays every request from `rosetta-web-console-rosetta-api.json`
and checks the responses against the matchers.

Provider states are Python callbacks in this test process. They drive the same
world the API reads (database, hot state), so a state such as "messages from an
unmapped source are waiting in the dead-letter queue" is produced by running
the pipeline, not by inserting rows. The callbacks exist only in this test.

The console's bearer token is an example value in the pact. The verifier
replaces it with a real token for the platform engineer, obtained by signing in.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import orjson
import pytest
from pact import Verifier

from .conftest import API, WEB_CONSOLE, pact_file

BUSY = {8000, 8765}                        # other services on this machine: never touch them
PORT = int(os.environ.get("PACT_PROVIDER_PORT", "18765"))
ENGINEER = "engineer@rosetta.example"


def free_port(preferred: int) -> int:
    assert preferred not in BUSY
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


@pytest.fixture(scope="module")
def world() -> Iterator[Any]:
    from tests.support import ALL_SOURCES, World

    # helix has no mapping at start-up, so its traffic becomes dead letters
    w = World(vehicles=300, sources=ALL_SOURCES)
    w.run(3)
    yield w
    w.close()


@pytest.fixture(scope="module")
def base_url(world) -> Iterator[str]:
    import uvicorn

    from rosetta.api.main import create_app

    port = free_port(PORT)
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="on"))
    t = threading.Thread(target=server.run, name="pact-provider-api", daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert t.is_alive() and time.monotonic() < deadline, "the API did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


# ------------------------------------------------------------ provider states
def states(world) -> dict[str, Callable[..., None]]:
    from rosetta.db.session import session_scope
    from rosetta.pipeline.dlq import queue_replay
    from rosetta.ports.broker import Record

    def nothing(**_: Any) -> None:
        """True in the seeded world: demo users with the demo password, 300 vehicles."""

    def dead_letters(**_: Any) -> None:
        world.run(2)                       # more helix traffic, parked as NO_ADAPTER

    def replay_job(**_: Any) -> None:
        with session_scope() as s:
            queue_replay(s, "helix")
        world.pump()                       # the dead-letter worker runs the job

    def hard_brake(**_: Any) -> None:
        # A canonical event with a driving event, handled by the real processor,
        # which writes the alert the console lists.
        vin = world.vins[0]
        world.clock += 1000
        ev = {"vin": vin, "ts": world.clock, "seq": 10_000_000 + world.clock // 1000, "oem": "stellaris",
              "map_v": 1, "lat": 17.4, "lon": 78.5, "speed_kmh": 61.0, "odo_km": 1200.0, "evt": "HARSH_BRAKE",
              "rx_ts": world.clock + 20, "norm_ts": world.clock + 25}
        assert world.processor.handle([Record(key=vin.encode(), value=orjson.dumps(ev))]) == 1

    return {
        "the demo users exist": nothing,
        "a platform engineer is signed in": nothing,      # the verifier sends a real token
        "the fleet has more than one page of vehicles": nothing,
        "messages from an unmapped source are waiting in the dead-letter queue": dead_letters,
        "a replay job exists": replay_job,
        "a vehicle has braked hard": hard_brake,
    }


def sign_in(base_url: str) -> str:
    from tests.support import PASSWORD

    r = httpx.post(f"{base_url}/api/v1/auth/token", data={"username": ENGINEER, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def verifier(base_url: str, world, token: str) -> Verifier:
    f = pact_file(WEB_CONSOLE, API)
    assert f.exists(), f"missing {f.name}: run `npm run test:pact` in web/ first"
    return (Verifier(API, host="127.0.0.1")
            .add_transport(url=base_url)
            .add_source(f)
            .state_handler(states(world), teardown=False)
            .add_custom_header("Authorization", f"Bearer {token}")
            .set_error_on_empty_pact(enabled=True)
            .set_coloured_output(enabled=False))


def test_api_honours_the_web_console_pact(base_url, world):
    v = verifier(base_url, world, sign_in(base_url))
    try:
        v.verify()
    except RuntimeError as e:        # the Rust core only says "failed": attach its report
        raise AssertionError(f"the API does not honour the console's pact:\n{v.output()}") from e
    out = str(v.output())
    expected = len(orjson.loads(pact_file(WEB_CONSOLE, API).read_bytes())["interactions"])
    assert out.count("has a matching body (OK)") == expected >= 8, out
