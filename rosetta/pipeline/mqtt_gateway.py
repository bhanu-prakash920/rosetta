"""MQTT gateway: the bridge from vehicles (or OEM clouds) to the raw topic.

Topic layout   oem/<source>/<device>/telemetry
Subscription   $share/gateway/oem/+/+/telemetry  (a shared subscription, so several
               gateway replicas split the load and one can die without loss)
Security       TLS with client certificates when ROSETTA_MQTT_CA / CERT / KEY are
               set (mTLS). The broker maps the certificate's common name to the
               topics a device may publish to, so a device cannot speak for another.
Delivery       QoS 1 with manual acknowledgement: a message is acknowledged to the
               broker only after the batch holding it is in the raw topic. A gateway
               that dies before that leaves it unacknowledged, and the broker delivers
               it again (at least once; the normaliser drops the duplicate).
Sessions       MQTT 5, persistent. The client id is stable (ROSETTA_MQTT_CLIENT_ID,
               else the host name), so a restarted or crashed gateway takes its own
               session back, with the messages the broker queued for it meanwhile.
               The session outlives the connection by ROSETTA_MQTT_SESSION_EXPIRY_S
               (default 300 s), not 2 hours: an id that never comes back stops
               receiving a share of the traffic after that.
               ROSETTA_MQTT_END_SESSION_ON_EXIT=1 ends the session on a clean
               shutdown instead. Use it only where ids change on every start (a
               Kubernetes Deployment) AND another replica is running: with a single
               gateway, a group with no member makes the broker drop what arrives
               until the gateway is back (measured: messages.dropped.no_subscriber).

Back-pressure: when the normalisers fall behind, the callback blocks. The
client stops reading from its socket, TCP flow control pushes back on the
broker, and the broker holds QoS 1 messages until we read again. Nothing is
dropped and nothing is buffered without limit in this process.
"""
from __future__ import annotations

import os
import signal
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ..config import get_settings
from ..factory import flush_broker, make_broker
from . import metrics
from .gateway import Backpressure, Gateway

FLUSH_EVERY_S = 0.05
FLUSH_AT = 2000
SHUTDOWN_GRACE_S = 20.0
SUBSCRIPTION = "$share/gateway/oem/+/+/telemetry"
SESSION_EXPIRY_S = int(os.environ.get("ROSETTA_MQTT_SESSION_EXPIRY_S", "300"))
END_SESSION_ON_EXIT = os.environ.get("ROSETTA_MQTT_END_SESSION_ON_EXIT", "") in ("1", "true", "yes")


def client_id() -> str:
    """Stable across restarts of the same replica, distinct between replicas."""
    return os.environ.get("ROSETTA_MQTT_CLIENT_ID") or f"gateway-{socket.gethostname()}"


