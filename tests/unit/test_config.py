"""rosetta.config: settings come from the environment and nowhere else."""
from __future__ import annotations

from pathlib import Path

import pytest

from rosetta import config
from rosetta.config import Settings, get_settings, reset_settings


def test_defaults_are_the_local_mode(monkeypatch, tmp_path):
    for name in list(__import__("os").environ):
        if name.startswith("ROSETTA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    reset_settings()
    st = get_settings()
    assert st.env == "local" and st.broker == "log" and st.state_store == "memory" and st.archive == "fs"
    assert st.data_dir == (tmp_path / "data").resolve()
    assert (st.raw_partitions, st.normalizers, st.processors, st.vehicles) == (8, 4, 2, 100_000)
    assert st.jwt_secret == "" and st.jwt_ttl_s == 3600 and st.jwt_issuer == "rosetta"
    assert st.embed_pipeline is False and st.llm_provider == "auto"
    assert st.golden_pass_rate == pytest.approx(0.99)
    assert not (tmp_path / "data").exists(), "reading the settings creates nothing"


def test_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("ROSETTA_ENV", "production")
    monkeypatch.setenv("ROSETTA_DATA_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("ROSETTA_BROKER", "kafka")
    monkeypatch.setenv("ROSETTA_RAW_PARTITIONS", "32")
    monkeypatch.setenv("ROSETTA_EMBED_PIPELINE", "YES")
    monkeypatch.setenv("ROSETTA_GOLDEN_PASS_RATE", "0.5")
    reset_settings()
    st = get_settings()
    assert (st.env, st.broker, st.raw_partitions, st.embed_pipeline, st.golden_pass_rate) == \
        ("production", "kafka", 32, True, 0.5)
    assert st.data_dir == (tmp_path / "elsewhere").resolve()


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("True", True), (" on ", True), ("yes", True),
                                            ("0", False), ("false", False), ("", False), ("nope", False)])
def test_bool_variables(monkeypatch, value, expected):
    monkeypatch.setenv("ROSETTA_EMBED_PIPELINE", value)
    reset_settings()
    assert get_settings().embed_pipeline is expected


def test_bad_integer_is_an_error(monkeypatch):
    monkeypatch.setenv("ROSETTA_VEHICLES", "many")
    reset_settings()
    with pytest.raises(ValueError):
        get_settings()


def test_settings_are_cached_until_reset(monkeypatch):
    monkeypatch.setenv("ROSETTA_VEHICLES", "10")
    reset_settings()
    first = get_settings()
    monkeypatch.setenv("ROSETTA_VEHICLES", "20")
    assert get_settings() is first and get_settings().vehicles == 10
    reset_settings()
    assert get_settings().vehicles == 20


def test_settings_are_immutable():
    st = get_settings()
    with pytest.raises(Exception):
        st.vehicles = 5


def test_derived_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("ROSETTA_DATA_DIR", str(tmp_path / "d"))
    reset_settings()
    st = get_settings()
    assert st.log_dir == st.data_dir / "log" and st.archive_dir == st.data_dir / "archive"
    assert not st.log_dir.exists()


def test_sqlite_is_the_default_database_and_lives_in_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ROSETTA_DATA_DIR", str(tmp_path / "d"))
    reset_settings()
    st = get_settings()
    assert st.db_url == f"sqlite:///{(tmp_path / 'd').resolve() / 'rosetta.db'}"
    assert st.is_sqlite is True
    assert (tmp_path / "d").is_dir(), "asking for the database creates its directory"


def test_explicit_database_url(monkeypatch):
    monkeypatch.setenv("ROSETTA_DATABASE_URL", "postgresql://db/rosetta")
    reset_settings()
    st = get_settings()
    assert st.db_url == "postgresql://db/rosetta" and st.is_sqlite is False


def test_settings_can_be_built_directly():
    st = Settings(env="test", vehicles=3, data_dir=Path("/nowhere"))
    assert (st.env, st.vehicles, st.data_dir) == ("test", 3, Path("/nowhere"))
    assert config._int("ROSETTA_DOES_NOT_EXIST", 7) == 7 and config._env("ROSETTA_DOES_NOT_EXIST", "d") == "d"
