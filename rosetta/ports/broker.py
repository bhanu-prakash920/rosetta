"""Broker port: what the pipeline needs from a message log.

Two adapters implement it. `LogBroker` is a file-backed partitioned log used
for the no-infrastructure local mode and for tests. `KafkaBroker` is the
production adapter. Services depend only on this module, so swapping the
broker is a configuration change, not a code change.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

T_RAW = "telemetry.raw"
T_CANONICAL = "telemetry.canonical"
T_DLQ = "telemetry.dlq"
T_ALERTS = "fleet.alerts"
T_METRICS = "ops.metrics"

TOPICS = (T_RAW, T_CANONICAL, T_DLQ, T_ALERTS, T_METRICS)


@dataclass(slots=True)
class Record:
    key: bytes
    value: bytes
    headers: dict = field(default_factory=dict)
    ts: int = 0                      # broker append time, epoch ms
    partition: int = -1
    offset: int = -1                 # position of this record
    next_offset: int = -1            # position to resume from after this record


class Consumer(Protocol):
    def poll(self, max_records: int = 1000, timeout_s: float = 0.5) -> list[Record]: ...
    def commit(self) -> None: ...
    def lag(self) -> int: ...
    def seek_to_beginning(self) -> None: ...
    def seek_to_end(self) -> None: ...
    def positions(self) -> dict[int, int]: ...
    def close(self) -> None: ...


class Broker(Protocol):
    def partitions(self, topic: str) -> int: ...
    def produce(self, topic: str, records: Sequence[Record]) -> None: ...
    def consumer(self, topic: str, group: str, partitions: Sequence[int] | None = None,
                 start: str = "committed") -> Consumer: ...
    def end_offsets(self, topic: str) -> dict[int, int]: ...
    def close(self) -> None: ...
