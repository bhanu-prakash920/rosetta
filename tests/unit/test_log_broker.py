"""rosetta.adapters.log_broker: a partitioned, durable, replayable log on the local disk."""
from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path

import pytest

from rosetta.adapters.log_broker import LogBroker, partition_for
from rosetta.ports.broker import Record

TOPIC = "telemetry.raw"


def rec(key: str, value: str, **headers) -> Record:
    return Record(key=key.encode(), value=value.encode(), headers=headers)


def drain(consumer, batch: int = 1000) -> list[Record]:
    out: list[Record] = []
    while True:
        got = consumer.poll(batch, 0.0)
        if not got:
            return out
        out.extend(got)


def segment_files(root: Path, topic: str = TOPIC, partition: int = 0) -> list[Path]:
    return sorted((root / topic / f"p{partition:03d}").glob("*.seg"))


@pytest.fixture()
def broker(tmp_path) -> LogBroker:
    return LogBroker(tmp_path / "log", partitions=4)


@pytest.fixture()
def single(tmp_path) -> LogBroker:
    return LogBroker(tmp_path / "log", partitions=1)


# ------------------------------------------------------------------ round trip
def test_record_round_trip(single):
    single.produce(TOPIC, [Record(key=b"dev-1", value=b"\x00\x01payload\xff", headers={"oem": "nordvik", "rx": 5},
                                  ts=1_790_000_000_123)])
    got = single.consumer(TOPIC, "g", start="beginning").poll(10, 0.0)
    assert len(got) == 1
    r = got[0]
    assert (r.key, r.value, r.headers, r.ts) == (b"dev-1", b"\x00\x01payload\xff", {"oem": "nordvik", "rx": 5},
                                                 1_790_000_000_123)
    assert r.partition == 0 and r.offset == 0
    assert r.next_offset == segment_files(single.root)[0].stat().st_size


def test_append_time_is_stamped_when_the_record_has_none(single):
    before = int(time.time() * 1000)
    single.produce(TOPIC, [rec("k", "v")])
    got = single.consumer(TOPIC, "g", start="beginning").poll(10, 0.0)[0]
    assert before <= got.ts <= int(time.time() * 1000)


def test_empty_key_value_and_headers(single):
    single.produce(TOPIC, [Record(key=b"", value=b""), Record(key=b"k", value=b"v", headers={})])
    got = single.consumer(TOPIC, "g", start="beginning").poll(10, 0.0)
    assert [(r.key, r.value, r.headers) for r in got] == [(b"", b"", {}), (b"k", b"v", {})]


def test_large_value(single):
    blob = bytes(range(256)) * 1024                      # 256 KiB
    single.produce(TOPIC, [Record(key=b"k", value=blob)])
    assert single.consumer(TOPIC, "g", start="beginning").poll(10, 0.0)[0].value == blob


def test_headers_shared_by_a_batch_are_written_for_every_record(single):
    shared = {"oem": "nordvik", "rx": 1}
    single.produce(TOPIC, [Record(key=b"a", value=b"1", headers=shared), Record(key=b"b", value=b"2", headers=shared),
                           Record(key=b"c", value=b"3", headers={"oem": "helix"})])
    got = single.consumer(TOPIC, "g", start="beginning").poll(10, 0.0)
    assert [r.headers for r in got] == [shared, shared, {"oem": "helix"}]


def test_producing_nothing_creates_nothing(single):
    single.produce(TOPIC, [])
    assert segment_files(single.root) == []
    assert single.consumer(TOPIC, "g").poll(10, 0.0) == []


def test_offsets_are_byte_positions_and_chain(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(5)])
    got = single.consumer(TOPIC, "g", start="beginning").poll(10, 0.0)
    assert got[0].offset == 0
    for a, b in zip(got, got[1:]):
        assert b.offset == a.next_offset
    assert got[-1].next_offset == single.end_offsets(TOPIC)[0]


# ---------------------------------------------------------------- partitioning
def test_partition_for_is_stable_and_in_range():
    for n in (1, 2, 8, 13):
        for i in range(200):
            p = partition_for(f"dev-{i}".encode(), n)
            assert 0 <= p < n
            assert p == partition_for(f"dev-{i}".encode(), n)


def test_keys_are_spread_over_all_partitions():
    assert {partition_for(f"dev-{i}".encode(), 8) for i in range(500)} == set(range(8))


