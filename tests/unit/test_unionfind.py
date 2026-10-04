"""rosetta.algorithms.unionfind: disjoint sets and clustering of payload shapes."""
from __future__ import annotations

import numpy as np
import pytest

from rosetta.algorithms.unionfind import UnionFind, cluster_shapes, jaccard


def test_new_structure_is_empty():
    uf = UnionFind()
    assert uf.components == 0
    assert uf.groups() == []


def test_add_creates_singletons_once():
    uf = UnionFind()
    uf.add("a")
    uf.add("b")
    uf.add("a")
    assert uf.components == 2
    assert uf.find("a") == "a" and uf.find("b") == "b"


def test_find_adds_unknown_items():
    uf = UnionFind()
    assert uf.find("x") == "x"
    assert uf.components == 1


def test_union_joins_two_sets():
    uf = UnionFind()
    assert uf.union("a", "b") is True
    assert uf.find("a") == uf.find("b")
    assert uf.components == 1


def test_union_of_already_joined_items_returns_false():
    uf = UnionFind()
    uf.union("a", "b")
    assert uf.union("b", "a") is False
    assert uf.union("a", "a") is False
    assert uf.components == 1


def test_union_is_transitive():
    uf = UnionFind()
    uf.union(1, 2)
    uf.union(3, 4)
    assert uf.find(1) != uf.find(3)
    uf.union(2, 3)
    assert len({uf.find(x) for x in (1, 2, 3, 4)}) == 1
    assert uf.components == 1


def test_groups_are_sorted_largest_first():
    uf = UnionFind()
    for a, b in (("a", "b"), ("b", "c"), ("x", "y")):
        uf.union(a, b)
    uf.add("solo")
    groups = uf.groups()
    assert [sorted(g) for g in groups] == [["a", "b", "c"], ["x", "y"], ["solo"]]


def test_path_compression_points_items_at_the_root():
    uf = UnionFind()
    for i in range(1, 50):
        uf.union(i - 1, i)
    root = uf.find(0)
    for i in range(50):
        uf.find(i)
    assert all(uf.parent[i] == root for i in range(50))


def test_rank_keeps_trees_shallow():
    uf = UnionFind()
    for i in range(1, 1024):
        uf.union(0, i)
    assert max(uf.rank.values()) <= 10


def test_agrees_with_a_naive_implementation_on_random_unions():
    rng = np.random.default_rng(3)
    n = 60
    uf = UnionFind()
    label = list(range(n))
    for i in range(n):
        uf.add(i)
    for _ in range(80):
        a, b = (int(x) for x in rng.integers(0, n, 2))
        merged = uf.union(a, b)
        assert merged == (label[a] != label[b])
        if label[a] != label[b]:
            old, new = label[b], label[a]
            label = [new if x == old else x for x in label]
    assert uf.components == len(set(label))
    for i in range(n):
        for j in range(i + 1, n):
            assert (uf.find(i) == uf.find(j)) == (label[i] == label[j])
    assert sorted(len(g) for g in uf.groups()) == sorted(label.count(x) for x in set(label))


def test_works_with_any_hashable():
    uf = UnionFind()
    uf.union(frozenset({"a"}), ("t", 1))
    assert uf.find(("t", 1)) == uf.find(frozenset({"a"}))


# -------------------------------------------------------------------- jaccard
@pytest.mark.parametrize("a,b,expected", [
    ({"x"}, {"x"}, 1.0),
    ({"x"}, {"y"}, 0.0),
    ({"a", "b", "c"}, {"b", "c", "d"}, 0.5),
    ({"a", "b"}, {"a", "b", "c", "d"}, 0.5),
    (set(), set(), 1.0),
    (set(), {"a"}, 0.0),
])
def test_jaccard(a, b, expected):
    assert jaccard(frozenset(a), frozenset(b)) == expected
    assert jaccard(frozenset(b), frozenset(a)) == expected


# ------------------------------------------------------------- cluster_shapes
def sig(*paths: str) -> frozenset:
    return frozenset(paths)


def test_cluster_nothing():
    assert cluster_shapes([]) == []


def test_identical_signatures_collapse_into_one():
    s = sig("vin", "ts", "lat", "lon")
    assert cluster_shapes([s, s, s]) == [[s]]


def test_similar_shapes_form_one_family_and_different_ones_stay_apart():
    base = sig("vin", "ts", "lat", "lon", "speed", "odo", "soc", "fuel", "temp", "ign")
    renamed = sig("vin", "ts", "lat", "lon", "speed", "odo", "soc", "fuel", "temp", "ignition")   # 9 / 11
    other = sig("kopf.fin", "kopf.zeit", "ort.breite", "ort.laenge")
    groups = cluster_shapes([base, other, renamed])
    assert len(groups) == 2
    assert set(groups[0]) == {base, renamed}
    assert groups[1] == [other]


def test_threshold_is_inclusive():
    a, b = sig("a", "b", "c"), sig("b", "c", "d")     # similarity exactly 0.5
    assert len(cluster_shapes([a, b], threshold=0.5)) == 1
    assert len(cluster_shapes([a, b], threshold=0.51)) == 2


def test_families_are_joined_through_a_chain():
    a = sig("1", "2", "3", "4")
    b = sig("2", "3", "4", "5")       # 0.6 with a
    c = sig("3", "4", "5", "6")       # 0.6 with b, 0.33 with a
    groups = cluster_shapes([a, b, c], threshold=0.6)
    assert len(groups) == 1 and set(groups[0]) == {a, b, c}


def test_every_signature_appears_in_exactly_one_group():
    rng = np.random.default_rng(4)
    fields = [f"f{i}" for i in range(12)]
    sigs = [frozenset(str(x) for x in rng.choice(fields, size=int(rng.integers(3, 10)), replace=False))
            for _ in range(40)]
    groups = cluster_shapes(sigs, threshold=0.7)
    flat = [s for g in groups for s in g]
    assert len(flat) == len(set(flat)) == len(set(sigs))
    assert [len(g) for g in groups] == sorted((len(g) for g in groups), reverse=True)


def test_accepts_a_generator():
    groups = cluster_shapes(s for s in (sig("a"), sig("b")))
    assert len(groups) == 2
