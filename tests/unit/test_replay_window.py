"""rosetta.algorithms.replay_window: exact duplicate detection per vehicle."""
from __future__ import annotations

import numpy as np
import pytest

from rosetta.algorithms.replay_window import DUPLICATE, NEW, STALE, WINDOW, BatchReplayWindow, ReplayWindow


# --------------------------------------------------------------- ReplayWindow
def test_first_sequence_of_a_vehicle_is_new():
    w = ReplayWindow()
    assert w.check_and_set("v1", 100) == NEW
    assert len(w) == 1


def test_repeat_is_a_duplicate():
    w = ReplayWindow()
    w.check_and_set("v1", 100)
    assert w.check_and_set("v1", 100) == DUPLICATE
    assert w.check_and_set("v1", 100) == DUPLICATE


def test_increasing_sequences_are_new():
    w = ReplayWindow()
    assert [w.check_and_set("v1", s) for s in range(1, 200)] == [NEW] * 199


def test_out_of_order_inside_the_window_is_accepted_once():
    w = ReplayWindow()
    w.check_and_set("v1", 100)
    assert w.check_and_set("v1", 97) == NEW
    assert w.check_and_set("v1", 97) == DUPLICATE
    assert w.check_and_set("v1", 99) == NEW
    assert w.check_and_set("v1", 100) == DUPLICATE


def test_window_edges():
    w = ReplayWindow()
    w.check_and_set("v1", 1000)
    assert w.check_and_set("v1", 1000 - (WINDOW - 1)) == NEW, "oldest slot of the window"
    assert w.check_and_set("v1", 1000 - (WINDOW - 1)) == DUPLICATE
    assert w.check_and_set("v1", 1000 - WINDOW) == STALE, "one step older than the window"


def test_stale_is_reported_every_time_and_changes_nothing():
    w = ReplayWindow()
    w.check_and_set("v1", 1000)
    before = w.snapshot()
    assert w.check_and_set("v1", 5) == STALE
    assert w.check_and_set("v1", 5) == STALE
    assert w.snapshot() == before


def test_advancing_keeps_recent_history():
    w = ReplayWindow()
    w.check_and_set("v1", 10)
    w.check_and_set("v1", 12)
    assert w.check_and_set("v1", 10) == DUPLICATE
    assert w.check_and_set("v1", 11) == NEW
    assert w.check_and_set("v1", 12) == DUPLICATE


def test_large_jump_forgets_the_old_window():
    w = ReplayWindow()
    for s in range(1, 6):
        w.check_and_set("v1", s)
    assert w.check_and_set("v1", 5 + WINDOW) == NEW
    assert w.snapshot()["v1"] == (5 + WINDOW, 1)
    assert w.check_and_set("v1", 5) == STALE
    assert w.check_and_set("v1", 6) == NEW, "inside the new window and never seen"


def test_jump_of_window_minus_one_keeps_the_previous_top():
    w = ReplayWindow()
    w.check_and_set("v1", 1)
    w.check_and_set("v1", WINDOW)       # shift of 63: sequence 1 is the oldest slot
    assert w.check_and_set("v1", 1) == DUPLICATE


def test_vehicles_are_independent():
    w = ReplayWindow()
    w.check_and_set("v1", 50)
    assert w.check_and_set("v2", 50) == NEW
    assert w.check_and_set("v2", 1) == NEW
    assert w.check_and_set("v1", 50) == DUPLICATE
    assert len(w) == 2


def test_reset_forgets_one_vehicle():
    w = ReplayWindow()
    w.check_and_set("v1", 50_000)
    w.check_and_set("v2", 7)
    w.reset("v1")
    assert w.check_and_set("v1", 1) == NEW, "a restarted counter is accepted after reset"
    assert w.check_and_set("v2", 7) == DUPLICATE
    w.reset("unknown")                   # no error


def test_mask_never_grows_beyond_64_bits():
    w = ReplayWindow()
    for s in range(0, 1000, 3):
        w.check_and_set("v1", s)
    top, mask = w.snapshot()["v1"]
    assert top == 999
    assert 0 < mask < (1 << WINDOW)


def test_snapshot_and_restore_round_trip():
    w = ReplayWindow()
    for s in (5, 7, 6, 20):
        w.check_and_set("v1", s)
    w.check_and_set("v2", 3)
    snap = w.snapshot()
    other = ReplayWindow()
    other.restore(snap)
    assert other.snapshot() == snap
    assert other.check_and_set("v1", 7) == DUPLICATE
    assert other.check_and_set("v1", 8) == NEW


def test_snapshot_is_a_copy():
    w = ReplayWindow()
    w.check_and_set("v1", 5)
    snap = w.snapshot()
    w.check_and_set("v1", 6)
    assert snap == {"v1": (5, 1)}


