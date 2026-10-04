"""Dead-letter indexer and replayer.

Nothing that fails is thrown away. A message that cannot be normalised is
parked on the dead-letter topic with a reason code. This worker

  * indexes dead letters into groups (source, reason, field, payload shape), so
    an operator sees "3 kinds of problem", not 40,000 rows
  * keeps a few sample payloads per group for the mapping agent
  * replays parked messages through the pipeline once a mapping can read them

Replay is what turns "onboarding a new OEM" into a zero-downtime operation:
the messages that arrived before the mapping existed are not lost, they are
late.
"""
from __future__ import annotations

import os
import signal
import time
from datetime import UTC, datetime
from typing import Any

import orjson
import xxhash
from sqlalchemy import select, update

from ..config import get_settings
from ..db.models import DlqGroup, DlqSample, ReplayJob
from ..db.session import session_scope
from ..engine.decoders import flatten
from ..factory import flush_broker, make_broker
from ..ports.broker import T_DLQ, T_RAW, Record
from . import metrics

MAX_SAMPLES = 60            # per group: keeps every payload shape represented
TRACKED_DEVICES = 24         # per source: devices whose consecutive messages are kept
TRACKED_MESSAGES = 40        # per tracked device
MAX_ATTEMPTS = 5
# Reasons that mean "we cannot read this format", as opposed to "this message is damaged".
FORMAT_REASONS = frozenset(("NO_ADAPTER", "SCHEMA_MISMATCH", "TRANSFORM_ERROR"))


def shape_of(payload: bytes, content_type: str = "") -> tuple[str, frozenset]:
    """Structural signature of a payload: a short hash and the set of field paths."""
    if payload[:1] in (b"{", b"["):
        try:
            keys = frozenset(flatten(orjson.loads(payload)).keys())
            h = xxhash.xxh3_64_hexdigest("\x1f".join(sorted(keys)))[:12]
            return f"json:{h}", keys
        except Exception:
            pass
    try:
        text = payload.decode("utf-8")
        for d in ("|", ";", ",", "\t"):
            if text.count(d) >= 3:
                n = text.count(d) + 1
                return f"text:{d}:{n}", frozenset(f"col{i}" for i in range(n))
        return "text:plain", frozenset()
    except UnicodeDecodeError:
        return f"binary:{len(payload) // 16 * 16}", frozenset()


