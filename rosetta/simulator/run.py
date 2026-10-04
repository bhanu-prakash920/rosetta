"""Simulator runner: drives the fleet and sends its events like a real network would.

A clean, ordered, one-event-per-second stream would prove nothing. This runner
adds the faults a production intake has to survive:

  duplicates      the same event sent twice (retry after a lost acknowledgement)
  reordering      events held back 0.5 to 4 s, so newer ones overtake older ones
  outage          a share of devices goes offline, buffers, then floods on recovery
  malformed       truncated payloads and corrupted VINs
  burst           a shift start: many parked vehicles start within seconds
  format drift    a share of one OEM's vehicles switches to a new firmware format
  unknown source  an OEM the platform has no mapping for starts sending

Settings change at run time through the ops.control topic.
"""
from __future__ import annotations

import heapq
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import orjson

from ..config import get_settings
from ..engine.router import canary_bucket
from ..factory import T_CONTROL, flush_broker, make_broker
from ..pipeline import metrics
from ..pipeline.gateway import Backpressure, Gateway
from .dialects import DIALECTS
from .fleet import OEM_INDEX, OEM_KEYS, Fleet


@dataclass
class SimSettings:
    mode: str = "realistic"            # realistic | all
    hz: float = 1.0                    # fleet ticks per second
    max_eps: int = 0                   # cap on events per second, 0 = no cap
    enabled: list[str] = field(default_factory=lambda: [k for k in OEM_KEYS if k != "helix"])
    drift_pct: int = 0                 # share of pacifica vehicles on the new firmware
    dup_pct: float = 1.5
    reorder_pct: float = 2.0
    malformed_pct: float = 0.05
    outage_pct: int = 0
    burst: float = 1.0
    paused: bool = False

    def apply(self, patch: dict[str, Any]) -> None:
        for k, v in patch.items():
            if k == "enabled" and isinstance(v, list):
                self.enabled = [x for x in v if x in OEM_KEYS]
            elif k in ("drift_pct", "outage_pct", "max_eps"):
                setattr(self, k, max(0, int(v)))
            elif k in ("dup_pct", "reorder_pct", "malformed_pct"):
                setattr(self, k, min(50.0, max(0.0, float(v))))
            elif k == "hz":
                self.hz = min(10.0, max(0.1, float(v)))
            elif k == "burst":
                self.burst = min(50.0, max(0.0, float(v)))
            elif k == "mode" and v in ("realistic", "all"):
                self.mode = v
            elif k == "paused":
                self.paused = bool(v)


