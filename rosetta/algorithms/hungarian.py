"""Hungarian algorithm (Kuhn-Munkres with potentials) for field matching.

Problem: the classifier scores every (source field, canonical field) pair.
Picking the best canonical field for each source field independently can map
two source fields to the same target. We need the one-to-one assignment with
the highest total score, which is maximum-weight bipartite matching.

Time O(n^2 * m) for n rows and m columns (n <= m). Space O(n * m).
"""
from __future__ import annotations

import numpy as np

_INF = float("inf")


def min_cost_assignment(cost: np.ndarray) -> list[tuple[int, int]]:
    """Assign every row to a distinct column with minimum total cost. Needs rows <= cols."""
    cost = np.asarray(cost, dtype=np.float64)
    if cost.ndim != 2:
        raise ValueError("cost must be a 2-D matrix")
    n, m = cost.shape
    if n == 0:
        return []
    if n > m:
        raise ValueError("needs rows <= columns; transpose the matrix")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)       # p[j] = row matched to column j (1-based, 0 = none)
    way = [0] * (m + 1)
    c = cost.tolist()
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [_INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], _INF, 0
            row = c[i0 - 1]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = row[j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return sorted((p[j] - 1, j - 1) for j in range(1, m + 1) if p[j] != 0)


def max_score_assignment(score: np.ndarray, min_score: float = 0.0) -> list[tuple[int, int, float]]:
    """Best one-to-one matching of rows to columns; pairs below `min_score` are left out.

    Returns (row, col, score) sorted by row. Works for any shape.
    """
    score = np.asarray(score, dtype=np.float64)
    if score.size == 0:
        return []
    flipped = score.shape[0] > score.shape[1]
    s = score.T if flipped else score
    pairs = min_cost_assignment(s.max() - s)
    out = []
    for r, c in pairs:
        val = float(s[r, c])
        if val >= min_score:
            out.append((c, r, val) if flipped else (r, c, val))
    return sorted(out)