def test_records_of_one_key_land_in_one_partition(broker):
    broker.produce(TOPIC, [rec("dev-7", f"v{i}") for i in range(20)])
    got = drain(broker.consumer(TOPIC, "g", start="beginning"))
    assert {r.partition for r in got} == {partition_for(b"dev-7", 4)}


def test_per_key_order_is_preserved(broker):
    keys = [f"dev-{i}" for i in range(25)]
    for n in range(40):                                  # 40 appends, interleaving all keys
        broker.produce(TOPIC, [rec(k, f"{n:04d}") for k in keys])
    got = drain(broker.consumer(TOPIC, "g", start="beginning"), batch=137)
    assert len(got) == 25 * 40
    by_key: dict[bytes, list[bytes]] = {}
    for r in got:
        by_key.setdefault(r.key, []).append(r.value)
    assert set(by_key) == {k.encode() for k in keys}
    for values in by_key.values():
        assert values == [f"{n:04d}".encode() for n in range(40)]


def test_explicit_partition_is_honoured(broker):
    broker.produce(TOPIC, [Record(key=b"dev-1", value=b"v", partition=3)])
    got = drain(broker.consumer(TOPIC, "g", start="beginning"))
    assert [r.partition for r in got] == [3]


@pytest.mark.parametrize("partition", [-1, 4, 99])
def test_partition_out_of_range_falls_back_to_the_key_hash(broker, partition):
    broker.produce(TOPIC, [Record(key=b"dev-1", value=b"v", partition=partition)])
    got = drain(broker.consumer(TOPIC, "g", start="beginning"))
    assert [r.partition for r in got] == [partition_for(b"dev-1", 4)]


def test_two_partitions(tmp_path):
    b = LogBroker(tmp_path / "log", partitions=2)
    sent = [rec(f"dev-{i}", f"v{i}") for i in range(200)]
    b.produce(TOPIC, sent)
    assert b.partitions(TOPIC) == 2
    assert sorted(b.end_offsets(TOPIC)) == [0, 1]
    assert all(off > 0 for off in b.end_offsets(TOPIC).values())

    p0 = drain(b.consumer(TOPIC, "w0", partitions=[0], start="beginning"))
    p1 = drain(b.consumer(TOPIC, "w1", partitions=[1], start="beginning"))
    assert {r.partition for r in p0} == {0} and {r.partition for r in p1} == {1}
    assert all(partition_for(r.key, 2) == r.partition for r in p0 + p1)
    assert sorted(r.value for r in p0 + p1) == sorted(r.value for r in sent)
    assert not {r.key for r in p0} & {r.key for r in p1}, "two workers never see the same vehicle"


def test_consumer_without_a_partition_list_reads_them_all(broker):
    broker.produce(TOPIC, [rec(f"dev-{i}", "v") for i in range(100)])
    c = broker.consumer(TOPIC, "g", start="beginning")
    assert sorted(c.positions()) == [0, 1, 2, 3]
    assert {r.partition for r in drain(c)} == {0, 1, 2, 3}


def test_partition_count_is_remembered_on_disk(tmp_path):
    LogBroker(tmp_path / "log", partitions=4).produce(TOPIC, [rec("k", "v")])
    reopened = LogBroker(tmp_path / "log", partitions=16)
    assert reopened.partitions(TOPIC) == 4
    assert reopened.partitions("another.topic") == 16


def test_topic_partitions_override(tmp_path):
    b = LogBroker(tmp_path / "log", partitions=8, topic_partitions={"telemetry.dlq": 2})
    assert b.partitions("telemetry.dlq") == 2
    assert b.partitions(TOPIC) == 8


def test_topics_are_independent(single):
    single.produce("a", [rec("k", "in-a")])
    single.produce("b", [rec("k", "in-b")])
    assert [r.value for r in drain(single.consumer("a", "g", start="beginning"))] == [b"in-a"]
    assert [r.value for r in drain(single.consumer("b", "g", start="beginning"))] == [b"in-b"]


# ------------------------------------------------------------ replay, commits
def test_replay_from_the_beginning(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(10)])
    c = single.consumer(TOPIC, "g", start="beginning")
    first = [r.value for r in drain(c)]
    assert first == [f"v{i}".encode() for i in range(10)]
    assert c.poll(10, 0.0) == []
    c.seek_to_beginning()
    assert [r.value for r in drain(c)] == first


