"""Structured logs: one JSON object per line, ready for Loki, ELK or Splunk."""
from __future__ import annotations

import json
import logging
import os
import sys
import time

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
# Never let these reach a log line, whatever a caller passes in `extra`.
_SECRET = ("password", "secret", "token", "authorization", "api_key", "apikey")


def _trace_id() -> str | None:
    """The id of the span recorded right now in this thread, if tracing is on."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
               "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
               "service": os.environ.get("ROSETTA_SERVICE", "api"), "pid": record.process}
        for k, v in record.__dict__.items():
            if k in _STD or k.startswith("_"):
                continue
            out[k] = "[redacted]" if any(s in k.lower() for s in _SECRET) else v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-2000:]
        tid = _trace_id()
        if tid:
            out["trace_id"] = tid     # Grafana links the line to the trace in Tempo
        return json.dumps(out, default=str)


def configure(level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_rosetta", False):
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.handlers[:] = [h]
    root.setLevel((level or os.environ.get("ROSETTA_LOG_LEVEL", "INFO")).upper())
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._rosetta = True  # type: ignore[attr-defined]
