"""rosetta.algorithms.sliding_window: per-second ring buffer and log-bucket latency histogram."""
from __future__ import annotations

import numpy as np
import pytest

from rosetta.algorithms.sliding_window import LatencyHistogram, SlidingWindow

NOW = 1_790_000_000


# -------------------------------------------------------------- SlidingWindow
def test_empty_window():
    w = SlidingWindow(60)
    assert w.total(NOW) == 0.0
    assert w.rate(NOW) == 0.0
    assert w.series(NOW) == [0.0] * 60


def test_values_in_the_same_second_add_up():
    w = SlidingWindow(60)
    w.add(NOW)
    w.add(NOW, 4.0)
    w.add(NOW, 0.5)
    assert w.total(NOW) == 5.5


def test_total_covers_exactly_the_last_n_seconds():
    w = SlidingWindow(10)
    for t in range(NOW - 20, NOW + 1):
        w.add(t, 1.0)
    assert w.total(NOW) == 10.0
    assert w.rate(NOW) == 1.0


def test_value_leaves_the_window_after_n_seconds():
    w = SlidingWindow(10)
    w.add(NOW, 7.0)
    assert w.total(NOW + 9) == 7.0, "still inside: 10 s window includes the 9 s old bucket"
    assert w.total(NOW + 10) == 0.0


def test_future_buckets_are_not_counted():
    w = SlidingWindow(10)
    w.add(NOW + 3, 2.0)
    assert w.total(NOW) == 0.0
    assert w.total(NOW + 3) == 2.0


def test_slot_is_reused_after_a_full_turn():
    w = SlidingWindow(10)
    w.add(NOW, 5.0)
    w.add(NOW + 10, 1.0)               # same slot, one turn later
    assert w.total(NOW + 10) == 1.0
    assert w.series(NOW + 10)[-1] == 1.0


def test_series_is_ordered_oldest_to_newest_with_zero_gaps():
    w = SlidingWindow(5)
    w.add(NOW - 4, 1.0)
    w.add(NOW - 2, 3.0)
    w.add(NOW, 5.0)
    assert w.series(NOW) == [1.0, 0.0, 3.0, 0.0, 5.0]
    assert w.series(NOW + 1) == [0.0, 3.0, 0.0, 5.0, 0.0]


def test_series_sums_to_total():
    rng = np.random.default_rng(1)
    w = SlidingWindow(60)
    for t in rng.integers(NOW - 59, NOW + 1, 500):
        w.add(int(t), float(rng.integers(1, 5)))
    assert sum(w.series(NOW)) == pytest.approx(w.total(NOW))


def test_rate_is_the_average_per_second_over_the_whole_window():
    w = SlidingWindow(60)
    w.add(NOW, 120.0)
    assert w.rate(NOW) == 2.0


def test_default_window_is_sixty_seconds():
    assert SlidingWindow().size == 60


def test_a_late_sample_does_not_erase_current_data():
    w = SlidingWindow(60)
    w.add(NOW, 5.0)
    w.add(NOW - 60, 1.0)               # a straggler, one full window late
    assert w.total(NOW) == 5.0


# ----------------------------------------------------------- LatencyHistogram
def test_empty_histogram():
    h = LatencyHistogram()
    assert h.n == 0
    assert h.percentile(50) == 0.0
    assert h.mean() == 0.0


def test_single_sample():
    h = LatencyHistogram()
    h.record(12.5)
    assert h.n == 1 and h.max == 12.5 and h.mean() == 12.5
    assert h.percentile(50) == pytest.approx(12.5, rel=0.08)
    assert h.percentile(100) == 12.5


def test_percentile_never_exceeds_the_maximum():
    h = LatencyHistogram()
    for ms in (1.0, 2.0, 3.0):
        h.record(ms)
    assert h.percentile(100) == 3.0
    assert h.percentile(99.999) <= 3.0