def test_a_new_group_can_replay_what_another_group_already_consumed(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(10)])
    a = single.consumer(TOPIC, "group-a")
    drain(a)
    a.commit()
    assert len(drain(single.consumer(TOPIC, "group-b"))) == 10


def test_committed_offsets_survive_a_new_consumer(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(10)])
    c = single.consumer(TOPIC, "workers")
    assert [r.value for r in c.poll(4, 0.0)] == [b"v0", b"v1", b"v2", b"v3"]
    c.commit()
    c.close()

    restarted = single.consumer(TOPIC, "workers")
    assert [r.value for r in drain(restarted)] == [f"v{i}".encode() for i in range(4, 10)]


def test_committed_offsets_survive_a_new_broker_instance(tmp_path):
    b = LogBroker(tmp_path / "log", partitions=1)
    b.produce(TOPIC, [rec("k", f"v{i}") for i in range(6)])
    c = b.consumer(TOPIC, "workers")
    c.poll(2, 0.0)
    c.commit()
    b.close()

    again = LogBroker(tmp_path / "log", partitions=1).consumer(TOPIC, "workers")
    assert [r.value for r in drain(again)] == [b"v2", b"v3", b"v4", b"v5"]


def test_uncommitted_progress_is_redelivered(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(5)])
    crashed = single.consumer(TOPIC, "workers")
    crashed.poll(3, 0.0)                                  # no commit: the worker died
    assert [r.value for r in drain(single.consumer(TOPIC, "workers"))] == [f"v{i}".encode() for i in range(5)]


def test_committed_reports_what_was_stored(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(5)])
    c = single.consumer(TOPIC, "workers")
    assert c.committed() == {0: 0}
    got = c.poll(2, 0.0)
    c.commit()
    assert c.committed() == {0: got[-1].next_offset} == c.positions()


def test_start_beginning_ignores_the_committed_offset(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(5)])
    c = single.consumer(TOPIC, "workers")
    drain(c)
    c.commit()
    assert len(drain(single.consumer(TOPIC, "workers", start="beginning"))) == 5


def test_start_end_skips_what_is_already_there(single):
    single.produce(TOPIC, [rec("k", "old")])
    c = single.consumer(TOPIC, "tail", start="end")
    assert c.poll(10, 0.0) == []
    single.produce(TOPIC, [rec("k", "new")])
    assert [r.value for r in c.poll(10, 0.0)] == [b"new"]


def test_start_latest_uses_the_commit_when_there_is_one(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(4)])
    fresh = single.consumer(TOPIC, "g", start="latest")
    assert fresh.poll(10, 0.0) == [], "no commit yet: start at the end"

    worker = single.consumer(TOPIC, "workers")
    worker.poll(1, 0.0)
    worker.commit()
    assert [r.value for r in drain(single.consumer(TOPIC, "workers", start="latest"))] == [b"v1", b"v2", b"v3"]


def test_seek_and_seek_to_end(single):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(5)])
    c = single.consumer(TOPIC, "g", start="beginning")
    got = c.poll(10, 0.0)
    c.seek(0, got[3].offset)
    assert [r.value for r in c.poll(10, 0.0)] == [b"v3", b"v4"]
    c.seek_to_beginning()
    c.seek_to_end()
    assert c.poll(10, 0.0) == []


def test_corrupt_commit_file_is_treated_as_no_commit(single):
    single.produce(TOPIC, [rec("k", "v")])
    c = single.consumer(TOPIC, "workers")
    drain(c)
    c.commit()
    (single.root / "_offsets" / TOPIC / "workers" / "0").write_text("not a number")
    assert len(drain(single.consumer(TOPIC, "workers"))) == 1


# ------------------------------------------------------------------------ poll
def test_poll_respects_max_records(broker):
    broker.produce(TOPIC, [rec(f"dev-{i}", "v") for i in range(100)])
    c = broker.consumer(TOPIC, "g", start="beginning")
    sizes = []
    while True:
        got = c.poll(7, 0.0)
        if not got:
            break
        sizes.append(len(got))
    assert sum(sizes) == 100 and max(sizes) <= 7


def test_poll_on_an_empty_topic_returns_after_the_timeout(single):
    c = single.consumer(TOPIC, "g")
    started = time.monotonic()
    assert c.poll(10, 0.05) == []
    assert 0.04 <= time.monotonic() - started < 1.0