class DlqWorker:
    def __init__(self, broker: Any = None, group: str = "dlq-indexer") -> None:
        self.broker = broker or make_broker(get_settings())
        self.consumer = self.broker.consumer(T_DLQ, group)
        self.running = True
        self.indexed = 0
        self.replayed = 0
        self._last_pub = time.monotonic()
        self._last_jobs = 0.0
        self._sample_counts: dict[tuple, int] = {}
        self._tracked: dict[str, dict[str, int]] = {}

    # ---------------------------------------------------------------- indexing
    def index_batch(self, recs: list[Record]) -> int:
        groups: dict[tuple, dict[str, Any]] = {}
        for r in recs:
            h = r.headers
            shape, _ = shape_of(r.value, h.get("ct", ""))
            key = (h.get("oem", "unknown"), h.get("reason", "?"), h.get("field", ""), shape)
            g = groups.get(key)
            ts = int(h.get("dead_ts") or r.ts or time.time() * 1000)
            if g is None:
                g = groups[key] = {"n": 0, "first": ts, "last": ts, "detail": h.get("detail", ""),
                                   "ct": h.get("ct", ""), "samples": []}
            g["n"] += 1
            g["last"] = max(g["last"], ts)
            dev = h.get("dev", "")
            if key[1] in FORMAT_REASONS and dev:
                # Follow a few devices closely. Consecutive messages of one vehicle are
                # what lets the mapper check a field against physics (speed vs GPS).
                tr = self._tracked.setdefault(key[0], {})
                seen = tr.get(dev)
                if seen is None and len(tr) < TRACKED_DEVICES:
                    seen = tr[dev] = 0
                if seen is not None and seen < TRACKED_MESSAGES:
                    tr[dev] = seen + 1
                    g["tracked"] = g.get("tracked", [])
                    g["tracked"].append((r.value, dev, ts))
                    continue
            if len(g["samples"]) < 8:
                g["samples"].append((r.value, dev, ts))
        if not groups:
            return 0
        with session_scope() as s:
            for (oem, reason, fld, shape), g in groups.items():
                row = s.execute(select(DlqGroup).where(
                    DlqGroup.oem_key == oem, DlqGroup.reason == reason,
                    DlqGroup.field == fld, DlqGroup.shape == shape)).scalar_one_or_none()
                if row is None:
                    first = (g["samples"] or g.get("tracked") or [(b"", "", 0)])[0][0]
                    row = DlqGroup(oem_key=oem, reason=reason, field=fld, shape=shape, count=0,
                                   first_seen=g["first"], last_seen=g["last"], detail=g["detail"][:200],
                                   sample=first, content_type=g["ct"])
                    s.add(row)
                    s.flush()
                row.count += g["n"]
                row.last_seen = max(row.last_seen, g["last"])
                key = (oem, reason, fld, shape)
                have = self._sample_counts.get(key, 0)
                for payload, dev, ts in g["samples"]:
                    if have >= MAX_SAMPLES:
                        break
                    s.add(DlqSample(group_id=row.id, payload=payload, device=dev, ts=ts))
                    have += 1
                self._sample_counts[key] = have
                for payload, dev, ts in g.get("tracked", ()):
                    s.add(DlqSample(group_id=row.id, payload=payload, device=dev, ts=ts, tracked=True))
        n = sum(g["n"] for g in groups.values())
        self.indexed += n
        return n

    # ------------------------------------------------------------------ replay
    def run_replay(self, job_id: int, max_records: int = 5_000_000) -> dict[str, int]:
        with session_scope() as s:
            job = s.get(ReplayJob, job_id)
            if job is None:
                return {}
            oem = job.oem_key
            job.status = "running"
        # One cursor per source: a second replay continues where the first stopped.
        cur = self.broker.consumer(T_DLQ, f"replay-{oem}")
        end = self.broker.end_offsets(T_DLQ)
        scanned = sent = skipped = 0
        per_group: dict[tuple, int] = {}
        while scanned < max_records:
            pos = cur.positions()
            if pos and all(pos.get(p, 0) >= e for p, e in end.items()):
                break
            recs = cur.poll(5000, 0.3)
            if not recs:
                break
            out = []
            for r in recs:
                if r.partition in end and r.offset >= end[r.partition]:
                    continue  # arrived after the job started: the next job takes it
                scanned += 1
                h = r.headers
                if h.get("oem") != oem:
                    continue
                if int(h.get("attempts", 1)) >= MAX_ATTEMPTS:
                    skipped += 1
                    continue
                shape, _ = shape_of(r.value, h.get("ct", ""))
                k = (oem, h.get("reason", "?"), h.get("field", ""), shape)
                per_group[k] = per_group.get(k, 0) + 1
                hdr = {"oem": oem, "rx": h.get("rx", r.ts), "replay": 1, "attempts": int(h.get("attempts", 1))}
                if h.get("ct"):
                    hdr["ct"] = h["ct"]
                out.append(Record(key=str(h.get("dev", "")).encode(), value=r.value, headers=hdr))
            if out:
                self.broker.produce(T_RAW, out)
                sent += len(out)
        flush_broker(self.broker)
        cur.commit()
        cur.close()
        with session_scope() as s:
            for (o, reason, fld, shape), n in per_group.items():
                s.execute(update(DlqGroup).where(
                    DlqGroup.oem_key == o, DlqGroup.reason == reason, DlqGroup.field == fld,
                    DlqGroup.shape == shape).values(replayed=DlqGroup.replayed + n))
            job = s.get(ReplayJob, job_id)
            if job is not None:
                job.status = "done"
                job.scanned, job.republished, job.skipped = scanned, sent, skipped
                job.finished_at = datetime.now(UTC)
        self.replayed += sent
        return {"scanned": scanned, "republished": sent, "skipped": skipped}

    def run_queued_jobs(self) -> int:
        with session_scope() as s:
            ids = [r[0] for r in s.execute(select(ReplayJob.id).where(ReplayJob.status == "queued")
                                           .order_by(ReplayJob.id))]
        for job_id in ids:
            try:
                self.run_replay(job_id)
            except Exception:
                with session_scope() as s:
                    job = s.get(ReplayJob, job_id)
                    if job is not None:
                        job.status = "failed"
                        job.finished_at = datetime.now(UTC)
        return len(ids)

    # -------------------------------------------------------------------- loop
    def step(self, timeout_s: float = 0.3) -> int:
        recs = self.consumer.poll(5000, timeout_s)
        n = 0
        if recs:
            n = self.index_batch(recs)
            self.consumer.commit()
        now = time.monotonic()
        if now - self._last_jobs >= 0.5:
            self._last_jobs = now
            self.run_queued_jobs()
        if now - self._last_pub >= 1.0:
            metrics.publish(self.broker, "dlq", "d0", {"pid": os.getpid(), "indexed": self.indexed,
                                                        "replayed": self.replayed, "lag": self.consumer.lag()})
            self.indexed = self.replayed = 0
            self._last_pub = now
        return n

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        try:
            while self.running:
                self.step()
        except KeyboardInterrupt:
            pass


def queue_replay(s: Any, oem_key: str, user_id: int | None = None, trigger: str = "manual") -> ReplayJob:
    job = ReplayJob(oem_key=oem_key, requested_by=user_id, trigger=trigger)
    s.add(job)
    s.flush()
    return job


def main() -> None:
    DlqWorker().run()


if __name__ == "__main__":
    main()