def parse_topic(topic: str) -> tuple[str, str] | None:
    """'oem/helix/hx-0000012/telemetry' -> ('helix', 'hx-0000012')."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "oem" or parts[3] != "telemetry":
        return None
    oem, dev = parts[1], parts[2]
    if not oem or not dev or len(oem) > 32 or len(dev) > 64:
        return None
    return oem, dev


class MqttGateway:
    def __init__(self, broker: Any = None, client: Any = None) -> None:
        st = get_settings()
        self.broker = broker or make_broker(st)
        self.gateway = Gateway(self.broker)
        self.buf: dict[str, list[tuple[str, bytes]]] = {}
        self.mids: list[int] = []          # message ids to acknowledge once forwarded
        self.n = 0
        self.lock = threading.Lock()
        self.running = True
        self.received = self.forwarded = self.bad_topic = self.waits = 0
        self._last_flush = time.monotonic()
        self._last_pub = time.monotonic()
        self.client = client or self._connect(st)

    def _connect(self, st: Any) -> Any:
        import paho.mqtt.client as mqtt

        u = urlparse(st.mqtt_url)
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id(), protocol=mqtt.MQTTv5,
                        manual_ack=True)
        if u.scheme in ("mqtts", "ssl", "tls") or st.mqtt_ca:
            ctx = ssl.create_default_context(cafile=st.mqtt_ca or None)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            if st.mqtt_cert:
                ctx.load_cert_chain(st.mqtt_cert, st.mqtt_key or None)
            c.tls_set_context(ctx)
        if u.username:
            c.username_pw_set(u.username, u.password)
        c.on_connect = lambda cl, _u, _f, rc, _p=None: cl.subscribe(SUBSCRIPTION, qos=1)
        c.on_message = lambda _c, _u, m: self.on_message(m.topic, m.payload, m.mid)
        c.max_inflight_messages_set(2000)
        props = Properties(PacketTypes.CONNECT)
        props.SessionExpiryInterval = SESSION_EXPIRY_S
        c.connect(u.hostname or "localhost", u.port or (8883 if st.mqtt_ca else 1883), keepalive=30,
                  clean_start=False, properties=props)
        return c

    def on_message(self, topic: str, payload: bytes, mid: int | None = None) -> None:
        self.received += 1
        parsed = parse_topic(topic)
        if parsed is None:
            self.bad_topic += 1
            self._ack([mid])                 # never deliverable: do not have it sent again
            return
        oem, dev = parsed
        with self.lock:
            self.buf.setdefault(oem, []).append((dev, bytes(payload)))   # content type is unknown over MQTT
            if mid is not None:
                self.mids.append(mid)
            self.n += 1
            due = self.n >= FLUSH_AT or time.monotonic() - self._last_flush >= FLUSH_EVERY_S
        if due:
            self.flush()

    def flush(self) -> int:
        with self.lock:
            buf, self.buf, self.n = self.buf, {}, 0
            mids, self.mids = self.mids, []
            self._last_flush = time.monotonic()
        sent = 0
        complete = True
        # On shutdown keep trying for a while: these messages were already acknowledged
        # to the MQTT broker, so dropping them here would lose them.
        give_up = time.monotonic() + SHUTDOWN_GRACE_S
        for oem, items in buf.items():
            while True:
                try:
                    sent += self.gateway.submit(oem, items)
                    break
                except Backpressure as bp:
                    self.waits += 1
                    if not self.running and time.monotonic() > give_up:
                        complete = False          # left unacknowledged: the broker keeps them
                        break
                    time.sleep(bp.retry_after_s)      # blocks the network loop on purpose
                except ValueError:
                    break                         # a source the gateway refuses: acknowledged, dropped
        self.forwarded += sent
        if complete and mids:
            flush_broker(self.broker)             # in the raw topic before the broker forgets them
            self._ack(mids)
        return sent

    def _ack(self, mids: list[int | None]) -> None:
        ack = getattr(self.client, "ack", None)
        if ack is None:
            return
        for mid in mids:
            if mid is not None:
                ack(mid, 1)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        self.client.loop_start()
        try:
            while self.running:
                time.sleep(FLUSH_EVERY_S)
                if self.n:
                    self.flush()
                now = time.monotonic()
                if now - self._last_pub >= 1.0:
                    flush_broker(self.broker)
                    metrics.publish(self.broker, "gateway", f"g{os.getpid()}", {
                        "pid": os.getpid(), "received": self.received, "forwarded": self.forwarded,
                        "bad_topic": self.bad_topic, "backpressure_waits": self.waits})
                    self.received = self.forwarded = self.bad_topic = self.waits = 0
                    self._last_pub = now
        except KeyboardInterrupt:
            pass
        finally:
            if END_SESSION_ON_EXIT:
                # Leave the shared group first, so nothing new is dispatched to us.
                unsubscribe = getattr(self.client, "unsubscribe", None)
                if unsubscribe is not None:
                    try:
                        unsubscribe(SUBSCRIPTION)
                        time.sleep(0.3)           # let deliveries already on the wire arrive
                    except Exception:             # already disconnected: the session keeps them
                        pass
            else:
                # Stop reading. What the broker sends from now on waits in our session,
                # and anything received but not yet acknowledged is delivered again.
                self.client.loop_stop()
            self.flush()                          # forwards and acknowledges what is buffered
            flush_broker(self.broker)
            if END_SESSION_ON_EXIT:
                self.client.loop_stop()
                self._disconnect(end_session=True)
            else:
                self._disconnect(end_session=False)

    def _disconnect(self, end_session: bool) -> None:
        if not end_session:
            self.client.disconnect()              # the session stays for SESSION_EXPIRY_S
            return
        try:
            from paho.mqtt.packettypes import PacketTypes
            from paho.mqtt.properties import Properties

            props = Properties(PacketTypes.DISCONNECT)
            props.SessionExpiryInterval = 0
            self.client.disconnect(properties=props)
        except TypeError:                         # a client without MQTT 5 properties
            self.client.disconnect()


def main() -> None:
    MqttGateway().run()


if __name__ == "__main__":
    main()