def test_poll_sees_records_produced_by_another_broker_instance(tmp_path):
    writer = LogBroker(tmp_path / "log", partitions=1)
    reader = LogBroker(tmp_path / "log", partitions=1).consumer(TOPIC, "g")
    writer.produce(TOPIC, [rec("k", "one")])
    assert [r.value for r in reader.poll(10, 0.0)] == [b"one"]
    writer.produce(TOPIC, [rec("k", "two")])
    assert [r.value for r in reader.poll(10, 0.0)] == [b"two"]


def test_lag_counts_what_is_left_to_read(single):
    c = single.consumer(TOPIC, "g")
    assert c.lag() == 0
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(4)])
    total = single.end_offsets(TOPIC)[0]
    assert c.lag() == total
    got = c.poll(1, 0.0)
    assert c.lag() == total - got[0].next_offset
    drain(c)
    assert c.lag() == 0


# ------------------------------------------------------------------ torn write
def frame(body: bytes, crc: int | None = None, length: int | None = None) -> bytes:
    return struct.pack("<II", len(body) + 8 if length is None else length,
                       zlib.crc32(body) if crc is None else crc) + body


GOOD_BODY = struct.pack("<qHH", 1_790_000_000_000, 1, 2) + b"k" + b"{}" + b"value"

TORN_TAILS = {
    "three-bytes-of-a-header": b"\x40\x00\x00",
    "header-only": struct.pack("<II", 64, 12345),
    "length-beyond-the-end": frame(GOOD_BODY, length=len(GOOD_BODY) + 8 + 500),
    "half-a-record": frame(GOOD_BODY)[: len(GOOD_BODY) // 2],
    "checksum-mismatch": frame(GOOD_BODY, crc=zlib.crc32(GOOD_BODY) ^ 1),
    "flipped-byte": frame(GOOD_BODY[:-1] + b"X", crc=zlib.crc32(GOOD_BODY)),
    "length-too-small": struct.pack("<II", 10, 0) + b"xx",
    "zeros": b"\x00" * 64,
    "text": b"garbage appended by a crash",
}


def test_the_frame_helper_writes_what_the_broker_reads(single):
    single.produce(TOPIC, [rec("a", "1")])
    with open(segment_files(single.root)[0], "ab") as f:
        f.write(frame(GOOD_BODY))
    got = drain(single.consumer(TOPIC, "g", start="beginning"))
    assert [(r.key, r.value, r.ts) for r in got][1] == (b"k", b"value", 1_790_000_000_000)


@pytest.mark.parametrize("tail", TORN_TAILS.values(), ids=TORN_TAILS.keys())
def test_torn_write_at_the_tail_is_not_returned(single, tail):
    single.produce(TOPIC, [rec("k", f"v{i}") for i in range(3)])
    complete = single.end_offsets(TOPIC)[0]
    with open(segment_files(single.root)[-1], "ab") as f:
        f.write(tail)

    c = single.consumer(TOPIC, "g", start="beginning")
    got = drain(c)
    assert [r.value for r in got] == [b"v0", b"v1", b"v2"]
    assert c.positions() == {0: complete}, "the reader stops at the last complete record"
    assert c.poll(10, 0.0) == []


def test_partial_record_becomes_readable_once_it_is_complete(single):
    single.produce(TOPIC, [rec("k", "v0")])
    whole = frame(GOOD_BODY)
    path = segment_files(single.root)[0]
    c = single.consumer(TOPIC, "g", start="beginning")
    with open(path, "ab") as f:
        f.write(whole[:10])
    assert [r.value for r in drain(c)] == [b"v0"]
    with open(path, "ab") as f:
        f.write(whole[10:])
    assert [r.value for r in drain(c)] == [b"value"]


def test_records_appended_after_a_torn_write_are_delivered(single):
    single.produce(TOPIC, [rec("k", "before")])
    with open(segment_files(single.root)[-1], "ab") as f:
        f.write(TORN_TAILS["half-a-record"])
    single.produce(TOPIC, [rec("k", "after")])            # the process restarted and carries on
    got = drain(single.consumer(TOPIC, "g", start="beginning"))
    assert [r.value for r in got] == [b"before", b"after"]


# ---------------------------------------------------------- segments, retention
def small_segments(tmp_path, retention: int) -> LogBroker:
    return LogBroker(tmp_path / "log", partitions=1, segment_bytes=1000, retention_bytes=retention)


def fill(broker: LogBroker, n: int, start: int = 0) -> None:
    for i in range(start, start + n):                     # one append per record, each 123 bytes on disk
        broker.produce(TOPIC, [Record(key=b"k", value=f"{i:04d}".encode() + b"x" * 96)])


def test_log_rolls_to_a_new_segment_when_one_is_full(tmp_path):
    b = small_segments(tmp_path, retention=1 << 20)
    fill(b, 30)
    files = segment_files(b.root)
    assert len(files) == 4
    assert [int(f.stem) for f in files] == sorted(int(f.stem) for f in files)
    assert int(files[0].stem) == 0
    assert int(files[1].stem) == files[0].stat().st_size, "a segment is named by its base offset"
    assert b.bytes_on_disk(TOPIC) == 30 * 123 == b.end_offsets(TOPIC)[0]


def test_reading_crosses_segment_boundaries(tmp_path):
    b = small_segments(tmp_path, retention=1 << 20)
    fill(b, 30)
    c = b.consumer(TOPIC, "g", start="beginning")
    got = drain(c, batch=7)
    assert [r.value[:4] for r in got] == [f"{i:04d}".encode() for i in range(30)]
    assert [r.offset for r in got] == [i * 123 for i in range(30)]


def test_retention_deletes_the_oldest_segment(tmp_path):
    b = small_segments(tmp_path, retention=3000)
    fill(b, 9)
    assert int(segment_files(b.root)[0].stem) == 0
    fill(b, 91, start=9)

    files = segment_files(b.root)
    assert int(files[0].stem) > 0, "segment 0 is gone"
    assert b.bytes_on_disk(TOPIC) <= 3000 + 1000
    assert b.end_offsets(TOPIC)[0] == 100 * 123, "offsets keep counting after a deletion"

    got = drain(b.consumer(TOPIC, "late", start="beginning"))
    assert got[0].offset == int(files[0].stem)
    values = [int(r.value[:4]) for r in got]
    assert values == list(range(values[0], 100)), "what is left is complete and in order"
    assert 0 < len(values) < 100


def test_consumer_behind_the_retention_point_jumps_to_the_oldest_record(tmp_path):
    b = small_segments(tmp_path, retention=3000)
    fill(b, 5)
    slow = b.consumer(TOPIC, "slow", start="beginning")
    assert int(slow.poll(1, 0.0)[0].value[:4]) == 0
    fill(b, 95, start=5)

    got = drain(slow)
    oldest = int(segment_files(b.root)[0].stem)
    assert got[0].offset == oldest
    assert int(got[-1].value[:4]) == 99


def test_retention_never_deletes_the_segment_being_written(tmp_path):
    b = LogBroker(tmp_path / "log", partitions=1, segment_bytes=1000, retention_bytes=1)
    fill(b, 40)
    assert len(segment_files(b.root)) >= 1
    got = drain(b.consumer(TOPIC, "g", start="beginning"))
    assert int(got[-1].value[:4]) == 39


def test_retention_budget_is_shared_by_the_partitions_of_a_topic(tmp_path):
    b = LogBroker(tmp_path / "log", partitions=2, segment_bytes=1000, retention_bytes=1 << 20,
                  topic_retention_bytes={TOPIC: 4000})
    assert b._part(TOPIC, 0).retention_bytes == 2000
    assert b._part("other", 0).retention_bytes == (1 << 20) // 2


def test_a_partition_always_keeps_at_least_one_segment_worth(tmp_path):
    b = LogBroker(tmp_path / "log", partitions=8, segment_bytes=1000, retention_bytes=800)
    assert b._part(TOPIC, 0).retention_bytes == 1000


def test_files_that_are_not_segments_are_ignored(single):
    single.produce(TOPIC, [rec("k", "v")])
    part_dir = single.root / TOPIC / "p000"
    (part_dir / "notes.seg").write_bytes(b"junk")
    (part_dir / "README.txt").write_text("hello")
    assert [r.value for r in drain(single.consumer(TOPIC, "g", start="beginning"))] == [b"v"]


def test_close_forgets_cached_partitions_but_not_the_data(single):
    single.produce(TOPIC, [rec("k", "v")])
    single.close()
    assert single._parts == {}
    assert [r.value for r in drain(single.consumer(TOPIC, "g", start="beginning"))] == [b"v"]
