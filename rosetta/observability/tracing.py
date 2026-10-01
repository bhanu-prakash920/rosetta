"""Distributed tracing with OpenTelemetry.

One trace follows a batch from the gateway to the dashboard. The trace context
travels in record headers as a W3C `traceparent`, so spans from different
processes join into one trace.

Tracing every event at 100K per second would cost more than the pipeline
itself, so batches are sampled (ROSETTA_TRACE_RATIO, default 1 in 200).
Without OTEL_EXPORTER_OTLP_ENDPOINT nothing is exported and the helpers are
no-ops: the pipeline never depends on the tracing backend being up.
"""
from __future__ import annotations

import os
import random
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_tracer: Any = None
_ready = False
RATIO = float(os.environ.get("ROSETTA_TRACE_RATIO", "0.005"))


def setup(service: str, exporter: Any = None) -> bool:
    """Configure once per process. Returns True when spans will be recorded."""
    global _tracer, _ready
    if _ready:
        return _tracer is not None
    _ready = True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint and exporter is None:
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": f"rosetta-{service}"}))
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _tracer = provider.get_tracer("rosetta")
        return True
    except Exception:
        _tracer = None
        return False


def reset() -> None:
    global _tracer, _ready
    _tracer, _ready = None, False


def sampled() -> bool:
    return _tracer is not None and random.random() < RATIO


@contextmanager
def span(name: str, parent: str | None = None, force: bool = False, **attrs: Any) -> Iterator[str | None]:
    """Record a span. Yields the traceparent to put into outgoing record headers."""
    if _tracer is None or not (force or parent or sampled()):
        yield parent
        return
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    prop = TraceContextTextMapPropagator()
    ctx = prop.extract({"traceparent": parent}) if parent else None
    with _tracer.start_as_current_span(name, context=ctx) as sp:
        for k, v in attrs.items():
            sp.set_attribute(k, v)
        carrier: dict[str, str] = {}
        prop.inject(carrier)
        yield carrier.get("traceparent")
