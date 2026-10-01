"""rosetta.algorithms.countmin: frequency estimates and heavy hitters in fixed memory."""
from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pytest

from rosetta.algorithms.countmin import CountMinSketch, TopK


def skewed_stream(seed: int, n: int, distinct: int) -> list[str]:
    rng = np.random.default_rng(seed)
    ranks = np.minimum(rng.zipf(1.3, size=n), distinct) - 1
    return [f"field-{r}" for r in ranks]


@pytest.mark.parametrize("eps,delta", [(0, 0.01), (1, 0.01), (-0.1, 0.01), (0.01, 0), (0.01, 1), (0.01, 2)])
def test_invalid_parameters_are_rejected(eps, delta):
    with pytest.raises(ValueError):
        CountMinSketch(eps, delta)


def test_dimensions_follow_the_formulas():
    s = CountMinSketch(eps=0.001, delta=0.01)
    assert s.width == math.ceil(math.e / 0.001) == 2719
    assert s.depth == math.ceil(math.log(100)) == 5
    assert s.table.shape == (5, 2719)


def test_unknown_key_is_estimated_as_zero_in_an_empty_sketch():
    assert CountMinSketch().estimate("nothing") == 0


def test_add_returns_the_new_estimate():
    s = CountMinSketch()
    assert s.add("a") == 1
    assert s.add("a", 4) == 5
    assert s.estimate("a") == 5
    assert s.total == 5


def test_estimate_never_undercounts():
    s = CountMinSketch(eps=0.01, delta=0.05)          # deliberately small: collisions happen
    stream = skewed_stream(seed=3, n=20_000, distinct=3_000)
    truth = Counter(stream)
    for key in stream:
        s.add(key)
    assert s.total == len(stream)
    assert all(s.estimate(k) >= c for k, c in truth.items())


def test_overcount_stays_within_the_error_bound():
    eps, delta = 0.005, 0.01
    s = CountMinSketch(eps, delta)
    stream = skewed_stream(seed=4, n=20_000, distinct=5_000)
    truth = Counter(stream)
    for key in stream:
        s.add(key)
    outside = sum(1 for k, c in truth.items() if s.estimate(k) - c > eps * len(stream))
    assert outside / len(truth) <= delta


def test_weighted_add_equals_repeated_add():
    a, b = CountMinSketch(0.01, 0.01), CountMinSketch(0.01, 0.01)
    for _ in range(7):
        a.add("x")
    b.add("x", 7)
    assert np.array_equal(a.table, b.table)
    assert a.total == b.total == 7


def test_merge_adds_two_sketches():
    left, right, whole = (CountMinSketch(0.01, 0.01) for _ in range(3))
    stream = skewed_stream(seed=5, n=4_000, distinct=500)
    for i, key in enumerate(stream):
        (left if i % 2 else right).add(key)
        whole.add(key)
    left.merge(right)
    assert np.array_equal(left.table, whole.table)
    assert left.total == whole.total == len(stream)
    assert left.estimate("field-0") == whole.estimate("field-0")


def test_merge_leaves_the_other_sketch_untouched():
    a, b = CountMinSketch(0.01, 0.01), CountMinSketch(0.01, 0.01)
    a.add("x", 3)
    b.add("x", 2)
    a.merge(b)
    assert a.estimate("x") == 5 and b.estimate("x") == 2


def test_merge_rejects_a_different_shape():
    with pytest.raises(ValueError, match="same shape"):
        CountMinSketch(0.01, 0.01).merge(CountMinSketch(0.001, 0.01))


# ----------------------------------------------------------------------- TopK
def test_topk_keeps_everything_while_below_k():
    t = TopK(k=5)
    for key, n in (("a", 3), ("b", 1), ("c", 2)):
        t.add(key, n)
    assert t.items() == [("a", 3), ("c", 2), ("b", 1)]


def test_topk_orders_by_count_then_key():
    t = TopK(k=4)
    for key in ("b", "a", "d", "c"):
        t.add(key, 2)
    assert t.items() == [("a", 2), ("b", 2), ("c", 2), ("d", 2)]


def test_topk_evicts_the_smallest_member():
    t = TopK(k=2)
    t.add("small", 1)
    t.add("medium", 5)
    t.add("large", 9)
    assert [k for k, _ in t.items()] == ["large", "medium"]


def test_topk_does_not_admit_a_key_that_is_not_larger_than_the_minimum():
    t = TopK(k=2)
    t.add("a", 5)
    t.add("b", 5)
    t.add("c", 5)
    assert [k for k, _ in t.items()] == ["a", "b"]


def test_topk_member_counts_keep_growing():
    t = TopK(k=2)
    t.add("a")
    t.add("b")
    for _ in range(10):
        t.add("a")
    assert dict(t.items())["a"] == 11
    t.add("c", 5)                       # the stale heap entry for "a" must not get "a" evicted
    assert [k for k, _ in t.items()] == ["a", "c"]


def test_topk_never_holds_more_than_k_keys():
    t = TopK(k=10)
    for key in skewed_stream(seed=6, n=5_000, distinct=2_000):
        t.add(key)
    assert len(t.items()) == 10


def test_topk_finds_the_true_heavy_hitters_on_a_skewed_stream():
    stream = skewed_stream(seed=7, n=30_000, distinct=10_000)
    truth = Counter(stream)
    t = TopK(k=10)
    for key in stream:
        t.add(key)
    found = [k for k, _ in t.items()]
    true_top5 = [k for k, _ in truth.most_common(5)]
    assert found[:5] == true_top5
    assert set(found) >= {k for k, _ in truth.most_common(8)}
    for key, est in t.items():
        assert est >= truth[key], "estimates never undercount"
        assert est - truth[key] <= 0.001 * len(stream)


def test_topk_finds_a_heavy_hitter_that_arrives_late():
    t = TopK(k=3)
    for i in range(300):
        t.add(f"noise-{i}")
    for _ in range(50):
        t.add("late-but-frequent")
    assert t.items()[0] == ("late-but-frequent", 50)
