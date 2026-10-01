# Algorithms and data structures

Each entry: the problem it solves in Rosetta, why this algorithm, pseudocode,
complexity, and a measured runtime at a realistic size. Measurements come from
`python scripts/bench_algorithms.py` (Apple M4, Python 3.11), written to
`docs/evidence/algorithms.json`. Every algorithm has unit tests in `tests/unit/`.

## 1. Exact de-duplication: per-vehicle replay window
`rosetta/algorithms/replay_window.py`

**Problem.** Networks and devices resend. At 100,000 events per second every event
must be checked, and a false "duplicate" would lose data.

**Why this.** A set of every (VIN, sequence) seen grows without bound. The IPsec
anti-replay window keeps, per vehicle, the highest sequence number and a 64-bit mask
of which of the 64 numbers below it have arrived. Exact, constant time, constant
memory per vehicle, and tolerant of reordering inside the window.

```
check(vehicle, seq):
    top, mask = state[vehicle]                     # absent: store (seq, 1), NEW
    if seq > top:  mask = (mask << (seq - top)) | 1 truncated to 64 bits; top = seq; NEW
    back = top - seq
    if back >= 64: STALE                           # handled by the late path (2)
    if mask has bit back: DUPLICATE
    set bit back; NEW
```

O(1) time, 2 integers per vehicle. The processor uses a vectorised version
(`BatchReplayWindow`): one sort of the batch by (vehicle, seq), then numpy
operations on uint64 masks. Measured: 1,000,000 events over 100,000 vehicles in
0.31 s scalar (0.31 µs per event), 0.10 s vectorised in batches of 5,000.

## 2. Late events: Bloom filter in front of an exact store
`rosetta/algorithms/bloom.py`, `rosetta/engine/dedup.py`

**Problem.** An event older than the 64-number window (a device that was offline)
still has to be checked, and exactly.

**Why this.** A Bloom filter answers "definitely new" for almost every late event
without a network round trip. Only on "maybe seen" do we ask the exact store
(Redis `SET NX EX`). A false positive therefore costs one lookup, never an event.
Two filters rotate so memory stays bounded on an endless stream.

m = −n ln p / (ln 2)², k = (m / n) ln 2. Double hashing (Kirsch and Mitzenmacher):
k positions from one 128-bit xxHash. O(k) per operation.

Measured: capacity 2,000,000 at p = 1%: 2.4 MB, k = 7. After 1,000,000 inserts
the measured false-positive rate was 0.030% against a theoretical 0.025% at that
fill level. Insert: 1.26 µs.

## 3. Heavy hitters: Count-Min sketch with a top-K heap
`rosetta/algorithms/countmin.py`

**Problem.** Which fields fail most, across every source, without a counter per
distinct key (field names of unknown formats are unbounded).

**Why this.** Count-Min never undercounts and overcounts by at most εN with
probability 1 − δ, in fixed memory: w = ⌈e / ε⌉, d = ⌈ln 1/δ⌉. Sketches from
several workers add together, which is how the API merges them. A lazy min-heap
keeps the current top K in O(log K) per update.

Measured: 500,000 events over 26,063 distinct keys (Zipf 1.3), ε = 0.001, δ = 0.01:
table 5 × 2,719, 0.67 s, top-10 recall 100%, largest overcount on a top-10 key: 30.

## 4. Geohash
`rosetta/algorithms/geohash.py`

**Problem.** Map queries by area, density per cell, and location masking for
privacy (a 5-character cell is about 4.9 km).

**Why this.** Interleaving longitude and latitude bits gives a string where nearby
points share a prefix, so cells are strings and grouping is a string operation.
Scalar encode is O(precision). The vectorised version builds all bits with numpy.

Measured: 1,000,000 points in 0.051 s vectorised; 50,000 in 0.095 s scalar.

## 5. Trip segmentation: dynamic programming (Viterbi over two states)
`rosetta/algorithms/trip_segmentation.py`

**Problem.** A noisy GPS track has to become trips and stops. Speed jitters above
zero while parked and drops to zero at traffic lights, so a threshold cuts one
trip into dozens of pieces.