def test_restore_accepts_json_style_lists():
    w = ReplayWindow()
    w.restore({"v1": [10, 3]})
    assert w.check_and_set("v1", 9) == DUPLICATE
    assert w.check_and_set("v1", 8) == NEW


# ---------------------------------------------------------- BatchReplayWindow
def arr(*values: int) -> np.ndarray:
    return np.array(values, dtype=np.int64)


def test_batch_empty_input():
    w = BatchReplayWindow(4)
    keep = w.check_batch(arr(), arr())
    assert keep.dtype == bool and keep.size == 0


def test_batch_all_new():
    w = BatchReplayWindow(3)
    assert w.check_batch(arr(0, 1, 2, 0), arr(10, 10, 10, 11)).tolist() == [True] * 4
    assert w.top.tolist() == [11, 10, 10]


def test_batch_repeat_inside_one_batch_keeps_the_first_occurrence():
    w = BatchReplayWindow(2)
    assert w.check_batch(arr(0, 1, 0, 0), arr(5, 5, 5, 5)).tolist() == [True, True, False, False]


def test_batch_duplicate_across_batches():
    w = BatchReplayWindow(2)
    w.check_batch(arr(0, 0, 1), arr(5, 6, 9))
    assert w.check_batch(arr(0, 0, 1, 1), arr(6, 7, 9, 8)).tolist() == [False, True, False, True]


def test_batch_out_of_order_inside_the_window():
    w = BatchReplayWindow(1)
    w.check_batch(arr(0), arr(100))
    assert w.check_batch(arr(0, 0), arr(90, 95)).tolist() == [True, True]
    assert w.check_batch(arr(0, 0, 0), arr(90, 95, 100)).tolist() == [False, False, False]


def test_batch_stale_events_are_kept_for_the_late_path():
    w = BatchReplayWindow(1)
    w.check_batch(arr(0), arr(1000))
    assert w.check_batch(arr(0), arr(1000 - WINDOW)).tolist() == [True]
    assert w.top.tolist() == [1000]


def test_batch_sequence_zero_is_accepted_for_a_new_vehicle():
    w = BatchReplayWindow(1)
    assert w.check_batch(arr(0), arr(0)).tolist() == [True]
    assert w.check_batch(arr(0), arr(0)).tolist() == [False]


def test_batch_result_is_aligned_with_the_input_order():
    w = BatchReplayWindow(3)
    w.check_batch(arr(2), arr(50))
    keep = w.check_batch(arr(2, 0, 2, 1, 2), arr(51, 7, 50, 7, 49))
    assert keep.tolist() == [True, True, False, True, True]


def _reference_batch(ref: ReplayWindow, idx: np.ndarray, seq: np.ndarray) -> np.ndarray:
    """What the scalar window says for a batch.

    The batch version handles a batch in (vehicle, sequence) order, so the
    reference is fed in that order. It keeps everything that is not a DUPLICATE
    (STALE goes to the late path), and additionally drops a repeat of the same
    (vehicle, sequence) inside one batch, which for a stale number the scalar
    window would report as STALE twice.
    """
    expected = np.ones(idx.size, dtype=bool)
    seen: set[tuple[int, int]] = set()
    for j in np.lexsort((seq, idx)):
        key = (int(idx[j]), int(seq[j]))
        verdict = ref.check_and_set(str(key[0]), key[1])
        expected[j] = verdict != DUPLICATE and key not in seen
        seen.add(key)
    return expected


@pytest.mark.parametrize("seed", range(25))
def test_batch_agrees_with_the_scalar_reference_on_random_traffic(seed):
    rng = np.random.default_rng(seed)
    vehicles = 7
    ref, batch = ReplayWindow(), BatchReplayWindow(vehicles)
    head = np.zeros(vehicles, dtype=np.int64)
    for _ in range(60):
        m = int(rng.integers(0, 50))
        idx = rng.integers(0, vehicles, m).astype(np.int64)
        seq = np.maximum(0, head[idx] + rng.integers(-90, 12, m)).astype(np.int64)
        if m and rng.random() < 0.15:
            seq[0] = head[idx[0]] + int(rng.integers(60, 300))      # a jump across the window
        expected = _reference_batch(ref, idx, seq)
        got = batch.check_batch(idx, seq)
        assert got.tolist() == expected.tolist()
        for v in range(vehicles):
            state = ref.snapshot().get(str(v))
            if state is None:
                assert batch.top[v] == -1
            else:
                assert (int(batch.top[v]), int(batch.mask[v])) == state
        head = np.maximum(head, batch.top)


def test_batch_with_many_vehicles_and_one_event_each():
    n = 5_000
    w = BatchReplayWindow(n)
    idx = np.arange(n, dtype=np.int64)
    seq = np.random.default_rng(1).integers(1, 60_000, n).astype(np.int64)
    assert w.check_batch(idx, seq).all()
    assert not w.check_batch(idx, seq).any()
    assert w.check_batch(idx, seq + 1).all()
