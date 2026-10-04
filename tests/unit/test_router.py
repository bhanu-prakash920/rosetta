"""rosetta.engine.router: which mapping version tries a message, and in which order."""
from __future__ import annotations

import copy
from collections import Counter

import pytest

from rosetta.engine.router import (
    ALL_STATES,
    LIVE_STATES,
    ST_ACTIVE,
    ST_CANARY,
    MappingRow,
    OemRoutes,
    RoutingTable,
    canary_bucket,
)
from rosetta.simulator.dialects import SPEC_NORDVIK, SPEC_PACIFICA, SPEC_PACIFICA_V2

BAD_SPEC = {"decoder": {"type": "json"}, "fields": {"vin": {"path": "vin"}}}     # required fields missing


def row(version: int, state: str = ST_ACTIVE, oem: str = "pacifica", spec=SPEC_PACIFICA, pct: int = 0) -> MappingRow:
    return MappingRow(oem, version, state, spec, pct)


def device_in_bucket(pred) -> bytes:
    for i in range(10_000):
        key = f"pa-{i:07d}".encode()
        if pred(canary_bucket(key)):
            return key
    raise AssertionError("no device found")


def versions(adapters) -> list[int]:
    return [a.version for a in adapters]


# --------------------------------------------------------------- canary_bucket
def test_bucket_is_between_0_and_99():
    assert all(0 <= canary_bucket(f"dev-{i}".encode()) <= 99 for i in range(5000))


def test_bucket_is_stable_per_device():
    keys = [f"pa-{i:07d}".encode() for i in range(200)]
    first = [canary_bucket(k) for k in keys]
    assert [canary_bucket(k) for k in keys] == first
    assert [canary_bucket(bytes(bytearray(k))) for k in keys] == first


def test_bucket_values_are_pinned():
    # The hash and its seed are part of the contract: a vehicle must stay in its
    # group across restarts and releases.
    import xxhash

    for key in (b"pa-0000001", b"hx-0000012", b""):
        assert canary_bucket(key) == xxhash.xxh3_64_intdigest(key, seed=0x5EED) % 100


def test_buckets_are_spread_evenly():
    counts = Counter(canary_bucket(f"pa-{i:07d}".encode()) for i in range(20_000))
    assert len(counts) == 100
    assert 140 <= min(counts.values()) and max(counts.values()) <= 260


@pytest.mark.parametrize("pct", [1, 10, 50, 90])
def test_canary_share_matches_the_percentage(pct):
    inside = sum(canary_bucket(f"pa-{i:07d}".encode()) < pct for i in range(20_000))
    assert inside / 20_000 == pytest.approx(pct / 100, abs=0.015)


# ---------------------------------------------------------------- RoutingTable
def test_empty_table():
    t = RoutingTable([])
    assert t.routes == {} and t.errors == [] and t.epoch == 0
    assert t.for_oem("pacifica") is None
    assert t.describe() == {}


def test_epoch_is_kept():
    assert RoutingTable([], epoch=42).epoch == 42


def test_only_live_states_are_loaded():
    rows = [row(i + 1, state) for i, state in enumerate(ALL_STATES)]
    t = RoutingTable(rows)
    live = {r.version for r in rows if r.state in LIVE_STATES}
    assert set(t.for_oem("pacifica").by_version) == live
    assert set(LIVE_STATES) == {"canary", "active"}


def test_source_without_live_versions_has_no_routes():
    t = RoutingTable([row(1, "draft"), row(2, "retired"), row(3, "rejected"), row(4, "validated")])
    assert t.for_oem("pacifica") is None


def test_active_versions_are_ordered_newest_first():
    t = RoutingTable([row(2), row(7), row(5)])
    assert versions(t.for_oem("pacifica").active) == [7, 5, 2]


def test_sources_are_kept_apart():
    t = RoutingTable([row(1), row(1, oem="nordvik", spec=SPEC_NORDVIK), row(2, oem="nordvik", spec=SPEC_NORDVIK)])
    assert versions(t.for_oem("pacifica").active) == [1]
    assert versions(t.for_oem("nordvik").active) == [2, 1]
    assert t.for_oem("pacifica").active[0].oem == "pacifica"


def test_single_is_set_only_when_one_version_is_live():
    assert RoutingTable([row(3)]).for_oem("pacifica").single.version == 3
    assert RoutingTable([row(3), row(4)]).for_oem("pacifica").single is None
    assert RoutingTable([row(3), row(4, ST_CANARY, pct=10)]).for_oem("pacifica").single is None
    assert RoutingTable([row(4, ST_CANARY, pct=10)]).for_oem("pacifica").single is None


def test_canary_and_its_percentage():
    r = RoutingTable([row(1), row(2, ST_CANARY, spec=SPEC_PACIFICA_V2, pct=25)]).for_oem("pacifica")
    assert r.canary.version == 2 and r.canary_pct == 25
    assert versions(r.active) == [1]
    assert set(r.by_version) == {1, 2}


@pytest.mark.parametrize("given,kept", [(-5, 0), (0, 0), (100, 100), (250, 100), ("30", 30), (12.9, 12)])
def test_canary_percentage_is_clamped(given, kept):
    r = RoutingTable([row(1), row(2, ST_CANARY, pct=given)]).for_oem("pacifica")
    assert r.canary_pct == kept


def test_newest_canary_wins_when_there_are_two():
    r = RoutingTable([row(1), row(2, ST_CANARY, pct=10), row(3, ST_CANARY, pct=40)]).for_oem("pacifica")
    assert r.canary.version == 3 and r.canary_pct == 40
    assert set(r.by_version) == {1, 2, 3}


