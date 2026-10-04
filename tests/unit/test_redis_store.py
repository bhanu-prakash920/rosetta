"""rosetta.adapters.redis_store: exact membership for late events, kept in Redis."""
from __future__ import annotations

import fakeredis
import pytest

from rosetta.adapters.redis_store import RedisExactStore
from rosetta.algorithms.replay_window import WINDOW
from rosetta.engine.dedup import Deduplicator

VIN = "1HGCM82633A004352"


@pytest.fixture()
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


def test_key_is_stored_once(client):
    store = RedisExactStore("redis://unused", client=client)
    assert store.add_if_absent(f"{VIN}:7") is True
    assert store.add_if_absent(f"{VIN}:7") is False
    assert store.add_if_absent(f"{VIN}:8") is True


def test_result_is_a_plain_bool(client):
    store = RedisExactStore("redis://unused", client=client)
    assert type(store.add_if_absent("k")) is bool
    assert type(store.add_if_absent("k")) is bool


def test_keys_are_namespaced(client):
    RedisExactStore("redis://unused", client=client).add_if_absent(f"{VIN}:7")
    assert client.keys("*") == [f"rosetta:late:{VIN}:7".encode()]


def test_keys_expire(client):
    RedisExactStore("redis://unused", ttl_s=600, client=client).add_if_absent("k")
    assert 0 < client.ttl("rosetta:late:k") <= 600


def test_default_lifetime_is_two_days(client):
    store = RedisExactStore("redis://unused", client=client)
    store.add_if_absent("k")
    assert store.ttl_s == 172_800
    assert 172_000 < client.ttl("rosetta:late:k") <= 172_800


def test_a_repeat_does_not_extend_the_lifetime(client):
    store = RedisExactStore("redis://unused", ttl_s=600, client=client)
    store.add_if_absent("k")
    client.expire("rosetta:late:k", 30)
    store.add_if_absent("k")
    assert client.ttl("rosetta:late:k") <= 30


def test_key_is_accepted_again_after_it_expired(client):
    store = RedisExactStore("redis://unused", ttl_s=600, client=client)
    store.add_if_absent("k")
    client.delete("rosetta:late:k")                      # what expiry does
    assert store.add_if_absent("k") is True


def test_two_workers_share_one_store(client):
    a = RedisExactStore("redis://unused", client=client)
    b = RedisExactStore("redis://unused", client=client)
    assert a.add_if_absent("k") is True
    assert b.add_if_absent("k") is False


def test_client_is_built_from_the_url_without_connecting():
    store = RedisExactStore("redis://localhost:6399/3")
    kwargs = store.r.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("localhost", 6399, 3)


def test_deduplicator_uses_it_for_late_events(client):
    d = Deduplicator(exact=RedisExactStore("redis://unused", client=client), late_capacity=1000)
    d.is_duplicate(VIN, 5000)
    late = 5000 - WINDOW - 1
    assert d.is_duplicate(VIN, late) is False
    assert d.is_duplicate(VIN, late) is True
    assert client.exists(f"rosetta:late:{VIN}:{late}") == 1
