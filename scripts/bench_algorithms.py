"""Measured runtimes of the algorithms at realistic sizes, for docs/algorithms.md.

    python scripts/bench_algorithms.py      # writes docs/evidence/algorithms.json
"""
from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rosetta.algorithms import geohash  # noqa: E402
from rosetta.algorithms.bloom import BloomFilter  # noqa: E402
from rosetta.algorithms.countmin import TopK  # noqa: E402
from rosetta.algorithms.hungarian import max_score_assignment  # noqa: E402
from rosetta.algorithms.replay_window import BatchReplayWindow, ReplayWindow  # noqa: E402
from rosetta.algorithms.sliding_window import LatencyHistogram, SlidingWindow  # noqa: E402
from rosetta.algorithms.trip_segmentation import segment_trips, threshold_baseline, to_segments  # noqa: E402
from rosetta.algorithms.unionfind import cluster_shapes  # noqa: E402
from rosetta.domain.dtc import extract_dtcs  # noqa: E402
from rosetta.domain.vin import is_valid_vin, make_vin  # noqa: E402


def timed(fn, repeat: int = 3) -> float:
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def main() -> None:
    rng = np.random.default_rng(1)
    out: dict = {"machine": {"cpu": platform.machine(), "python": platform.python_version()}, "results": {}}
    R = out["results"]

    vins = [make_vin("7NV", i) for i in range(100_000)]
    s = timed(lambda: [is_valid_vin(v) for v in vins])
    R["vin_check_digit"] = {"n": 100_000, "seconds": round(s, 4), "per_item_us": round(s / 1e5 * 1e6, 2)}

    texts = [f"codes: p0301, U0100;xP0420 B1A2F #{i}" for i in range(100_000)]
    s = timed(lambda: [extract_dtcs(t) for t in texts])
    R["dtc_extraction"] = {"n": 100_000, "seconds": round(s, 4), "per_item_us": round(s / 1e5 * 1e6, 2)}

    b = BloomFilter(2_000_000, 0.01)
    keys = [f"VIN{i}:{i * 7}".encode() for i in range(1_000_000)]
    s = timed(lambda: [b.add(k) for k in keys], 1)
    probes = [f"X{i}".encode() for i in range(200_000)]
    fp = sum(p in b for p in probes) / len(probes)
    R["bloom_filter"] = {"capacity": 2_000_000, "inserted": 1_000_000, "bytes": b.size_bytes, "hashes": b.k,
                         "insert_seconds": round(s, 3), "measured_false_positive_rate": round(fp, 5),
                         "theory_at_this_fill": round(b.estimated_fp_rate(), 5)}

    w = ReplayWindow()
    vids = [f"V{i}" for i in range(100_000)]
    seqs = rng.integers(0, 50, 1_000_000)
    order = rng.integers(0, 100_000, 1_000_000)
    s = timed(lambda: [w.check_and_set(vids[v], int(q)) for v, q in zip(order.tolist(), seqs.tolist())], 1)
    R["replay_window_scalar"] = {"events": 1_000_000, "vehicles": 100_000, "seconds": round(s, 3),
                                 "per_event_us": round(s / 1e6 * 1e6, 3)}
    bw = BatchReplayWindow(100_000)
    idx = order.astype(np.int64)
    sq = seqs.astype(np.int64)
    s = timed(lambda: [bw.check_batch(idx[a:a + 5000], sq[a:a + 5000]) for a in range(0, 1_000_000, 5000)], 1)
    R["replay_window_vectorised"] = {"events": 1_000_000, "batch": 5000, "seconds": round(s, 3),
                                     "per_event_us": round(s / 1e6 * 1e6, 3)}

    stream = [f"k{int(x)}" for x in rng.zipf(1.3, 500_000) % 50_000]
    t = TopK(10)
    s = timed(lambda: [t.add(k) for k in stream], 1)
    true = {}
    for k in stream:
        true[k] = true.get(k, 0) + 1
    real_top = sorted(true, key=lambda k: -true[k])[:10]
    R["count_min_topk"] = {"events": 500_000, "distinct": len(true), "width": t.sketch.width, "depth": t.sketch.depth,
                           "seconds": round(s, 3), "top10_recall": len(set(real_top) & {k for k, _ in t.items()}) / 10,
                           "max_overcount": max(t.sketch.estimate(k) - true[k] for k in real_top)}

    lat, lon = rng.uniform(-60, 60, 1_000_000), rng.uniform(-180, 180, 1_000_000)
    s = timed(lambda: geohash.encode_many(lat, lon, 5))
    s1 = timed(lambda: [geohash.encode(la, lo, 5) for la, lo in zip(lat[:50_000], lon[:50_000])], 1)
    R["geohash"] = {"vectorised_points": 1_000_000, "vectorised_seconds": round(s, 3),
                    "scalar_points": 50_000, "scalar_seconds": round(s1, 3)}

    h = LatencyHistogram()
    samples = rng.lognormal(5, 1, 1_000_000)
    s = timed(lambda: h.record_many(samples), 1)
    R["latency_histogram"] = {"samples": 1_000_000, "seconds": round(s, 4), "buckets": len(h.counts),
                              "p99_estimate": round(h.percentile(99), 1), "p99_exact": round(float(np.percentile(samples, 99)), 1)}
    sw = SlidingWindow(300)
    s = timed(lambda: [sw.add(1_790_000_000 + (i // 3000), 1.0) for i in range(300_000)], 1)
    R["sliding_window"] = {"updates": 300_000, "seconds": round(s, 3)}

    for n in (3_600, 86_400):
        sp = np.clip(np.concatenate([np.abs(rng.normal(0, 1.2, n // 4)), rng.normal(45, 15, n // 2),
                                     np.abs(rng.normal(0, 1.2, n - n // 4 - n // 2))]), 0, None)
        sp[n // 3: n // 3 + 20] = 0.2
        ts = np.arange(n) * 1000
        la = 12.9 + np.cumsum(sp / 3600 / 111)
        lo = np.full(n, 77.6)
        s = timed(lambda ts=ts, la=la, lo=lo, sp=sp: segment_trips(ts, la, lo, sp), 1)
        segs = segment_trips(ts, la, lo, sp)
        base = to_segments(threshold_baseline(sp), ts, la, lo, sp)
        R[f"trip_segmentation_{n}"] = {"points": n, "seconds": round(s, 4), "dp_segments": len(segs),
                                       "threshold_segments": len(base)}

    for n in (15, 60, 200):
        m = rng.random((n, n + 5))
        s = timed(lambda m=m: max_score_assignment(m))
        R[f"hungarian_{n}x{n + 5}"] = {"seconds": round(s, 5)}

    # six real formats, each seen with some optional fields missing: 20 shapes per format
    bases = [frozenset(f"f{b}_{i}" for i in range(14)) for b in range(6)]
    optional = [sorted(b)[-4:] for b in bases]
    sigs = []
    for b, opt in zip(bases, optional):
        for _ in range(20):
            drop = set(rng.choice(opt, size=rng.integers(0, 4), replace=False).tolist())
            sigs.append(frozenset(b - drop))
    s = timed(lambda: cluster_shapes(sigs, 0.6))
    R["union_find_shape_clustering"] = {"signatures": len(sigs), "distinct": len(set(sigs)), "true_formats": 6,
                                        "families_found": len(cluster_shapes(sigs, 0.6)), "seconds": round(s, 4)}

    p = ROOT / "docs" / "evidence" / "algorithms.json"
    p.write_text(json.dumps(out, indent=2))
    print(json.dumps(R, indent=1))


if __name__ == "__main__":
    main()
