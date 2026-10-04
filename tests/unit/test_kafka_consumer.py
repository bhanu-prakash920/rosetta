"""rosetta.adapters.kafka_broker.KafkaConsumer: commits during a consumer-group rebalance.

Found in the Compose stack: two normalisers joining the group at once made each
other's commits fail with REBALANCE_IN_PROGRESS or ILLEGAL_GENERATION, the exception
killed the process, the restart caused another rebalance, and the pipeline stalled.
"""
from __future__ import annotations

import pytest
from confluent_kafka import KafkaError, KafkaException, TopicPartition

from rosetta.adapters import kafka_broker as KB


class FakeConsumer:
    def __init__(self, _conf) -> None:
        self.fail_with: int | None = None
        self.committed: list[list[tuple[int, int]]] = []
        self.callbacks: dict = {}

    def subscribe(self, _topics, on_assign=None, on_revoke=None, on_lost=None) -> None:
        self.callbacks = {"assign": on_assign, "revoke": on_revoke, "lost": on_lost}

    def commit(self, offsets, asynchronous=False) -> None:
        if self.fail_with is not None:
            raise KafkaException(KafkaError(self.fail_with))
        self.committed.append([(tp.partition, tp.offset) for tp in offsets])

    def consume(self, num_messages, timeout):
        return []


@pytest.fixture()
def consumer(monkeypatch) -> KB.KafkaConsumer:
    monkeypatch.setattr(KB, "Consumer", FakeConsumer)
    monkeypatch.setattr(KB, "kafka_client_config", lambda: {})
    c = KB.KafkaConsumer("kafka:9092", "telemetry.raw", "normalizer", None, "committed")
    c._pos = {0: 100, 1: 200}
    return c


@pytest.mark.parametrize("code", [KafkaError.REBALANCE_IN_PROGRESS, KafkaError.ILLEGAL_GENERATION,
                                  KafkaError.UNKNOWN_MEMBER_ID, KafkaError.NOT_COORDINATOR])
def test_a_commit_refused_for_a_rebalance_is_deferred_not_fatal(consumer, code):
    consumer._c.fail_with = code
    assert consumer.commit() is False
    assert consumer.commit_skipped == 1
    assert consumer._pos == {0: 100, 1: 200}, "kept for the next commit"
    consumer._c.fail_with = None
    assert consumer.commit() is True
    assert consumer._c.committed == [[(0, 100), (1, 200)]]


def test_any_other_commit_error_still_raises(consumer):
    consumer._c.fail_with = KafkaError.TOPIC_AUTHORIZATION_FAILED
    with pytest.raises(KafkaException):
        consumer.commit()


def test_revoked_partitions_are_forgotten_and_not_committed(consumer):
    consumer._c.callbacks["revoke"](consumer._c, [TopicPartition("telemetry.raw", 1)])
    assert consumer._c.committed == [], "the batch in hand may not be durable yet"
    consumer.commit()
    assert consumer._c.committed == [[(0, 100)]], "never commit a partition another member owns"


def test_lost_and_newly_assigned_partitions_start_from_the_group_offset(consumer):
    consumer._c.callbacks["lost"](consumer._c, [TopicPartition("telemetry.raw", 0)])
    consumer._c.callbacks["assign"](consumer._c, [TopicPartition("telemetry.raw", 1)])
    assert consumer._pos == {}
    assert consumer.commit() is True and consumer._c.committed == []
