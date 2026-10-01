"""rosetta.algorithms.bloom: Bloom filter and its rotating, memory-bounded variant."""
from __future__ import annotations

import math

import pytest

from rosetta.algorithms.bloom import BloomFilter, RotatingBloom


def keys(prefix: str, n: int) -> list[bytes]:
    return [f"{prefix}:{i}".encode() for i in range(n)]


@pytest.mark.parametrize("capacity,fp_rate", [(0, 0.01), (-5, 0.01), (100, 0.0), (100, 1.0), (100, -0.1), (100, 1.5)])
def test_invalid_parameters_are_rejected(capacity, fp_rate):
    with pytest.raises(ValueError):
        BloomFilter(capacity, fp_rate)


def test_sizing_follows_the_textbook_formulas():
    bf = BloomFilter(10_000, 0.01)
    assert bf.m == math.ceil(-10_000 * math.log(0.01) / math.log(2) ** 2) == 95_851
    assert bf.k == 7
    assert bf.size_bytes == (bf.m + 7) // 8


def test_tiny_filter_has_a_minimum_size():
    bf = BloomFilter(1, 0.5)
    assert bf.m == 64 and bf.k >= 1


def test_empty_filter_contains_nothing():
    bf = BloomFilter(1000)
    assert all(k not in bf for k in keys("x", 200))
    assert bf.count == 0
    assert bf.estimated_fp_rate() == 0.0


def test_no_false_negatives():
    bf = BloomFilter(5_000, 0.01)
    inserted = keys("vin", 5_000)
    for k in inserted:
        bf.add(k)
    assert all(k in bf for k in inserted)


def test_no_false_negatives_even_when_overfilled():
    bf = BloomFilter(100, 0.01)
    inserted = keys("vin", 2_000)
    for k in inserted:
        bf.add(k)
    assert all(k in bf for k in inserted)


def test_add_reports_whether_the_key_was_possibly_present():
    bf = BloomFilter(1000)
    assert bf.add(b"a") is False
    assert bf.add(b"a") is True
    assert bf.count == 1, "a repeated key is not counted twice"


def test_measured_false_positive_rate_is_close_to_the_target():
    target = 0.01
    bf = BloomFilter(10_000, target)
    for k in keys("in", 10_000):
        bf.add(k)
    probes = keys("out", 50_000)
    measured = sum(1 for k in probes if k in bf) / len(probes)
    assert 0.003 < measured < 0.02, measured
    assert bf.estimated_fp_rate() == pytest.approx(target, rel=0.25)


def test_lower_target_gives_fewer_false_positives_and_more_bits():
    loose, tight = BloomFilter(5_000, 0.05), BloomFilter(5_000, 0.001)
    for k in keys("in", 5_000):
        loose.add(k)
        tight.add(k)
    probes = keys("out", 20_000)
    fp_loose = sum(1 for k in probes if k in loose)
    fp_tight = sum(1 for k in probes if k in tight)
    assert fp_tight < fp_loose
    assert tight.size_bytes > loose.size_bytes


def test_positions_are_deterministic_and_inside_the_bit_array():
    bf = BloomFilter(1000)
    pos = bf._positions(b"1HGCM82633A004352:42")
    assert pos == bf._positions(b"1HGCM82633A004352:42")
    assert len(pos) == bf.k
    assert all(0 <= p < bf.m for p in pos)


# ------------------------------------------------------------------- rotating
def test_rotating_bloom_remembers_keys_of_the_current_generation():
    rb = RotatingBloom(100)
    assert rb.add(b"a") is False
    assert b"a" in rb
    assert rb.add(b"a") is True
    assert b"b" not in rb


def test_rotation_happens_after_capacity_inserts():
    rb = RotatingBloom(50)
    first = rb.current
    for k in keys("g1", 50):
        rb.add(k)
    assert rb.previous is first, "the filled generation becomes the previous one"
    assert rb.current is not first and rb.current.count == 0


def test_keys_survive_one_rotation_but_not_two():
    rb = RotatingBloom(50, fp_rate=0.001)
    gen1 = keys("g1", 50)
    for k in gen1:
        rb.add(k)                       # fills generation 1 and rotates
    assert all(k in rb for k in gen1), "still visible through the previous generation"
    for k in keys("g2", 50):
        rb.add(k)                       # rotates again, generation 1 is dropped
    survivors = sum(1 for k in gen1 if k in rb)
    assert survivors <= 2, "only false positives may remain"


def test_add_sees_a_key_held_by_the_previous_generation():
    rb = RotatingBloom(10, fp_rate=0.001)
    for k in keys("g1", 10):
        rb.add(k)
    assert rb.add(b"g1:3") is True


def test_memory_stays_bounded_on_a_long_stream():
    rb = RotatingBloom(200)
    size = rb.current.size_bytes
    for k in keys("stream", 5_000):
        rb.add(k)
    assert rb.current.size_bytes == rb.previous.size_bytes == size
    assert rb.current.count < 200


def test_repeated_keys_do_not_trigger_rotation():
    rb = RotatingBloom(10)
    first = rb.current
    for _ in range(100):
        rb.add(b"same")
    assert rb.current is first
