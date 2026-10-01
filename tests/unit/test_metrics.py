"""rosetta.pipeline.metrics: worker snapshots merged into dashboard numbers."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from rosetta.adapters.log_broker import LogBroker
from rosetta.algorithms.sliding_window import LatencyHistogram
from rosetta.pipeline import metrics as M
from rosetta.pipeline.metrics import Aggregator, MetricsReader, hist_to_sparse, publish
from rosetta.ports.broker import T_METRICS

NOW_S = 1_790_000_000
NOW_MS = NOW_S * 1000


@pytest.fixture()
def clock(monkeypatch):
    """Freeze the clock the metrics module sees."""
    c = SimpleNamespace(now=float(NOW_S) + 0.25)
    monkeypatch.setattr(M, "time", SimpleNamespace(time=lambda: c.now, time_ns=time.time_ns, sleep=time.sleep))
    return c


def hist(*samples: float) -> dict[str, Any]:
    h = LatencyHistogram()
    for ms in samples:
        h.record(ms)
    return {"lat": hist_to_sparse(h), "lat_max": h.max}


def normalizer_msg(ident: str = "n0", ts: int = NOW_MS - 1000, **stats: Any) -> dict[str, Any]:
    base = {"ok": [], "failed": [], "failed_field": [], "canary_failed": [], "duplicates": {}, "fallbacks": 0,
            "replayed_ok": 0, "bytes_in": 0}
    base.update(stats)
    return {"svc": "normalizer", "id": ident, "ts": ts, "pid": 100, "lag": 0, "lag_records": 0, "stats": base}


# -------------------------------------------------------------------- helpers
def test_hist_to_sparse_lists_only_the_used_buckets():
    h = LatencyHistogram()
    assert hist_to_sparse(h) == []
    h.record(5.0, count=3)
    h.record(50.0)
    sparse = hist_to_sparse(h)
    assert [c for _, c in sparse] == [3, 1]
    assert sparse[0][0] < sparse[1][0]
    assert all(type(i) is int and type(c) is int for i, c in sparse)


def test_publish_writes_one_snapshot_to_the_metrics_topic(tmp_path):
    broker = LogBroker(tmp_path / "log", partitions=1)
    before = int(time.time() * 1000)
    publish(broker, "normalizer", "n3", {"lag": 12, "stats": {"fallbacks": 1}})
    rec = broker.consumer(T_METRICS, "t", start="beginning").poll(10, 0.0)[0]
    msg = orjson.loads(rec.value)
    assert rec.key == b"normalizer:n3"
    assert (msg["svc"], msg["id"], msg["lag"], msg["stats"]) == ("normalizer", "n3", 12, {"fallbacks": 1})
    assert before <= msg["ts"] <= int(time.time() * 1000)


def test_histogram_ring_forgets_old_seconds():
    ring = M._Hist(seconds=60)
    ring.add(NOW_S - 100, hist(5.0)["lat"], 5.0)
    ring.add(NOW_S - 10, hist(50.0, 60.0)["lat"], 60.0)
    merged = ring.merged(NOW_S)
    assert merged.n == 2 and merged.max == 60.0
    assert len(ring.ring) == 1
    assert ring.summary(NOW_S)["n"] == 2
    assert ring.summary(NOW_S + 60)["n"] == 0


def test_histogram_ring_ignores_bucket_indexes_out_of_range():
    ring = M._Hist()
    ring.add(NOW_S, [[-1, 5], [10 ** 6, 5], [3, 2]], 1.0)
    assert ring.merged(NOW_S).n == 2


def test_histogram_summary_of_nothing():
    assert M._Hist().summary(NOW_S) == {"n": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}


# ------------------------------------------------------------------ normalizer
def test_normalizer_snapshot_feeds_totals_versions_and_reasons(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ok=[["nordvik", 1, 90], ["pacifica", 2, 40]],
                              failed=[["pacifica", "SCHEMA_MISMATCH", 10], ["helix", "NO_ADAPTER", 5]],
                              failed_field=[["pacifica", "SCHEMA_MISMATCH", "speed_kmh", 10],
                                            ["helix", "NO_ADAPTER", "", 5]],
                              duplicates={"nordvik": 3}, fallbacks=7, replayed_ok=2, bytes_in=12_345))
    ov = agg.overview()
    assert ov["totals"] == {"ok": 130, "failed": 15, "duplicates": 3, "sent": 0}
    assert ov["counters"] == {"fallbacks": 7, "replayed_ok": 2, "bytes_in": 12_345}
    by_oem = {o["oem"]: o for o in ov["oems"]}
    assert sorted(by_oem) == ["helix", "nordvik", "pacifica"]
    assert by_oem["nordvik"]["total_ok"] == 90 and by_oem["nordvik"]["total_duplicates"] == 3
    assert by_oem["pacifica"]["versions"] == [{"version": 2, "events": 40, "per_s": 4.0}]
    assert by_oem["pacifica"]["reasons"] == {"SCHEMA_MISMATCH": 10}
    assert by_oem["helix"]["reasons"] == {"NO_ADAPTER": 5}
    assert ov["top_failing_fields"] == [{"key": "pacifica.speed_kmh (SCHEMA_MISMATCH)", "count": 10}]


def test_rates_are_averages_over_the_last_ten_seconds(clock):
    agg = Aggregator()
    for k in range(1, 11):                                # 10 seconds with 50 ok and 5 failed each
        agg.ingest(normalizer_msg(ts=NOW_MS - k * 1000, ok=[["nordvik", 1, 50]],
                                  failed=[["nordvik", "INVALID", 5]]))
    agg.ingest(normalizer_msg(ts=NOW_MS - 60_000, ok=[["nordvik", 1, 10_000]]))      # too old for the rate
    ov = agg.overview()
    assert ov["throughput"]["ok_per_s"] == 50.0 and ov["throughput"]["failed_per_s"] == 5.0
    nordvik = ov["oems"][0]
    assert (nordvik["ok_per_s"], nordvik["failed_per_s"]) == (50.0, 5.0)
    assert nordvik["success_rate"] == pytest.approx(50 / 55, abs=1e-5)
    assert nordvik["total_ok"] == 10_500, "totals keep everything"


def test_success_rate_is_unknown_without_traffic(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ts=NOW_MS - 120_000, ok=[["nordvik", 1, 5]]))
    assert agg.overview()["oems"][0]["success_rate"] is None


def test_snapshots_of_several_workers_add_up(clock):
    agg = Aggregator()
    for ident in ("n0", "n1", "n2"):
        agg.ingest(normalizer_msg(ident, ok=[["nordvik", 1, 10]], fallbacks=1))
    ov = agg.overview()
    assert ov["totals"]["ok"] == 30 and ov["counters"]["fallbacks"] == 3
    assert [w["id"] for w in ov["workers"]] == ["n0", "n1", "n2"]


def test_latency_percentiles_come_from_merged_histograms(clock):
    agg = Aggregator()
    agg.ingest({**normalizer_msg("n0"), **hist(*[10.0] * 90)})
    agg.ingest({**normalizer_msg("n1"), **hist(*[200.0] * 10)})
    lat = agg.overview()["latency_ms"]["normalize"]
    assert lat["n"] == 100 and lat["max"] == 200.0
    assert lat["p50"] == pytest.approx(10.0, rel=0.08)
    assert lat["p95"] == pytest.approx(200.0, rel=0.08)


def test_routing_table_and_errors_are_taken_from_a_live_worker(clock):
    agg = Aggregator()
    table = {"pacifica": {"active": [1], "canary": 2, "canary_pct": 10}}
    agg.ingest({**normalizer_msg("n0"), "table": table, "table_errors": ["pacifica v3: bad"], "epoch": 4})
    ov = agg.overview()
    assert ov["routing"] == table
    worker = ov["workers"][0]
    assert worker["epoch"] == 4
    assert "table" not in worker and "table_errors" not in worker
    assert agg.workers["normalizer:n0"]["table_errors"] == ["pacifica v3: bad"]


def test_workers_that_went_silent_are_left_out(clock):
    agg = Aggregator()
    agg.ingest({**normalizer_msg("alive", ts=NOW_MS - 2000), "lag_records": 40})
    agg.ingest({**normalizer_msg("gone", ts=NOW_MS - 16_000), "lag_records": 900})
    ov = agg.overview()
    assert [w["id"] for w in ov["workers"]] == ["alive"]
    assert ov["lag"] == {"normalizer": 40, "processor": 0}


def test_canary_health(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ok=[["pacifica", 2, 95], ["pacifica", 1, 500]], canary_failed=[["pacifica", 2, 5]]))
    assert agg.canary_health("pacifica", 2) == {"ok": 95, "failed": 5, "window_s": 60, "failure_rate": 0.05}
    assert agg.canary_health("pacifica", 9) == {"ok": 0, "failed": 0, "window_s": 60, "failure_rate": None}


def test_canary_health_only_looks_at_its_window(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ts=NOW_MS - 90_000, ok=[["pacifica", 2, 1000]]))
    agg.ingest(normalizer_msg(ts=NOW_MS - 5_000, ok=[["pacifica", 2, 9]], canary_failed=[["pacifica", 2, 1]]))
    assert agg.canary_health("pacifica", 2, window=60)["failure_rate"] == 0.1
    assert agg.canary_health("pacifica", 2, window=120)["ok"] == 1009


# ------------------------------------------------------------- other services
def test_processor_snapshot(clock):
    agg = Aggregator()
    agg.ingest({"svc": "processor", "id": "p0", "ts": NOW_MS - 1000, "events": 400, "alerts": 3,
                "archived_rows": 380, "late": 2, "duplicates": 1, "lag_records": 25,
                "e2e": hist(40.0, 45.0)["lat"], "e2e_max": 45.0, "alert_lat": hist(90.0)["lat"], "alert_lat_max": 90.0})
    ov = agg.overview()
    assert ov["counters"] == {"processed": 400, "alerts": 3, "archived_rows": 380, "late_events": 2,
                              "sink_duplicates": 1}
    assert ov["throughput"]["processed_per_s"] == 40.0
    assert ov["latency_ms"]["ingest_to_dashboard"]["n"] == 2
    assert ov["latency_ms"]["alert"]["max"] == 90.0
    assert ov["lag"] == {"normalizer": 0, "processor": 25}


def test_simulator_snapshot(clock):
    agg = Aggregator()
    settings = {"mode": "all", "hz": 1.0}
    agg.ingest({"svc": "simulator", "id": "s0", "ts": NOW_MS - 1000, "sent": 100, "by_oem": {"nordvik": 60, "helix": 40},
                "dups": 2, "malformed": 1, "reordered": 3, "settings": settings})
    ov = agg.overview()
    assert ov["totals"]["sent"] == 100
    assert ov["counters"] == {"sim_sent": 100, "sim_duplicates": 2, "sim_malformed": 1, "sim_reordered": 3}
    assert ov["simulator"] == settings
    assert {o["oem"]: o["sent_per_s"] for o in ov["oems"]} == {"helix": 4.0, "nordvik": 6.0}


def test_dlq_snapshot(clock):
    agg = Aggregator()
    agg.ingest({"svc": "dlq", "id": "d0", "ts": NOW_MS, "indexed": 12, "replayed": 7})
    agg.ingest({"svc": "dlq", "id": "d0", "ts": NOW_MS, "indexed": 1})
    assert agg.overview()["counters"] == {"dlq_indexed": 13, "dlq_replayed": 7}


def test_unknown_service_is_listed_as_a_worker_and_nothing_else(clock):
    agg = Aggregator()
    agg.ingest({"svc": "gateway", "id": "g1", "ts": NOW_MS - 500, "received": 10})
    ov = agg.overview()
    assert [w["svc"] for w in ov["workers"]] == ["gateway"]
    assert ov["counters"] == {} and ov["oems"] == []


def test_message_without_id_or_timestamp_does_not_break_ingest(clock):
    agg = Aggregator()
    agg.ingest({"svc": "normalizer", "stats": {"ok": [["nordvik", 1, 4]]}})
    assert agg.totals[("nordvik", "ok")] == 4
    assert "normalizer:?" in agg.workers


def test_empty_aggregator_overview(clock):
    ov = Aggregator().overview()
    assert ov["now"] == NOW_MS and ov["uptime_s"] == 0
    assert ov["oems"] == [] and ov["workers"] == [] and ov["routing"] == {} and ov["simulator"] == {}
    assert ov["totals"] == {"ok": 0, "failed": 0, "duplicates": 0, "sent": 0}
    assert set(ov["throughput"].values()) == {0.0}


def test_uptime(clock):
    agg = Aggregator()
    clock.now += 125
    assert agg.overview()["uptime_s"] == 125


# ---------------------------------------------------------------------- series
def test_series_is_one_value_per_second_ending_at_the_last_full_second(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ts=NOW_MS - 1000, ok=[["nordvik", 1, 7]], failed=[["nordvik", "INVALID", 2]]))
    agg.ingest(normalizer_msg(ts=NOW_MS - 3000, ok=[["nordvik", 1, 5]], duplicates={"nordvik": 1}))
    s = agg.series("nordvik", seconds=5)
    assert s["now"] == NOW_MS - 1000 and s["step_s"] == 1 and s["oem"] == "nordvik"
    assert s["ok"] == [0, 0, 5, 0, 7]
    assert s["failed"] == [0, 0, 0, 0, 2]
    assert s["duplicates"] == [0, 0, 1, 0, 0]
    assert s["sent"] == [0] * 5


def test_series_for_all_sources_and_for_an_unknown_one(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ts=NOW_MS - 1000, ok=[["nordvik", 1, 7], ["pacifica", 1, 3]]))
    assert agg.series(seconds=3)["ok"] == [0, 0, 10]
    assert agg.series("ghost", seconds=3)["ok"] == [0, 0, 0]


def test_series_all(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ts=NOW_MS - 1000, ok=[["nordvik", 1, 7], ["pacifica", 1, 3]],
                              failed=[["helix", "NO_ADAPTER", 4]], duplicates={"nordvik": 2}))
    s = agg.series_all(seconds=3)
    assert s["seconds"] == 3
    assert s["ok"] == {"nordvik": [0, 0, 7], "pacifica": [0, 0, 3]}, "helix has no successful events"
    assert s["failed"] == [0, 0, 4] and s["duplicates"] == [0, 0, 2]


def test_series_all_without_failures(clock):
    s = Aggregator().series_all(seconds=4)
    assert s["ok"] == {} and s["failed"] == [0] * 4 and s["duplicates"] == [0] * 4


# ------------------------------------------------------------- minute rollups
def test_drain_minutes_returns_completed_minutes_once(clock):
    agg = Aggregator()
    minute = NOW_S // 60
    agg.ingest(normalizer_msg(ts=(minute - 2) * 60_000 + 5000, ok=[["nordvik", 1, 10]],
                              failed=[["nordvik", "INVALID", 1]]))
    agg.ingest(normalizer_msg(ts=(minute - 2) * 60_000 + 9000, ok=[["nordvik", 1, 5]], duplicates={"nordvik": 2}))
    agg.ingest(normalizer_msg(ts=minute * 60_000 + 1000, ok=[["nordvik", 1, 99]]))

    done = agg.drain_minutes(before_minute=minute)
    assert done == [{"oem_key": "nordvik", "minute": minute - 2, "ok": 15, "failed": 1, "duplicates": 2}]
    assert agg.drain_minutes(before_minute=minute) == []
    assert agg.drain_minutes(before_minute=minute + 1) == [{"oem_key": "nordvik", "minute": minute, "ok": 99}]


def test_sent_is_not_part_of_the_minute_rollup(clock):
    agg = Aggregator()
    agg.ingest({"svc": "simulator", "id": "s0", "ts": NOW_MS - 120_000, "by_oem": {"nordvik": 60}})
    assert agg.drain_minutes(before_minute=NOW_S // 60 + 1) == []


# ------------------------------------------------------------------ prometheus
def test_prometheus_exposition(clock):
    agg = Aggregator()
    agg.ingest({**normalizer_msg(ok=[["nordvik", 1, 90]], failed=[["nordvik", "INVALID", 10]],
                                 duplicates={"nordvik": 3}), "lag_records": 17, **hist(12.0)})
    text = agg.prometheus()
    lines = text.splitlines()
    assert text.endswith("\n")
    assert 'rosetta_events_total{oem="nordvik",outcome="ok"} 90' in lines
    assert 'rosetta_events_total{oem="nordvik",outcome="failed"} 10' in lines
    assert 'rosetta_events_total{oem="nordvik",outcome="duplicate"} 3' in lines
    assert 'rosetta_dead_letters_total{oem="nordvik",reason="INVALID"} 10' in lines
    assert "rosetta_throughput_events_per_second 9.0" in lines
    assert 'rosetta_consumer_lag_records{service="normalizer"} 17' in lines
    assert 'rosetta_consumer_lag_records{service="processor"} 0' in lines
    assert any(line.startswith('rosetta_latency_milliseconds{stage="normalize",quantile="p99"} ') for line in lines)


def test_prometheus_format_is_well_formed(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg(ok=[["nordvik", 1, 1]]))
    lines = agg.prometheus().splitlines()
    helps = [line.split()[2] for line in lines if line.startswith("# HELP")]
    types = [line.split()[2] for line in lines if line.startswith("# TYPE")]
    assert helps == types and len(set(helps)) == len(helps) == 6
    for line in lines:
        if not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            float(value)
            assert name.split("{")[0] in helps


def test_workers_reporting_tells_gone_from_idle(clock):
    agg = Aggregator()
    agg.ingest(normalizer_msg("n0"))                       # reported a second ago
    agg.ingest(normalizer_msg("n1", ts=NOW_MS - 60_000))   # silent for a minute
    text = agg.prometheus()
    assert 'rosetta_workers_reporting{service="normalizer"} 1' in text
    assert 'rosetta_workers_reporting{service="processor"} 0' in text   # listed even when none ever reported
    clock.now += 30
    assert 'rosetta_workers_reporting{service="normalizer"} 0' in agg.prometheus()


# --------------------------------------------------------------- MetricsReader
def test_metrics_reader_tails_the_topic_into_the_aggregator(tmp_path):
    broker = LogBroker(tmp_path / "log", partitions=1)
    publish(broker, "normalizer", "old", {"stats": {"ok": [["nordvik", 1, 1]]}})     # before the reader started
    agg = Aggregator()
    reader = MetricsReader(broker, agg, group="test-reader")
    reader.start()
    try:
        publish(broker, "normalizer", "n0", {"stats": {"ok": [["nordvik", 1, 12]]}})
        broker.produce(T_METRICS, [M.Record(key=b"x", value=b"not json")])          # must not kill the thread
        deadline = time.monotonic() + 3.0
        while "normalizer:n0" not in agg.workers and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        reader.stop()
        reader.join(timeout=3.0)
    assert not reader.is_alive()
    assert agg.totals[("nordvik", "ok")] == 12, "only what arrived after the reader started"
    assert "normalizer:old" not in agg.workers


class ScriptedConsumer:
    """Returns the prepared batches, then asks the reader to stop."""

    def __init__(self, batches: list[list[M.Record]]) -> None:
        self.batches = list(batches)
        self.reader: MetricsReader | None = None

    def poll(self, max_records: int = 1000, timeout_s: float = 0.5) -> list[M.Record]:
        if self.batches:
            return self.batches.pop(0)
        assert self.reader is not None
        self.reader.stop()
        return []


class ScriptedBroker:
    def __init__(self, consumer: ScriptedConsumer) -> None:
        self._consumer = consumer
        self.calls: list[tuple] = []

    def consumer(self, topic, group, start="committed"):
        self.calls.append((topic, group, start))
        return self._consumer


def snapshot(ident: str, n: int) -> M.Record:
    return M.Record(key=b"k", value=orjson.dumps({"svc": "normalizer", "id": ident, "ts": NOW_MS,
                                                   "stats": {"ok": [["nordvik", 1, n]]}}))


def run_reader(batches: list[list[M.Record]], monkeypatch) -> Aggregator:
    monkeypatch.setattr(M, "time", SimpleNamespace(time=time.time, time_ns=time.time_ns, sleep=lambda _s: None))
    consumer = ScriptedConsumer(batches)
    agg = Aggregator()
    reader = MetricsReader(ScriptedBroker(consumer), agg, group="g")
    consumer.reader = reader
    reader.run()
    return agg


def test_metrics_reader_starts_at_the_end_of_the_topic(monkeypatch):
    consumer = ScriptedConsumer([])
    broker = ScriptedBroker(consumer)
    MetricsReader(broker, Aggregator())
    topic, group, start = broker.calls[0]
    assert (topic, start) == (T_METRICS, "end")
    assert group.startswith("api-metrics-"), "every API process gets a group of its own"


def test_metrics_reader_ingests_every_batch(monkeypatch):
    agg = run_reader([[snapshot("n0", 5), snapshot("n1", 6)], [snapshot("n0", 7)]], monkeypatch)
    assert agg.totals[("nordvik", "ok")] == 18


def test_metrics_reader_survives_a_malformed_snapshot(monkeypatch):
    bad = M.Record(key=b"k", value=b"not json")
    agg = run_reader([[snapshot("n0", 5)], [bad], [snapshot("n1", 6)]], monkeypatch)
    assert agg.totals[("nordvik", "ok")] == 11


def test_a_malformed_snapshot_does_not_cost_the_rest_of_its_batch(monkeypatch):
    bad = M.Record(key=b"k", value=b"not json")
    agg = run_reader([[snapshot("n0", 5), bad, snapshot("n1", 6), snapshot("n2", 7)]], monkeypatch)
    assert agg.totals[("nordvik", "ok")] == 18
