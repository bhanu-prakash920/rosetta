"""rosetta.observability.logs: one JSON object per log line, without secrets."""
from __future__ import annotations

import json
import logging
import sys

import pytest

from rosetta.observability import logs
from rosetta.observability.logs import JsonFormatter


def record(msg: str = "hello", *args, level: int = logging.INFO, name: str = "rosetta.test", exc_info=None,
           **extra) -> logging.LogRecord:
    rec = logging.LogRecord(name, level, __file__, 10, msg, args, exc_info)
    rec.created, rec.msecs = 1_790_000_000.0, 7.0
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def fmt(rec: logging.LogRecord) -> dict:
    line = JsonFormatter().format(rec)
    assert "\n" not in line, "one line per record"
    return json.loads(line)


@pytest.fixture()
def clean_root():
    """Give the test a root logger that was never configured, and restore it afterwards."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, getattr(root, "_rosetta", None),
             {n: logging.getLogger(n).level for n in ("uvicorn.access", "sqlalchemy.engine")})
    root.handlers[:] = []
    if hasattr(root, "_rosetta"):
        del root._rosetta
    yield root
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    if saved[2] is None:
        if hasattr(root, "_rosetta"):
            del root._rosetta
    else:
        root._rosetta = saved[2]
    for name, level in saved[3].items():
        logging.getLogger(name).setLevel(level)


# ------------------------------------------------------------------ formatter
def test_standard_fields(monkeypatch):
    monkeypatch.setenv("ROSETTA_SERVICE", "normalizer")
    out = fmt(record("batch of %d done in %s ms", 500, 12.5, level=logging.WARNING))
    assert out["msg"] == "batch of 500 done in 12.5 ms"
    assert out["level"] == "WARNING" and out["logger"] == "rosetta.test"
    assert out["service"] == "normalizer"
    assert out["ts"] == "2026-09-21T14:13:20.007Z"
    assert isinstance(out["pid"], int)
    assert set(out) == {"ts", "level", "logger", "msg", "service", "pid"}


def test_service_defaults_to_api(monkeypatch):
    monkeypatch.delenv("ROSETTA_SERVICE", raising=False)
    assert fmt(record())["service"] == "api"


def test_extra_fields_are_added():
    out = fmt(record(oem="pacifica", version=3, latency_ms=12.5, tags=["a", "b"], detail={"k": 1}))
    assert (out["oem"], out["version"], out["latency_ms"], out["tags"], out["detail"]) == \
        ("pacifica", 3, 12.5, ["a", "b"], {"k": 1})


def test_private_attributes_are_left_out():
    assert "_internal" not in fmt(record(_internal="x"))


@pytest.mark.parametrize("key", ["password", "user_password", "PASSWORD", "jwt_secret", "token", "access_token",
                                 "Authorization", "api_key", "apikey", "X_API_KEY", "refreshToken"])
def test_secrets_are_redacted(key):
    out = fmt(record(**{key: "hunter2-very-secret"}))
    assert out[key] == "[redacted]"
    assert "hunter2" not in json.dumps(out)


def test_harmless_fields_with_similar_names_are_kept():
    out = fmt(record(passed=True, keys=3, author="ops"))
    assert (out["passed"], out["keys"], out["author"]) == (True, 3, "ops")


def test_values_that_json_cannot_encode_are_turned_into_text():
    class Thing:
        def __str__(self) -> str:
            return "a thing"

    out = fmt(record(obj=Thing(), blob=b"\x00\x01", when={1, }))
    assert out["obj"] == "a thing" and isinstance(out["blob"], str) and isinstance(out["when"], str)


def test_exception_is_included():
    try:
        raise ValueError("boom")
    except ValueError:
        out = fmt(record("failed", level=logging.ERROR, exc_info=sys.exc_info()))
    assert out["exc"].startswith("Traceback")
    assert out["exc"].rstrip().endswith("ValueError: boom")
    assert out["msg"] == "failed"


def test_long_traceback_keeps_its_end():
    try:
        raise RuntimeError("x" * 5000 + " the actual cause")
    except RuntimeError:
        out = fmt(record("failed", exc_info=sys.exc_info()))
    assert len(out["exc"]) == 2000
    assert out["exc"].endswith(" the actual cause")


def test_message_with_quotes_newlines_and_unicode_is_valid_json():
    out = fmt(record('he said "hi"\nthen left: é中'))
    assert out["msg"] == 'he said "hi"\nthen left: é中'


# ------------------------------------------------------------------ configure
def test_configure_installs_one_json_handler(clean_root, capsys):
    logs.configure("debug")
    assert len(clean_root.handlers) == 1
    assert isinstance(clean_root.handlers[0].formatter, JsonFormatter)
    assert clean_root.level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING


def test_configured_logger_writes_json_lines_to_stdout(clean_root, monkeypatch):
    import io

    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    logs.configure("INFO")
    logging.getLogger("rosetta.unit").info("mapping promoted", extra={"oem": "helix", "token": "abc"})
    logging.getLogger("rosetta.unit").debug("not shown")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    out = json.loads(lines[0])
    assert (out["msg"], out["oem"], out["token"], out["logger"]) == ("mapping promoted", "helix", "[redacted]",
                                                                     "rosetta.unit")


def test_configure_is_idempotent(clean_root):
    logs.configure("INFO")
    handler = clean_root.handlers[0]
    logs.configure("DEBUG")
    assert clean_root.handlers == [handler]
    assert clean_root.level == logging.INFO, "the first configuration wins"


def test_level_comes_from_the_environment(clean_root, monkeypatch):
    monkeypatch.setenv("ROSETTA_LOG_LEVEL", "warning")
    logs.configure()
    assert clean_root.level == logging.WARNING


def test_default_level_is_info(clean_root, monkeypatch):
    monkeypatch.delenv("ROSETTA_LOG_LEVEL", raising=False)
    logs.configure()
    assert clean_root.level == logging.INFO


def test_configure_replaces_existing_handlers(clean_root):
    clean_root.addHandler(logging.NullHandler())
    clean_root.addHandler(logging.NullHandler())
    logs.configure()
    assert len(clean_root.handlers) == 1


def test_a_line_logged_inside_a_span_carries_its_trace_id():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from rosetta.observability import tracing
    tracing.reset()
    exporter = InMemorySpanExporter()
    try:
        assert tracing.setup("test", exporter=exporter) is True
        with tracing.span("unit", force=True):
            inside = fmt(record("in a span"))
        outside = fmt(record("no span"))
        trace_id = format(exporter.get_finished_spans()[0].context.trace_id, "032x")
        assert inside["trace_id"] == trace_id
        assert "trace_id" not in outside
    finally:
        tracing.reset()