def corrupt(payload: bytes, vin: str, rng: np.random.Generator) -> bytes:
    """Damage a payload the way a bad link or a buggy firmware would."""
    if rng.random() < 0.5 and len(payload) > 8:
        return payload[: int(rng.integers(4, len(payload) - 1))]           # truncated
    v = vin.encode()
    i = payload.find(v)
    if i >= 0:
        bad = bytearray(payload)
        bad[i + 8] = ord("0") if bad[i + 8] != ord("0") else ord("1")     # break the check digit
        return bytes(bad)
    return payload[: len(payload) // 2]


class SimulatorRunner:
    def __init__(self, fleet: Fleet | None = None, gateway: Gateway | None = None,
                 settings: SimSettings | None = None, shard: int = 0, shards: int = 1,
                 vehicles: int = 100_000, seed: int = 7, broker: Any = None,
                 listen_control: bool = True) -> None:
        self.broker = broker or make_broker(get_settings())
        if gateway is None and os.environ.get("SIM_TRANSPORT", "direct") == "mqtt":
            from .transports import MqttTransport

            gateway = MqttTransport()
        self.gateway = gateway or Gateway(self.broker)
        self.fleet = fleet or Fleet(vehicles, seed=seed)
        self.s = settings or SimSettings()
        self.shard, self.shards = shard, shards
        self.rng = np.random.default_rng(seed * 31 + shard)
        self.delayed: list[tuple[float, int, str, str, bytes, int]] = []
        self.held: list[tuple[str, str, bytes, int]] = []
        self._n = 0
        self.bucket = np.array([canary_bucket(d.encode()) for d in self.fleet.device_id], dtype=np.int16)
        self.control = self.broker.consumer(T_CONTROL, f"sim-{shard}-{os.getpid()}", start="beginning") \
            if listen_control else None
        self.running = True
        self.sent = self.dups = self.malformed = self.reordered = self.throttled = 0
        self.by_oem: dict[str, int] = {}
        self._last_pub = time.monotonic()

    # ---------------------------------------------------------------- control
    def poll_control(self) -> None:
        if self.control is None:
            return
        for r in self.control.poll(100, 0.0):
            try:
                self.s.apply(orjson.loads(r.value))
            except Exception:
                continue

    # ------------------------------------------------------------------- tick
    def tick(self, now_ms: int | None = None, dt: float | None = None) -> int:
        s, f, rng = self.s, self.fleet, self.rng
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        dt = dt if dt is not None else 1.0 / s.hz
        f.step(dt, now_ms, start_boost=s.burst)
        idx = f.emitting(s.mode)
        if self.shards > 1:
            idx = idx[idx % self.shards == self.shard]
        cap = int(s.max_eps / s.hz / self.shards) if s.max_eps else 0
        if cap and idx.size > cap:
            idx = np.sort(rng.choice(idx, size=cap, replace=False))
        total = 0
        now = time.monotonic()
        offline = (self.bucket[idx] < s.outage_pct) if s.outage_pct else None

        for key in s.enabled:
            o = OEM_INDEX[key]
            sel_mask = f.oem[idx] == o
            if not sel_mask.any():
                continue
            groups = [(key, idx[sel_mask], offline[sel_mask] if offline is not None else None)]
            if key == "pacifica" and s.drift_pct:
                sel = idx[sel_mask]
                off = offline[sel_mask] if offline is not None else None
                upd = self.bucket[sel] < s.drift_pct
                groups = [("pacifica", sel[~upd], off[~upd] if off is not None else None),
                          ("pacifica_v2", sel[upd], off[upd] if off is not None else None)]
            for dialect_key, sel, off in groups:
                if sel.size == 0:
                    continue
                d = DIALECTS[dialect_key]
                t = f.truth(sel)
                payloads = d.encode(t)
                devs = t["device_id"]
                vins = t["vin"]
                m = len(payloads)
                bad = rng.random(m) < s.malformed_pct / 100.0 if s.malformed_pct else None
                late = rng.random(m) < s.reorder_pct / 100.0 if s.reorder_pct else None
                dup = rng.random(m) < s.dup_pct / 100.0 if s.dup_pct else None
                items = []
                for j in range(m):
                    p = payloads[j]
                    if bad is not None and bad[j]:
                        p = corrupt(p, vins[j], rng)
                        self.malformed += 1
                    if off is not None and off[j]:
                        if len(self.held) < 2_000_000:
                            self.held.append((d.oem, devs[j], p, now_ms))
                        continue
                    if late is not None and late[j]:
                        self._n += 1
                        heapq.heappush(self.delayed, (now + float(rng.uniform(0.5, 4.0)), self._n, d.oem, devs[j], p, now_ms))
                        self.reordered += 1
                        continue
                    items.append((devs[j], p))
                    if dup is not None and dup[j]:
                        self._n += 1
                        heapq.heappush(self.delayed, (now + float(rng.uniform(0.0, 2.0)), self._n, d.oem, devs[j], p, now_ms))
                        self.dups += 1
                total += self._send(d.oem, items, d.content_type)

        total += self._release(now)
        if not s.outage_pct and self.held:
            total += self._flush_held()
        return total

    def _send(self, oem: str, items: list[tuple[str, bytes]], content_type: str = "") -> int:
        if not items:
            return 0
        for _ in range(200):
            try:
                n = self.gateway.submit(oem, items, content_type)
                break
            except Backpressure as bp:
                self.throttled += 1
                time.sleep(bp.retry_after_s)   # a well-behaved producer backs off
        else:
            return 0
        self.sent += n
        self.by_oem[oem] = self.by_oem.get(oem, 0) + n
        return n

    def _release(self, now: float) -> int:
        out: dict[str, list[tuple[str, bytes]]] = {}
        while self.delayed and self.delayed[0][0] <= now:
            _, _, oem, dev, p, _ts = heapq.heappop(self.delayed)
            out.setdefault(oem, []).append((dev, p))
        return sum(self._send(oem, items, DIALECTS[oem].content_type) for oem, items in out.items())

    def _flush_held(self) -> int:
        """Network recovery: everything buffered during the outage arrives at once."""
        out: dict[str, list[tuple[str, bytes]]] = {}
        for oem, dev, p, _ts in self.held:
            out.setdefault(oem, []).append((dev, p))
        self.held = []
        n = 0
        for oem, items in out.items():
            for a in range(0, len(items), 20_000):
                n += self._send(oem, items[a:a + 20_000], DIALECTS[oem].content_type)
        return n

    def publish(self) -> None:
        metrics.publish(self.broker, "simulator", f"s{self.shard}", {
            "pid": os.getpid(), "sent": self.sent, "by_oem": self.by_oem, "dups": self.dups,
            "malformed": self.malformed, "reordered": self.reordered, "throttled": self.throttled,
            "held": len(self.held), "delayed": len(self.delayed), "vehicles": self.fleet.n,
            "settings": asdict(self.s),
        })
        self.sent = self.dups = self.malformed = self.reordered = self.throttled = 0
        self.by_oem = {}

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        next_tick = time.monotonic()
        try:
            while self.running:
                self.poll_control()
                if self.s.paused:
                    time.sleep(0.2)
                    next_tick = time.monotonic()
                else:
                    self.tick()
                    next_tick += 1.0 / self.s.hz
                    flush_broker(self.broker)
                now = time.monotonic()
                if now - self._last_pub >= 1.0:
                    self.publish()
                    self._last_pub = now
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -2.0:
                    next_tick = time.monotonic()   # cannot keep up: do not try to catch up
        except KeyboardInterrupt:
            pass


def send_control(broker: Any, patch: dict[str, Any]) -> None:
    from ..ports.broker import Record

    broker.produce(T_CONTROL, [Record(key=b"sim", value=orjson.dumps(patch))])
    flush_broker(broker)


def main(shard: int = 0, shards: int = 1) -> None:
    st = SimSettings()
    st.apply({k[4:].lower(): orjson.loads(v) if v[:1] in "[{0123456789tf" else v
              for k, v in os.environ.items() if k.startswith("SIM_") and k != "SIM_TRANSPORT"})
    SimulatorRunner(shard=shard, shards=shards, settings=st,
                    vehicles=int(os.environ.get("ROSETTA_VEHICLES", "100000"))).run()


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0, int(sys.argv[2]) if len(sys.argv) > 2 else 1)
