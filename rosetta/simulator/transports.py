"""How the simulator delivers payloads: straight to the gateway, or over MQTT."""
from __future__ import annotations

import os
import socket
import ssl
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from ..config import get_settings
from ..pipeline.gateway import Backpressure


class MqttTransport:
    """Publishes like a fleet of devices would: one topic per device, QoS 1."""

    def __init__(self, client: Any = None) -> None:
        st = get_settings()
        if client is None:
            import paho.mqtt.client as mqtt

            u = urlparse(st.mqtt_url)
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{socket.gethostname()}-{os.getpid()}")
            if u.scheme in ("mqtts", "ssl", "tls") or st.mqtt_ca:
                ctx = ssl.create_default_context(cafile=st.mqtt_ca or None)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_3
                if st.mqtt_cert:
                    ctx.load_cert_chain(st.mqtt_cert, st.mqtt_key or None)
                client.tls_set_context(ctx)
            client.max_queued_messages_set(200_000)
            client.connect(u.hostname or "localhost", u.port or 1883, keepalive=30)
            client.loop_start()
        self.client = client
        self.accepted = self.rejected = self.throttled = 0

    def submit(self, oem: str, items: Sequence[tuple[str, bytes]], content_type: str = "", **_: Any) -> int:
        n = 0
        for dev, payload in items:
            info = self.client.publish(f"oem/{oem}/{dev}/telemetry", payload, qos=1)
            if getattr(info, "rc", 0) != 0:
                # The local queue is full: behave like the direct transport and ask the
                # caller to back off.
                self.throttled += len(items) - n
                raise Backpressure(0.2)
            n += 1
        self.accepted += n
        return n

    def close(self) -> None:
        time.sleep(0.2)
        self.client.loop_stop()
        self.client.disconnect()