def test_adapters_are_compiled_and_usable(truth):
    from rosetta.simulator.dialects import DIALECTS

    r = RoutingTable([row(4)]).for_oem("pacifica")
    ev = r.single.normalize(DIALECTS["pacifica"].encode(truth)[0])
    assert ev["vin"] == truth["vin"][0] and ev["map_v"] == 4


def test_spec_that_does_not_compile_is_skipped_and_reported():
    t = RoutingTable([row(1), row(2, spec=BAD_SPEC), row(3, ST_CANARY, spec={"fields": {}}, pct=50)])
    r = t.for_oem("pacifica")
    assert versions(r.active) == [1] and r.canary is None
    assert r.single.version == 1
    assert len(t.errors) == 2
    assert any(e.startswith("pacifica v2: ") and "required canonical fields" in e for e in t.errors)
    assert any(e.startswith("pacifica v3: ") for e in t.errors)


def test_source_whose_only_version_is_broken_has_no_routes():
    t = RoutingTable([row(1, spec=BAD_SPEC)])
    assert t.for_oem("pacifica") is None
    assert len(t.errors) == 1


def test_spec_with_a_malformed_transform_is_skipped_and_reported():
    broken = copy.deepcopy(SPEC_PACIFICA)
    broken["fields"]["ts"]["transforms"] = [{"op": ["unit"]}]
    t = RoutingTable([row(1), row(2, spec=broken)])
    assert versions(t.for_oem("pacifica").active) == [1]
    assert len(t.errors) == 1


def test_describe():
    t = RoutingTable([row(1), row(2), row(3, ST_CANARY, pct=20), row(1, oem="nordvik", spec=SPEC_NORDVIK)])
    assert t.describe() == {
        "nordvik": {"active": [1], "canary": None, "canary_pct": 0},
        "pacifica": {"active": [2, 1], "canary": 3, "canary_pct": 20},
    }


# ------------------------------------------------------------------ candidates
@pytest.fixture()
def routes() -> OemRoutes:
    """Active versions 1 and 2, canary version 3 for half of the fleet."""
    return RoutingTable([row(1), row(2), row(3, ST_CANARY, spec=SPEC_PACIFICA_V2, pct=50)]).for_oem("pacifica")


IN_CANARY = device_in_bucket(lambda b: b < 50)
NOT_IN_CANARY = device_in_bucket(lambda b: b >= 50)


def test_device_outside_the_canary_group_gets_actives_then_canary_as_last_resort(routes):
    assert versions(routes.candidates(NOT_IN_CANARY, None)) == [2, 1, 3]


def test_device_inside_the_canary_group_gets_the_canary_first(routes):
    assert versions(routes.candidates(IN_CANARY, None)) == [3, 2, 1]


def test_sticky_version_comes_first(routes):
    assert versions(routes.candidates(NOT_IN_CANARY, 1)) == [1, 2, 3]
    assert versions(routes.candidates(NOT_IN_CANARY, 2)) == [2, 1, 3]


def test_sticky_then_canary_then_actives_for_a_canary_device(routes):
    assert versions(routes.candidates(IN_CANARY, 1)) == [1, 3, 2]


def test_sticky_canary_is_not_listed_twice(routes):
    assert versions(routes.candidates(IN_CANARY, 3)) == [3, 2, 1]
    assert versions(routes.candidates(NOT_IN_CANARY, 3)) == [3, 2, 1]


def test_unknown_sticky_version_is_ignored(routes):
    assert versions(routes.candidates(NOT_IN_CANARY, 99)) == [2, 1, 3]
    assert versions(routes.candidates(IN_CANARY, 99)) == [3, 2, 1]


def test_every_live_version_is_a_candidate_exactly_once(routes):
    for key in (IN_CANARY, NOT_IN_CANARY):
        for sticky in (None, 1, 2, 3, 99):
            assert sorted(versions(routes.candidates(key, sticky))) == [1, 2, 3]


def test_candidates_are_the_same_for_every_message_of_a_device(routes):
    first = versions(routes.candidates(IN_CANARY, None))
    assert all(versions(routes.candidates(IN_CANARY, None)) == first for _ in range(20))


@pytest.mark.parametrize("pct,expected_first", [(0, 2), (100, 3)])
def test_canary_percentage_extremes(pct, expected_first):
    r = RoutingTable([row(2), row(3, ST_CANARY, pct=pct)]).for_oem("pacifica")
    for i in range(200):
        assert routes_first(r, f"pa-{i:07d}".encode()) == expected_first


def routes_first(r: OemRoutes, key: bytes) -> int:
    return r.candidates(key, None)[0].version


def test_canary_group_size_follows_the_percentage():
    r = RoutingTable([row(2), row(3, ST_CANARY, pct=30)]).for_oem("pacifica")
    first = Counter(routes_first(r, f"pa-{i:07d}".encode()) for i in range(5000))
    assert first[3] / 5000 == pytest.approx(0.30, abs=0.03)


def test_canary_only_source():
    r = RoutingTable([row(3, ST_CANARY, pct=10)]).for_oem("pacifica")
    assert versions(r.candidates(IN_CANARY, None)) == [3]
    assert versions(r.candidates(NOT_IN_CANARY, None)) == [3]


def test_no_canary():
    r = RoutingTable([row(1), row(2)]).for_oem("pacifica")
    assert versions(r.candidates(IN_CANARY, None)) == [2, 1]
    assert versions(r.candidates(IN_CANARY, 1)) == [1, 2]
