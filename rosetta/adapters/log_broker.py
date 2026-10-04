"""LogBroker: a small partitioned, durable, replayable log on the local disk.

It gives the local mode the same semantics the pipeline relies on in Kafka:
  * topics split into partitions, records routed by key hash (per-key order)
  * append-only segments, so any consumer can rewind and replay
  * consumer groups with committed offsets, so a restarted worker resumes
  * size-based retention, oldest segment first

Record frame: | total_len u32 | crc32 u32 | ts i64 | key_len u16 | hdr_len u16 | key | headers | value |
The CRC makes a torn write (a crash mid-append) detectable: the reader stops
at the last complete record.
"""
from __future__ import annotations

import fcntl
import os
import struct
import time
import zlib
from collections.abc import Sequence
from pathlib import Path

import orjson
import xxhash

from ..ports.broker import Record

_BODY = struct.Struct("<qHH")
_LEN_CRC = struct.Struct("<II")
_EMPTY_HEADERS = b"{}"


def partition_for(key: bytes, n: int) -> int:
    return xxhash.xxh3_64_intdigest(key) % n


def _frame(rec: Record, now_ms: int, hdr: bytes) -> bytes:
    body = _BODY.pack(rec.ts or now_ms, len(rec.key), len(hdr)) + rec.key + hdr + rec.value
    return _LEN_CRC.pack(len(body) + 8, zlib.crc32(body)) + body


class _Partition:
    """One partition: a directory of segment files named by their base offset."""

    def __init__(self, root: Path, segment_bytes: int, retention_bytes: int) -> None:
        self.root = root
        self.segment_bytes = segment_bytes
        self.retention_bytes = retention_bytes
        root.mkdir(parents=True, exist_ok=True)
        self._lock_path = root / ".lock"
        self._lock_path.touch(exist_ok=True)
        self._good: dict[Path, int] = {}      # per segment: end of the last frame known complete

    def segments(self) -> list[tuple[int, Path]]:
        out = []
        for p in self.root.glob("*.seg"):
            try:
                out.append((int(p.stem), p))
            except ValueError:
                continue
        out.sort()
        return out

    def end_offset(self) -> int:
        segs = self.segments()
        if not segs:
            return 0
        base, path = segs[-1]
        try:
            return base + path.stat().st_size
        except FileNotFoundError:
            return base

    def start_offset(self) -> int:
        segs = self.segments()
        return segs[0][0] if segs else 0

    def append(self, blob: bytes) -> None:
        with open(self._lock_path) as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                segs = self.segments()
                if segs:
                    base, path = segs[-1]
                    size = path.stat().st_size
                    if size >= self.segment_bytes:
                        base, path = base + size, self.root / f"{base + size:020d}.seg"
                        self._enforce_retention(segs)
                else:
                    base, path = 0, self.root / f"{0:020d}.seg"
                self._repair_tail(path)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, blob)
                finally:
                    os.close(fd)
                self._good[path] = self._good.get(path, 0) + len(blob)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _repair_tail(self, path: Path) -> None:
        """Cut off a torn frame left by a writer that crashed mid-append.

        Readers stop at an incomplete or corrupt frame, so without this every
        record appended after a crash would be unreachable. Called under the
        partition lock. Only bytes written since this writer last looked are
        walked, and only frame headers are parsed, except for the last frame,
        whose checksum is verified.
        """
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self._good[path] = 0
            return
        good = self._good.get(path, 0)
        if good == size:
            return
        if good > size:
            good = 0
        with open(path, "rb") as f:
            f.seek(good)
            buf = f.read(size - good)
        pos, last = 0, -1
        n = len(buf)
        while pos + 8 <= n:
            total, _crc = _LEN_CRC.unpack_from(buf, pos)
            if total < 20 or pos + total > n:
                break
            last, pos = pos, pos + total
        valid = pos
        if last >= 0 and pos == n:
            total, crc = _LEN_CRC.unpack_from(buf, last)
            if zlib.crc32(buf[last + 8:last + total]) != crc:
                valid = last
        if good + valid < size:
            os.truncate(path, good + valid)
        self._good[path] = good + valid

    def _enforce_retention(self, segs: list[tuple[int, Path]]) -> None:
        total = 0
        sizes = []
        for _base, path in segs:
            try:
                sz = path.stat().st_size
            except FileNotFoundError:
                sz = 0
            sizes.append(sz)
            total += sz
        i = 0
        while total > self.retention_bytes and i < len(segs) - 1:
            try:
                segs[i][1].unlink()
            except FileNotFoundError:
                pass
            total -= sizes[i]
            i += 1

    def read(self, offset: int, max_records: int, max_bytes: int = 8 << 20) -> tuple[list[Record], int]:
        """Read complete records starting at `offset`. Returns (records, next_offset)."""
        segs = self.segments()
        if not segs:
            return [], offset
        if offset < segs[0][0]:
            offset = segs[0][0]  # retention removed what we wanted: jump to the oldest record
        out: list[Record] = []
        for n, (base, path) in enumerate(segs):
            nxt = segs[n + 1][0] if n + 1 < len(segs) else None
            if nxt is not None and offset >= nxt:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(offset - base)
                    buf = f.read(max_bytes)
            except FileNotFoundError:
                continue
            pos, size = 0, len(buf)
            unpack_lc, unpack_b, crc32 = _LEN_CRC.unpack_from, _BODY.unpack_from, zlib.crc32
            hcache: dict[bytes, dict] = {}
            while pos + 8 <= size and len(out) < max_records:
                total, crc = unpack_lc(buf, pos)
                if total < 20 or pos + total > size:
                    break  # partial record: the writer is still appending
                body = buf[pos + 8:pos + total]
                if crc32(body) != crc:
                    break  # torn write
                ts, klen, hlen = unpack_b(body, 0)
                k1 = 12 + klen
                hdr = body[k1:k1 + hlen]
                # Consecutive records often carry identical headers: parse once.
                # Consumers treat headers as read-only and copy before changing them.
                headers = hcache.get(hdr)
                if headers is None:
                    headers = hcache[hdr] = orjson.loads(hdr) if hlen > 2 else {}
                out.append(Record(body[12:k1], body[k1 + hlen:], headers, ts, -1,
                                  offset + pos, offset + pos + total))
                pos += total
            offset += pos
            if len(out) >= max_records or nxt is None or offset < nxt:
                break
        return out, offset


