"""rosetta.engine.dedup: exact window first, Bloom-guarded exact store for late events."""
from __future__ import annotations

from rosetta.algorithms.replay_window import WINDOW
from rosetta.engine.dedup import Deduplicator, MemoryExactStore

VIN = "1HGCM82633A004352"


class RecordingStore:
    """An exact store that remembers how it was used."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.calls: list[str] = []

    def add_if_absent(self, key: str) -> bool:
        self.calls.append(key)
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class AlwaysMaybe:
    """A Bloom filter at its worst: it claims to have seen everything."""

    def add(self, key: bytes) -> bool:
        return True


# ---------------------------------------------------------- MemoryExactStore
def test_memory_store_adds_each_key_once():
    store = MemoryExactStore()
    assert store.add_if_absent("a") is True
    assert store.add_if_absent("a") is False
    assert store.add_if_absent("b") is True
    assert len(store) == 2


def test_memory_store_drops_the_oldest_tenth_when_full():
    store = MemoryExactStore(max_items=20)
    for i in range(20):
        store.add_if_absent(f"k{i}")
    assert store.add_if_absent("k20") is True
    assert len(store) == 19
    assert store.add_if_absent("k0") is True, "k0 and k1 were evicted"
    assert store.add_if_absent("k2") is False, "k2 survived"
    assert store.add_if_absent("k20") is False


def test_memory_store_stays_bounded():
    store = MemoryExactStore(max_items=50)
    for i in range(1000):
        store.add_if_absent(f"k{i}")
    assert len(store) <= 50


# --------------------------------------------------------------- Deduplicator
def test_first_event_is_not_a_duplicate():
    d = Deduplicator(late_capacity=100)
    assert d.is_duplicate(VIN, 10) is False


def test_repeat_inside_the_window_is_a_duplicate():
    d = Deduplicator(late_capacity=100)
    d.is_duplicate(VIN, 10)
    assert d.is_duplicate(VIN, 10) is True
    assert d.late_seen == 0, "the exact window answered, the late path was not used"


def test_out_of_order_event_is_accepted_once():
    d = Deduplicator(late_capacity=100)
    d.is_duplicate(VIN, 10)
    assert d.is_duplicate(VIN, 8) is False
    assert d.is_duplicate(VIN, 8) is True


def test_vehicles_do_not_interfere():
    d = Deduplicator(late_capacity=100)
    d.is_duplicate(VIN, 10)
    assert d.is_duplicate("11111111111111111", 10) is False


def test_late_event_is_accepted_the_first_time_and_dropped_the_second_time():
    store = RecordingStore()
    d = Deduplicator(exact=store, late_capacity=1000)
    d.is_duplicate(VIN, 1000)
    late = 1000 - WINDOW
    assert d.is_duplicate(VIN, late) is False
    assert d.is_duplicate(VIN, late) is True
    assert d.is_duplicate(VIN, late) is True
    assert d.late_seen == 3
    assert d.bloom_hits == 2 and d.exact_lookups == 2
    assert store.keys == {f"{VIN}:{late}"}


def test_different_late_events_are_all_accepted():
    d = Deduplicator(exact=RecordingStore(), late_capacity=1000)
    d.is_duplicate(VIN, 10_000)
    assert [d.is_duplicate(VIN, s) for s in range(100, 200)] == [False] * 100
    assert [d.is_duplicate(VIN, s) for s in range(100, 200)] == [True] * 100


def test_same_late_sequence_of_two_vehicles_is_not_confused():
    d = Deduplicator(exact=RecordingStore(), late_capacity=1000)
    other = "11111111111111111"
    d.is_duplicate(VIN, 5000)
    d.is_duplicate(other, 5000)
    assert d.is_duplicate(VIN, 7) is False
    assert d.is_duplicate(other, 7) is False
    assert d.is_duplicate(VIN, 7) is True


def test_bloom_false_positive_never_loses_an_event():
    store = RecordingStore()
    d = Deduplicator(exact=store, late_capacity=1000)
    d.bloom = AlwaysMaybe()
    d.is_duplicate(VIN, 1000)
    assert d.is_duplicate(VIN, 5) is False, "the exact store has the final word"
    assert d.is_duplicate(VIN, 5) is True
    assert d.bloom_hits == 2 and d.exact_lookups == 2


def test_bloom_spares_the_exact_lookup_for_new_late_events():
    d = Deduplicator(exact=RecordingStore(), late_capacity=10_000, fp_rate=0.001)
    d.is_duplicate(VIN, 100_000)
    for seq in range(500):
        d.is_duplicate(VIN, seq)
    assert d.late_seen == 500
    assert d.exact_lookups <= 5, "only Bloom false positives cost a lookup"


def test_default_exact_store_is_in_memory():
    assert isinstance(Deduplicator(late_capacity=10).exact, MemoryExactStore)


def test_a_store_with_content_is_used_as_given():
    store = MemoryExactStore(max_items=10)
    store.add_if_absent("seed")
    assert Deduplicator(exact=store, late_capacity=10).exact is store


def test_an_empty_memory_store_is_used_as_given():
    store = MemoryExactStore(max_items=10)
    assert Deduplicator(exact=store, late_capacity=10).exact is store


def test_snapshot_and_restore_keep_the_window():
    d = Deduplicator(late_capacity=100)
    for seq in (5, 6, 9):
        d.is_duplicate(VIN, seq)
    snap = d.snapshot()
    assert snap == {"window": {VIN: (9, 0b11001)}}

    restarted = Deduplicator(late_capacity=100)
    restarted.restore(snap)
    assert restarted.is_duplicate(VIN, 9) is True
    assert restarted.is_duplicate(VIN, 6) is True
    assert restarted.is_duplicate(VIN, 7) is False
    assert restarted.is_duplicate(VIN, 10) is False


def test_restore_of_an_empty_snapshot_forgets_everything():
    d = Deduplicator(late_capacity=100)
    d.is_duplicate(VIN, 5)
    d.restore({})
    assert d.is_duplicate(VIN, 5) is False
