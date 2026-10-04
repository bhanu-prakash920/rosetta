"""rosetta.algorithms.hungarian: optimal one-to-one assignment."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.optimize import linear_sum_assignment

from rosetta.algorithms.hungarian import max_score_assignment, min_cost_assignment


def cost_of(cost: np.ndarray, pairs) -> float:
    return float(sum(cost[r, c] for r, c in pairs))


def test_empty_matrix():
    assert min_cost_assignment(np.zeros((0, 3))) == []
    assert max_score_assignment(np.zeros((0, 3))) == []
    assert max_score_assignment(np.zeros((3, 0))) == []


def test_one_by_one():
    assert min_cost_assignment([[7.0]]) == [(0, 0)]


def test_textbook_three_by_three():
    cost = np.array([[4, 1, 3], [2, 0, 5], [3, 2, 2]], dtype=float)
    pairs = min_cost_assignment(cost)
    assert pairs == [(0, 1), (1, 0), (2, 2)]
    assert cost_of(cost, pairs) == 5.0


def test_greedy_would_be_wrong():
    # Picking the cheapest cell first (0,0)=1 forces (1,1)=100. The optimum is 2 + 3.
    cost = np.array([[1.0, 2.0], [3.0, 100.0]])
    assert min_cost_assignment(cost) == [(0, 1), (1, 0)]


@pytest.mark.parametrize("cost", [np.zeros(3), np.zeros((2, 2, 2)), 5.0])
def test_input_must_be_two_dimensional(cost):
    with pytest.raises(ValueError, match="2-D"):
        min_cost_assignment(cost)


def test_more_rows_than_columns_is_rejected_by_min_cost():
    with pytest.raises(ValueError, match="rows <= columns"):
        min_cost_assignment(np.zeros((3, 2)))


def test_accepts_nested_lists_and_integers():
    assert min_cost_assignment([[1, 9], [9, 1]]) == [(0, 0), (1, 1)]


def test_every_row_gets_a_distinct_column():
    rng = np.random.default_rng(1)
    cost = rng.uniform(0, 10, (6, 9))
    pairs = min_cost_assignment(cost)
    assert [r for r, _ in pairs] == list(range(6))
    assert len({c for _, c in pairs}) == 6


@pytest.mark.parametrize("seed", range(30))
def test_square_matrices_agree_with_scipy(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 12))
    cost = rng.uniform(-5, 20, (n, n))
    rows, cols = linear_sum_assignment(cost)
    assert cost_of(cost, min_cost_assignment(cost)) == pytest.approx(float(cost[rows, cols].sum()))


@pytest.mark.parametrize("seed", range(30))
def test_wide_matrices_agree_with_scipy(seed):
    rng = np.random.default_rng(1000 + seed)
    n = int(rng.integers(1, 8))
    m = n + int(rng.integers(1, 8))
    cost = rng.uniform(0, 1, (n, m))
    rows, cols = linear_sum_assignment(cost)
    pairs = min_cost_assignment(cost)
    assert len(pairs) == n
    assert cost_of(cost, pairs) == pytest.approx(float(cost[rows, cols].sum()))


def test_integer_costs_with_many_ties_agree_with_scipy():
    rng = np.random.default_rng(5)
    for _ in range(20):
        cost = rng.integers(0, 4, (7, 7)).astype(float)
        rows, cols = linear_sum_assignment(cost)
        assert cost_of(cost, min_cost_assignment(cost)) == float(cost[rows, cols].sum())


# ------------------------------------------------------------------ max score
@pytest.mark.parametrize("shape", [(5, 5), (3, 8), (8, 3), (1, 6), (6, 1)])
@pytest.mark.parametrize("seed", range(6))
def test_max_score_agrees_with_scipy_for_any_shape(shape, seed):
    rng = np.random.default_rng(seed * 31 + shape[0] * 7 + shape[1])
    score = rng.uniform(0, 1, shape)
    rows, cols = linear_sum_assignment(score, maximize=True)
    result = max_score_assignment(score)
    assert sum(v for _, _, v in result) == pytest.approx(float(score[rows, cols].sum()))
    assert len(result) == min(shape)


def test_max_score_returns_row_column_score_sorted_by_row():
    score = np.array([[0.1, 0.9, 0.2], [0.8, 0.85, 0.1]])
    assert max_score_assignment(score) == [(0, 1, 0.9), (1, 0, 0.8)]


def test_tall_matrix_reports_pairs_in_the_original_orientation():
    score = np.array([[0.1, 0.2], [0.9, 0.1], [0.2, 0.8]])
    result = max_score_assignment(score)
    assert result == [(1, 0, 0.9), (2, 1, 0.8)]
    for r, c, v in result:
        assert score[r, c] == v


def test_two_rows_never_share_a_column():
    # Both rows like column 0 best. Independent argmax would map both to it.
    score = np.array([[0.9, 0.8, 0.0], [0.95, 0.1, 0.0]])
    result = max_score_assignment(score)
    assert result == [(0, 1, 0.8), (1, 0, 0.95)]


def test_min_score_leaves_weak_pairs_out():
    score = np.array([[0.9, 0.1], [0.2, 0.3]])
    assert max_score_assignment(score, min_score=0.5) == [(0, 0, 0.9)]
    assert max_score_assignment(score, min_score=0.95) == []


def test_min_score_is_inclusive():
    score = np.array([[0.5, 0.0], [0.0, 0.7]])
    assert max_score_assignment(score, min_score=0.5) == [(0, 0, 0.5), (1, 1, 0.7)]


def test_min_score_filters_after_the_optimal_matching_is_chosen():
    rng = np.random.default_rng(11)
    score = rng.uniform(0, 1, (6, 9))
    full = max_score_assignment(score, min_score=-1.0)
    filtered = max_score_assignment(score, min_score=0.6)
    assert filtered == [p for p in full if p[2] >= 0.6]
    assert all(v >= 0.6 for _, _, v in filtered)


def test_negative_scores_are_handled():
    score = np.array([[-1.0, -5.0], [-5.0, -2.0]])
    assert max_score_assignment(score, min_score=-10.0) == [(0, 0, -1.0), (1, 1, -2.0)]
    assert max_score_assignment(score) == [], "default min_score is 0"
