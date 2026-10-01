"""Union-Find (disjoint sets) and shape clustering for dead-lettered payloads.

When thousands of messages fail, the operator should see "3 kinds of problem",
not 40,000 rows. Each payload gets a structural signature (the set of field
paths it contains). Signatures that overlap strongly are joined into one
family, which is the connected-components problem on a similarity graph.

find / union: amortised O(alpha(n)), effectively constant.
cluster_shapes: O(s^2 * f) for s distinct signatures of f fields. s is small
(tens) because millions of messages share a handful of shapes.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable


class UnionFind:
    __slots__ = ("parent", "rank", "components")

    def __init__(self) -> None:
        self.parent: dict[Hashable, Hashable] = {}
        self.rank: dict[Hashable, int] = {}
        self.components = 0

    def add(self, x: Hashable) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
            self.components += 1

    def find(self, x: Hashable) -> Hashable:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Hashable, b: Hashable) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        self.components -= 1
        return True

    def groups(self) -> list[list[Hashable]]:
        out: dict[Hashable, list[Hashable]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return sorted(out.values(), key=lambda g: (-len(g), str(g[0])))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def cluster_shapes(signatures: Iterable[frozenset], threshold: float = 0.7) -> list[list[frozenset]]:
    """Group structural signatures whose Jaccard similarity is at least `threshold`."""
    sigs = list(dict.fromkeys(signatures))
    uf = UnionFind()
    for s in sigs:
        uf.add(s)
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            if jaccard(sigs[i], sigs[j]) >= threshold:
                uf.union(sigs[i], sigs[j])
    return uf.groups()