class LogConsumer:
    def __init__(self, broker: LogBroker, topic: str, group: str, partitions: Sequence[int], start: str) -> None:
        self.broker = broker
        self.topic = topic
        self.group = group
        self.parts = list(partitions)
        self._dir = broker.root / "_offsets" / topic / group
        self._dir.mkdir(parents=True, exist_ok=True)
        self.pos: dict[int, int] = {}
        self._rr = 0
        for p in self.parts:
            committed = self._read_committed(p)
            if start == "beginning":
                self.pos[p] = broker._part(topic, p).start_offset()
            elif start == "end" or (committed is None and start == "latest"):
                self.pos[p] = broker._part(topic, p).end_offset()
            else:
                self.pos[p] = committed if committed is not None else broker._part(topic, p).start_offset()

    def _read_committed(self, p: int) -> int | None:
        try:
            return int((self._dir / str(p)).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def poll(self, max_records: int = 1000, timeout_s: float = 0.5) -> list[Record]:
        deadline = time.monotonic() + timeout_s
        n = len(self.parts)
        while True:
            out: list[Record] = []
            for i in range(n):
                p = self.parts[(self._rr + i) % n]
                recs, nxt = self.broker._part(self.topic, p).read(self.pos[p], max_records - len(out))
                if recs:
                    for r in recs:
                        r.partition = p
                    out.extend(recs)
                self.pos[p] = nxt
                if len(out) >= max_records:
                    break
            self._rr = (self._rr + 1) % max(1, n)
            if out or time.monotonic() >= deadline:
                return out
            time.sleep(0.005)

    def commit(self) -> None:
        for p, off in self.pos.items():
            tmp = self._dir / f".{p}.{os.getpid()}.tmp"
            tmp.write_text(str(off))
            os.replace(tmp, self._dir / str(p))

    def committed(self) -> dict[int, int]:
        return {p: (self._read_committed(p) or 0) for p in self.parts}

    def lag(self) -> int:
        return sum(max(0, self.broker._part(self.topic, p).end_offset() - off) for p, off in self.pos.items())

    def positions(self) -> dict[int, int]:
        return dict(self.pos)

    def seek(self, partition: int, offset: int) -> None:
        self.pos[partition] = offset

    def seek_to_beginning(self) -> None:
        for p in self.parts:
            self.pos[p] = self.broker._part(self.topic, p).start_offset()

    def seek_to_end(self) -> None:
        for p in self.parts:
            self.pos[p] = self.broker._part(self.topic, p).end_offset()

    def close(self) -> None:
        pass


class LogBroker:
    def __init__(self, root: str | os.PathLike, partitions: int = 8,
                 segment_bytes: int = 32 << 20, retention_bytes: int = 192 << 20,
                 topic_partitions: dict[str, int] | None = None,
                 topic_retention_bytes: dict[str, int] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.default_partitions = partitions
        self.segment_bytes = segment_bytes
        self.retention_bytes = retention_bytes
        self.topic_partitions = dict(topic_partitions or {})
        self.topic_retention_bytes = dict(topic_retention_bytes or {})
        self._parts: dict[tuple[str, int], _Partition] = {}

    def partitions(self, topic: str) -> int:
        n = self.topic_partitions.get(topic)
        if n is None:
            meta = self.root / topic / "partitions"
            try:
                n = int(meta.read_text())
            except (FileNotFoundError, ValueError):
                n = self.default_partitions
                meta.parent.mkdir(parents=True, exist_ok=True)
                meta.write_text(str(n))
            self.topic_partitions[topic] = n
        return n

    def _part(self, topic: str, p: int) -> _Partition:
        part = self._parts.get((topic, p))
        if part is None:
            budget = self.topic_retention_bytes.get(topic, self.retention_bytes)
            per_part = max(self.segment_bytes, budget // max(1, self.partitions(topic)))
            part = _Partition(self.root / topic / f"p{p:03d}", self.segment_bytes, per_part)
            self._parts[(topic, p)] = part
        return part

    def produce(self, topic: str, records: Sequence[Record]) -> None:
        if not records:
            return
        n = self.partitions(topic)
        now = int(time.time() * 1000)
        buckets: dict[int, list[bytes]] = {}
        # A batch usually shares one headers object: serialise it once, not per record.
        hcache: dict[int, bytes] = {}
        h64 = xxhash.xxh3_64_intdigest
        for r in records:
            p = r.partition if 0 <= r.partition < n else h64(r.key) % n
            h = r.headers
            if h:
                hb = hcache.get(id(h))
                if hb is None:
                    hb = hcache[id(h)] = orjson.dumps(h)
            else:
                hb = _EMPTY_HEADERS
            b = buckets.get(p)
            if b is None:
                b = buckets[p] = []
            b.append(_frame(r, now, hb))
        for p, frames in buckets.items():
            self._part(topic, p).append(b"".join(frames))

    def consumer(self, topic: str, group: str, partitions: Sequence[int] | None = None,
                 start: str = "committed") -> LogConsumer:
        parts = list(partitions) if partitions is not None else list(range(self.partitions(topic)))
        return LogConsumer(self, topic, group, parts, start)

    def end_offsets(self, topic: str) -> dict[int, int]:
        return {p: self._part(topic, p).end_offset() for p in range(self.partitions(topic))}

    def bytes_on_disk(self, topic: str) -> int:
        total = 0
        for p in range(self.partitions(topic)):
            for _, path in self._part(topic, p).segments():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def close(self) -> None:
        self._parts.clear()
