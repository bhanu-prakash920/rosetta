"""Shared fixtures for the unit suite.

Every test runs with its own environment: a throw-away data directory, the
"test" environment name and no secrets inherited from the shell. Nothing here
touches the project's data/ directory or the network.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from rosetta import config

VALID_VIN = "1HGCM82633A004352"
T0_MS = 1_790_000_000_000  # 2026-09-21, inside the canonical ts range

_CLEARED = ("ROSETTA_JWT_SECRET", "ROSETTA_OIDC_JWKS_URL", "ROSETTA_DATABASE_URL",
            "OTEL_EXPORTER_OTLP_ENDPOINT", "ROSETTA_JWT_TTL_S", "ROSETTA_JWT_ISSUER",
            "ROSETTA_STATE_STORE", "ROSETTA_BROKER", "ROSETTA_ARCHIVE", "ROSETTA_TELEMETRY_STORE",
            "SIM_TRANSPORT", "ROSETTA_SERVICE")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Point the settings singleton at a temporary directory for every test."""
    monkeypatch.setenv("ROSETTA_ENV", "test")
    monkeypatch.setenv("ROSETTA_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    config.reset_settings()
    yield
    config.reset_settings()


@pytest.fixture()
def valid_event() -> Callable[..., dict[str, Any]]:
    """Factory for a canonical event that passes validation; keyword arguments override fields."""

    def make(**overrides: Any) -> dict[str, Any]:
        ev: dict[str, Any] = {
            "vin": VALID_VIN, "ts": T0_MS, "seq": 42, "lat": 12.9716, "lon": 77.5946,
            "speed_kmh": 54.2, "odo_km": 18234.5, "heading_deg": 181.0, "soc_pct": 76.5,
            "fuel_pct": 40.0, "ambient_c": 27.5, "ignition": True, "dtc": ["P0420"], "evt": "SPEEDING",
        }
        ev.update(overrides)
        return ev

    return make


def build_truth(vehicles: int = 300, seed: int = 11, steps: int = 30) -> dict[str, list]:
    """Ground truth for every vehicle of a small fleet after `steps` simulated seconds."""
    from rosetta.simulator.fleet import Fleet

    fleet = Fleet(vehicles, seed=seed, start_ms=T0_MS)
    for k in range(steps):
        fleet.step(1.0, T0_MS + (k + 1) * 1000)
    return fleet.truth(fleet.emitting("all"))


@pytest.fixture(scope="session")
def _truth_master() -> dict[str, list]:
    return build_truth()


@pytest.fixture()
def truth(_truth_master: dict[str, list]) -> dict[str, list]:
    """A private copy of the fleet truth, so a test may change it freely."""
    return copy.deepcopy(_truth_master)


@pytest.fixture()
def rng() -> np.random.Generator:
    return np.random.default_rng(20260930)