**Why this.** Label each point STOP or MOVE to minimise
`Σ emission(point, state) + penalty × (number of changes)`, where emission is the
negative log-likelihood of the speed under each state, weighted by the seconds the
point covers (so the answer does not depend on the reporting rate). The penalty is
derived from the shortest pause that should count as a stop (90 s by default).

```
dp[0][s] = cost(0, s)
dp[i][s] = cost(i, s) + min(dp[i-1][s], dp[i-1][1-s] + penalty)
walk the back-pointers from the cheaper final state
```

O(n · k²) with k = 2, so O(n) time and O(n) memory for the back-pointers.

Measured, against the plain speed threshold on the same track:

| Points | DP runtime | DP segments | Threshold segments |
|---|---|---|---|
| 3,600 (one hour) | 8 ms | 3 | 49 |
| 86,400 (one day at 1 Hz) | 182 ms | 3 | 1,299 |

## 6. Field assignment: the Hungarian algorithm
`rosetta/algorithms/hungarian.py`

**Problem.** The classifier scores every (source field, canonical field) pair.
Taking the best target for each source field independently can map two source
fields to `speed_kmh`.

**Why this.** Maximum-weight bipartite matching gives the one-to-one assignment
with the highest total probability. Kuhn and Munkres with potentials,
O(n² · m) for n ≤ m.

Measured: 15 × 20 in 0.07 ms (a typical format), 60 × 65 in 0.85 ms,
200 × 205 in 24 ms. Checked against `scipy.optimize.linear_sum_assignment` on
random matrices in the unit tests.

## 7. Grouping dead letters: union-find on payload shapes
`rosetta/algorithms/unionfind.py`

**Problem.** Forty thousand parked messages from one new source arrive in dozens
of shapes, because optional fields come and go. The operator should see one
format, not dozens.

**Why this.** Each shape is the set of its field paths. Shapes with a Jaccard
similarity of at least 0.6 are joined: connected components of a similarity graph,
with path compression and union by rank (amortised O(α(n))).

Measured: 120 signatures from 6 real formats with random optional fields missing:
6 families found, 0.8 ms.

## 8. Streaming windows and latency percentiles
`rosetta/algorithms/sliding_window.py`

**SlidingWindow.** One bucket per second in a ring buffer: update O(1), read
O(window), memory independent of the event rate. 300,000 updates in 52 ms.

**LatencyHistogram.** Log-spaced buckets (growth 1.08, 214 buckets from 10 µs to
120 s): record O(1), any percentile O(buckets), and histograms from different
workers merge exactly by adding counts. Relative error is bounded by the growth
factor. Measured on 1,000,000 log-normal samples: p99 estimated 1,637 ms against
1,517 ms exact, within the 8% bound. Recording the batch: 7 ms.

## 9. Parsing and validation

**VIN.** `^[A-HJ-NPR-Z0-9]{17}$` (no I, O or Q) plus the ISO 3779 / North American
check digit: transliterate, weight, sum mod 11, 10 is X. 100,000 VINs in 0.08 s
(0.8 µs each). The normaliser caches VINs that passed, so each vehicle's checksum is
computed once, not once per event.

**Diagnostic trouble codes.** `[PCBU][0-3][0-9A-F]{3}` with lookarounds so that
`xP0420` inside a longer token is not taken for a code. Works on lists, comma or
semicolon strings, free text and lower case. 100,000 texts in 0.10 s (1 µs each).

## 10. Physics as a feature: identifying units without names
`rosetta/ml/profile.py`

**Problem.** `fahrt.v` says nothing about speed or its unit. A field in mph mapped
as km/h still looks plausible.

**Approach.** Find the pair of fields that behave like latitude and longitude
(every plausible angle scale is a candidate; the pair whose movement is physically
possible and whose bearing agrees with a heading field wins). From consecutive
messages of the same vehicle, compute distance travelled and GPS speed. Then for
every numeric field: the median ratio of its value to GPS speed and how stable the
ratio is, and the ratio of its growth to distance travelled. A field always 0.278
times the GPS speed is metres per second. One that grows 1,000 units per km is an
odometer in metres. O(f · n) for f fields and n message pairs.
