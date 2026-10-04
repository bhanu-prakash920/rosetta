"""rosetta.observability.tracing: sampled spans whose context travels in record headers."""
from __future__ import annotations

import re

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rosetta.observability import tracing

TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


@pytest.fixture(autouse=True)
def fresh_tracing(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing.reset()
    yield
    tracing.reset()


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    assert tracing.setup("unit", exporter=exp) is True
    return exp


def trace_id(traceparent: str) -> str:
    return traceparent.split("-")[1]


def span_id(traceparent: str) -> str:
    return traceparent.split("-")[2]


# ------------------------------------------------------------------ switched off
def test_setup_without_an_endpoint_records_nothing():
    assert tracing.setup("unit") is False
    assert tracing.sampled() is False


def test_span_is_a_no_op_when_tracing_is_off():
    with tracing.span("work", force=True, records=5) as tp:
        assert tp is None


def test_parent_is_passed_through_when_tracing_is_off():
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    with tracing.span("work", parent=parent) as tp:
        assert tp == parent


def test_exceptions_pass_through_a_no_op_span():
    with pytest.raises(KeyError):
        with tracing.span("work"):
            raise KeyError("x")


# ------------------------------------------------------------------------ setup
def test_setup_happens_once_per_process(exporter):
    other = InMemorySpanExporter()
    assert tracing.setup("again", exporter=other) is True
    with tracing.span("work", force=True):
        pass
    assert len(exporter.get_finished_spans()) == 1
    assert other.get_finished_spans() == ()


def test_setup_that_was_off_stays_off_until_reset():
    assert tracing.setup("unit") is False
    assert tracing.setup("unit", exporter=InMemorySpanExporter()) is False
    tracing.reset()
    assert tracing.setup("unit", exporter=InMemorySpanExporter()) is True


def test_service_name_is_part_of_the_resource(exporter):
    with tracing.span("work", force=True):
        pass
    assert exporter.get_finished_spans()[0].resource.attributes["service.name"] == "rosetta-unit"


def test_setup_failure_switches_tracing_off(monkeypatch):
    import opentelemetry.sdk.trace as sdk

    def broken(*_a, **_kw):
        raise RuntimeError("no provider")

    monkeypatch.setattr(sdk, "TracerProvider", broken)
    assert tracing.setup("unit", exporter=InMemorySpanExporter()) is False
    with tracing.span("work", force=True) as tp:
        assert tp is None


def test_endpoint_in_the_environment_switches_tracing_on(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    from opentelemetry.sdk.trace import export

    created = []

    class Processor:
        def __init__(self, exporter) -> None:
            created.append(type(exporter).__name__)

        def on_start(self, *a, **kw) -> None: ...
        def on_end(self, *a, **kw) -> None: ...
        def shutdown(self) -> None: ...
        def force_flush(self, *a, **kw) -> bool:
            return True

    monkeypatch.setattr(export, "BatchSpanProcessor", Processor)       # no background export thread
    assert tracing.setup("unit") is True
    assert created == ["OTLPSpanExporter"]


# ------------------------------------------------------------------------ spans
def test_forced_span_is_recorded_with_its_attributes(exporter):
    with tracing.span("normalizer.batch", force=True, oem="pacifica", records=500, ok=True) as tp:
        assert TRACEPARENT.match(tp)
        assert int(tp.rsplit("-", 1)[1], 16) & 1, "the sampled flag is set"
    (span,) = exporter.get_finished_spans()
    assert span.name == "normalizer.batch"
    assert dict(span.attributes) == {"oem": "pacifica", "records": 500, "ok": True}
    assert trace_id(tp) == format(span.context.trace_id, "032x")
    assert span_id(tp) == format(span.context.span_id, "016x")
    assert span.parent is None


def test_span_is_exported_when_the_block_ends(exporter):
    with tracing.span("work", force=True):
        assert exporter.get_finished_spans() == ()
    assert len(exporter.get_finished_spans()) == 1


def test_child_span_joins_the_trace_of_its_parent(exporter):
    with tracing.span("gateway.submit", force=True) as parent:
        pass
    with tracing.span("normalizer.batch", parent=parent) as child:
        pass
    with tracing.span("processor.batch", parent=child) as grandchild:
        pass
    assert trace_id(parent) == trace_id(child) == trace_id(grandchild)
    assert len({span_id(parent), span_id(child), span_id(grandchild)}) == 3

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert format(by_name["normalizer.batch"].parent.span_id, "016x") == span_id(parent)
    assert format(by_name["processor.batch"].parent.span_id, "016x") == span_id(child)
    assert by_name["normalizer.batch"].parent.is_remote is True


def test_span_with_a_parent_is_recorded_without_sampling(exporter, monkeypatch):
    monkeypatch.setattr(tracing, "RATIO", 0.0)
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    with tracing.span("work", parent=parent) as tp:
        assert trace_id(tp) == "0af7651916cd43dd8448eb211c80319c"
        assert span_id(tp) != "b7ad6b7169203331"
    assert len(exporter.get_finished_spans()) == 1


def test_two_root_spans_have_different_traces(exporter):
    with tracing.span("a", force=True) as a:
        pass
    with tracing.span("b", force=True) as b:
        pass
    assert trace_id(a) != trace_id(b)


def test_nested_spans_share_a_trace(exporter):
    with tracing.span("outer", force=True) as outer:
        with tracing.span("inner", force=True) as inner:
            pass
    assert trace_id(outer) == trace_id(inner)
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert by_name["inner"].parent.span_id == by_name["outer"].context.span_id


def test_exception_inside_a_span_is_recorded_and_raised(exporter):
    with pytest.raises(ValueError, match="bad batch"):
        with tracing.span("work", force=True):
            raise ValueError("bad batch")
    (span,) = exporter.get_finished_spans()
    assert span.status.is_ok is False
    assert [e.name for e in span.events] == ["exception"]


# --------------------------------------------------------------------- sampling
def test_unsampled_span_is_not_recorded(exporter, monkeypatch):
    monkeypatch.setattr(tracing, "RATIO", 0.0)
    with tracing.span("work", records=5) as tp:
        assert tp is None
    assert exporter.get_finished_spans() == ()


def test_everything_is_sampled_at_ratio_one(exporter, monkeypatch):
    monkeypatch.setattr(tracing, "RATIO", 1.0)
    assert tracing.sampled() is True
    with tracing.span("work") as tp:
        assert TRACEPARENT.match(tp)
    assert len(exporter.get_finished_spans()) == 1


def test_sampling_follows_the_ratio(exporter, monkeypatch):
    import random

    monkeypatch.setattr(tracing, "RATIO", 0.2)
    monkeypatch.setattr(tracing, "random", random.Random(7))
    hits = sum(tracing.sampled() for _ in range(5000))
    assert hits / 5000 == pytest.approx(0.2, abs=0.02)


def test_default_ratio_is_one_in_two_hundred():
    assert tracing.RATIO == pytest.approx(0.005)


def test_reset_switches_tracing_off(exporter):
    tracing.reset()
    with tracing.span("work", force=True) as tp:
        assert tp is None
    assert exporter.get_finished_spans() == ()
