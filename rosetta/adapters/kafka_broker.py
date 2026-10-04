"""KafkaBroker: the production adapter for the Broker port.

Producer: idempotent, acks=all, so a retried send cannot create a duplicate or
be lost while at least one in-sync replica is alive.
Consumer: manual commits after the batch was processed and produced, which
gives at-least-once delivery. Consumers are idempotent, so the end result is
effectively-once without the cost of Kafka transactions (see ADR-0003).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import orjson
from confluent_kafka import OFFSET_BEGINNING, OFFSET_END, Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from ..config import kafka_client_config
from ..ports.broker import TOPICS, Record

log = logging.getLogger("rosetta.kafka")

# A commit refused for one of these reasons is not a fault of this process: the group
# is rebalancing, or the coordinator moved. The offsets are committed with the next
# batch; if the partition went to another member meanwhile, that member re-reads a
# few records, which the idempotent consumers absorb (at-least-once, ADR-0003).
REBALANCE_ERRORS = frozenset({
    KafkaError.REBALANCE_IN_PROGRESS, KafkaError.ILLEGAL_GENERATION, KafkaError.UNKNOWN_MEMBER_ID,
    KafkaError.NOT_COORDINATOR, KafkaError.COORDINATOR_LOAD_IN_PROGRESS, KafkaError.COORDINATOR_NOT_AVAILABLE,
    KafkaError._ASSIGNMENT_LOST, KafkaError._STATE, KafkaError.FENCED_INSTANCE_ID,
})

DEFAULT_PARTITIONS = {"telemetry.raw": 32, "telemetry.canonical": 32, "telemetry.dlq": 6,
                      "fleet.alerts": 6, "ops.metrics": 1, "ops.control": 1}


class KafkaConsumer:
    def __init__(self, bootstrap: str, topic: str, group: str, partitions: Sequence[int] | None, start: str) -> None:
        self.topic = topic
        self._c = Consumer({
            **kafka_client_config(),
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "enable.auto.commit": False,
            "auto.offset.reset": "latest" if start in ("latest", "end") else "earliest",
            "partition.assignment.strategy": "cooperative-sticky",
            "fetch.min.bytes": 1,
            "fetch.wait.max.ms": 50,
            "max.poll.interval.ms": 300_000,
        })
        self._pos: dict[int, int] = {}
        if partitions is not None:
            off = OFFSET_BEGINNING if start == "beginning" else OFFSET_END if start == "end" else -1001
            self._c.assign([TopicPartition(topic, p, off) if start in ("beginning", "end")
                            else TopicPartition(topic, p) for p in partitions])
            if start == "committed":
                committed = self._c.committed([TopicPartition(topic, p) for p in partitions], timeout=10)
                self._c.assign([tp if tp.offset >= 0 else TopicPartition(topic, tp.partition, OFFSET_BEGINNING)
                                for tp in committed])
        else:
            self._c.subscribe([topic], on_assign=self._on_assign, on_revoke=self._on_revoke,
                              on_lost=self._on_lost)
        self.commit_skipped = 0

    # With cooperative-sticky assignment the callbacks receive only the partitions
    # that change hands, so the others keep being consumed through a rebalance.
    def _on_assign(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        for tp in partitions:
            self._pos.pop(tp.partition, None)          # start from the group's committed offset

    def _on_revoke(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        # No commit here. The caller commits only once its output is durable (the
        # processor, for example, after the Parquet file is written), and the batch
        # in hand may not be yet. The new owner re-reads from the last real commit.
        for tp in partitions:
            self._pos.pop(tp.partition, None)

    def _on_lost(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        for tp in partitions:                         # already owned by another member: nothing to commit
            self._pos.pop(tp.partition, None)

    def poll(self, max_records: int = 1000, timeout_s: float = 0.5) -> list[Record]:
        out = []
        for m in self._c.consume(num_messages=max_records, timeout=timeout_s):
            if m.error():
                continue
            headers = {k: orjson.loads(v) for k, v in (m.headers() or []) if v}
            out.append(Record(key=m.key() or b"", value=m.value() or b"", headers=headers,
                              ts=m.timestamp()[1], partition=m.partition(), offset=m.offset(),
                              next_offset=m.offset() + 1))
            self._pos[m.partition()] = m.offset() + 1
        return out

    def commit(self) -> bool:
        """Commit what was processed. False when the group refused it for a rebalance."""
        if not self._pos:
            return True
        offsets = [TopicPartition(self.topic, p, o) for p, o in self._pos.items()]
        try:
            self._c.commit(offsets=offsets, asynchronous=False)
            return True
        except KafkaException as e:
            err = e.args[0] if e.args else None
            if isinstance(err, KafkaError) and (err.code() in REBALANCE_ERRORS or err.retriable()):
                self.commit_skipped += 1
                log.warning("commit deferred, group is rebalancing: %s", err.str())
                return False
            raise

    def lag(self) -> int:
        total = 0
        for tp in self._c.assignment():
            try:
                _lo, hi = self._c.get_watermark_offsets(tp, timeout=1.0, cached=True)
            except KafkaException:
                continue
            pos = self._pos.get(tp.partition)
            if pos is not None and hi >= 0:
                total += max(0, hi - pos)
        return total

    def positions(self) -> dict[int, int]:
        return dict(self._pos)

    def seek_to_beginning(self) -> None:
        self._c.assign([TopicPartition(self.topic, tp.partition, OFFSET_BEGINNING) for tp in self._c.assignment()])

    def seek_to_end(self) -> None:
        self._c.assign([TopicPartition(self.topic, tp.partition, OFFSET_END) for tp in self._c.assignment()])

    def close(self) -> None:
        self._c.close()


class KafkaBroker:
    def __init__(self, bootstrap: str, create_topics: bool = True, replication: int = 1) -> None:
        self.bootstrap = bootstrap
        self._cfg = kafka_client_config()
        self._p = Producer({
            **self._cfg,
            "bootstrap.servers": bootstrap,
            "enable.idempotence": True,
            "acks": "all",
            "compression.type": "lz4",
            "linger.ms": 5,
            "batch.num.messages": 10_000,
            "queue.buffering.max.messages": 1_000_000,
            "queue.buffering.max.kbytes": 1_048_576,
        })
        self._admin = AdminClient({**self._cfg, "bootstrap.servers": bootstrap})
        self._n: dict[str, int] = {}
        if create_topics:
            self.ensure_topics(replication)

    def ensure_topics(self, replication: int = 1) -> None:
        existing = set(self._admin.list_topics(timeout=10).topics)
        wanted = [NewTopic(t, num_partitions=DEFAULT_PARTITIONS.get(t, 6), replication_factor=replication)
                  for t in (*TOPICS, "ops.control") if t not in existing]
        if wanted:
            for fut in self._admin.create_topics(wanted).values():
                try:
                    fut.result(timeout=15)
                except KafkaException:
                    pass  # another service created it first

    def partitions(self, topic: str) -> int:
        if topic not in self._n:
            md = self._admin.list_topics(topic, timeout=10)
            self._n[topic] = len(md.topics[topic].partitions)
        return self._n[topic]

    def produce(self, topic: str, records: Sequence[Record]) -> None:
        p = self._p
        for r in records:
            headers = [(k, orjson.dumps(v)) for k, v in r.headers.items()] if r.headers else None
            while True:
                try:
                    if r.partition >= 0:
                        p.produce(topic, r.value, r.key, partition=r.partition, headers=headers)
                    else:
                        p.produce(topic, r.value, r.key, headers=headers)
                    break
                except BufferError:
                    p.poll(0.05)  # back-pressure: local queue is full, let it drain
        p.poll(0)

    def flush(self, timeout_s: float = 30.0) -> int:
        return self._p.flush(timeout_s)

    def consumer(self, topic: str, group: str, partitions: Sequence[int] | None = None,
                 start: str = "committed") -> KafkaConsumer:
        return KafkaConsumer(self.bootstrap, topic, group, partitions, start)

    def end_offsets(self, topic: str) -> dict[int, int]:
        c = Consumer({**self._cfg, "bootstrap.servers": self.bootstrap, "group.id": f"probe-{time.time_ns()}"})
        try:
            return {p: c.get_watermark_offsets(TopicPartition(topic, p), timeout=5)[1]
                    for p in range(self.partitions(topic))}
        finally:
            c.close()

    def close(self) -> None:
        self._p.flush(10)