@pytest.mark.parametrize("p", [50, 90, 95, 99])
def test_percentiles_are_within_the_bucket_growth_of_the_exact_value(p):
    rng = np.random.default_rng(2)
    samples = rng.lognormal(mean=2.0, sigma=0.8, size=20_000)
    h = LatencyHistogram()
    for ms in samples:
        h.record(float(ms))
    exact = float(np.percentile(samples, p, method="inverted_cdf"))
    assert exact <= h.percentile(p) <= exact * 1.08 * 1.0001


def test_percentiles_are_monotonic():
    rng = np.random.default_rng(3)
    h = LatencyHistogram()
    h.record_many(rng.exponential(20.0, 5_000))
    values = [h.percentile(p) for p in (1, 10, 50, 90, 99, 100)]
    assert values == sorted(values)


def test_record_with_count():
    a, b = LatencyHistogram(), LatencyHistogram()
    a.record(8.0, count=5)
    for _ in range(5):
        b.record(8.0)
    assert np.array_equal(a.counts, b.counts)
    assert (a.n, a.sum, a.max) == (b.n, b.sum, b.max) == (5, 40.0, 8.0)


def test_record_many_equals_repeated_record():
    rng = np.random.default_rng(4)
    samples = np.concatenate([rng.lognormal(1.5, 1.2, 3_000), [0.0, 0.005, 0.01, 119_999.0, 500_000.0]])
    one, many = LatencyHistogram(), LatencyHistogram()
    for ms in samples:
        one.record(float(ms))
    many.record_many(samples)
    assert np.array_equal(one.counts, many.counts)
    assert one.n == many.n == len(samples)
    assert one.max == many.max
    assert one.sum == pytest.approx(many.sum)


def test_record_many_with_nothing_is_a_no_op():
    h = LatencyHistogram()
    h.record_many(np.array([]))
    assert h.n == 0 and h.counts.sum() == 0


def test_values_at_or_below_the_floor_go_to_the_first_bucket():
    h = LatencyHistogram(lo_ms=0.01)
    for ms in (0.0, 0.001, 0.01):
        h.record(ms)
    assert h.counts[0] == 3


def test_values_above_the_ceiling_go_to_the_last_bucket():
    h = LatencyHistogram(hi_ms=1000.0)
    h.record(1e9)
    assert h.counts[-1] == 1
    assert h.max == 1e9


def test_mean_is_exact():
    h = LatencyHistogram()
    for ms in (1.0, 2.0, 6.0):
        h.record(ms)
    assert h.mean() == pytest.approx(3.0)


def test_merge_equals_recording_everything_in_one_histogram():
    rng = np.random.default_rng(5)
    a_s, b_s = rng.exponential(5.0, 1_000), rng.exponential(50.0, 1_000)
    a, b, whole = LatencyHistogram(), LatencyHistogram(), LatencyHistogram()
    a.record_many(a_s)
    b.record_many(b_s)
    whole.record_many(np.concatenate([a_s, b_s]))
    a.merge(b)
    assert np.array_equal(a.counts, whole.counts)
    assert a.n == whole.n and a.max == whole.max
    assert a.sum == pytest.approx(whole.sum)
    assert a.percentile(95) == whole.percentile(95)


def test_reset_clears_everything():
    h = LatencyHistogram()
    h.record_many(np.array([1.0, 10.0, 100.0]))
    h.reset()
    assert h.n == 0 and h.max == 0.0 and h.sum == 0.0 and h.counts.sum() == 0
    assert h.percentile(99) == 0.0


def test_finer_growth_gives_a_tighter_estimate():
    samples = np.random.default_rng(6).lognormal(3.0, 0.5, 5_000)
    exact = float(np.percentile(samples, 95, method="inverted_cdf"))
    coarse, fine = LatencyHistogram(growth=1.5), LatencyHistogram(growth=1.02)
    coarse.record_many(samples)
    fine.record_many(samples)
    assert abs(fine.percentile(95) - exact) <= abs(coarse.percentile(95) - exact)
    assert len(fine.counts) > len(coarse.counts)
