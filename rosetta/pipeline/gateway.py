"""Ingest gateway: the single front door for raw OEM payloads.

Responsibilities, in order:
  1. refuse what is obviously wrong (oversize, no source id) before it costs anything
  2. stamp the receive time, which is where every latency measurement starts
  3. apply back-pressure: when the normalisers fall behind, tell producers to slow
     down instead of buffering without limit and falling over
  4. append to the raw topic, keyed by device id, so each vehicle keeps its order

The gateway does not parse payloads. It cannot, because it does not know the
dialect. That is the normaliser's job.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from ..engine.decoders import MAX_PAYLOAD_BYTES
from ..observability import tracing
from ..ports.broker import T_RAW, Record


class Backpressure(Exception):
    """Raised when intake is paused. Carries how long the producer should wait."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"intake paused, retry after {retry_after_s:.1f}s")
        self.retry_after_s = retry_after_s


class Gateway:
    def __init__(self, broker: Any, lag_fn: Callable[[], int] | None = None,
                 high_watermark: int = 600_000, low_watermark: int = 200_000,
                 check_every_s: float = 0.5) -> None:
        self.broker = broker
        self.lag_fn = lag_fn
        self.high = high_watermark
        self.low = low_watermark
        self.check_every_s = check_every_s
        self._paused = False
        self._checked = 0.0
        self.accepted = 0
        self.rejected = 0
        self.throttled = 0
        # Once per process. The gateway runs inside the MQTT bridge, the simulator or
        # the API, so the span carries the name of the process it runs in.
        tracing.setup(os.environ.get("ROSETTA_SERVICE", "gateway"))

    def accepting(self) -> bool:
        """Hysteresis: pause above the high mark, resume only below the low mark."""
        if self.lag_fn is None:
            return True
        now = time.monotonic()
        if now - self._checked >= self.check_every_s:
            self._checked = now
            lag = self.lag_fn()
            if self._paused and lag < self.low:
                self._paused = False
            elif not self._paused and lag > self.high:
                self._paused = True
        return not self._paused

    def submit(self, oem: str, items: Sequence[tuple[str, bytes]], content_type: str = "",
               rx_ms: int | None = None, extra: dict[str, Any] | None = None) -> int:
        """items: (device_id, payload). Returns how many were accepted."""
        if not oem or len(oem) > 32:
            self.rejected += len(items)
            raise ValueError("missing or invalid source id")
        if not self.accepting():
            self.throttled += len(items)
            raise Backpressure(self.check_every_s)
        rx = rx_ms if rx_ms is not None else int(time.time() * 1000)
        headers = {"oem": oem, "rx": rx}
        if content_type:
            headers["ct"] = content_type
        if extra:
            headers.update(extra)
        with tracing.span("gateway.submit", oem=oem, records=len(items)) as tp:
            if tp:
                headers["tp"] = tp
        recs = []
        limit = MAX_PAYLOAD_BYTES * 4  # far above any real message: refuse at the door
        for dev, payload in items:
            if len(payload) > limit or not dev:
                self.rejected += 1
                continue
            recs.append(Record(key=dev.encode(), value=payload, headers=headers))
        self.broker.produce(T_RAW, recs)
        self.accepted += len(recs)
        return len(recs)
