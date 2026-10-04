"""Wire-format decoders: bytes in, a Python object out.

Four families cover the dialects we have seen: JSON (optionally with a
name/value signal array that is pivoted into an object), delimited text,
key=value text, and Protobuf (message class built from a descriptor stored in
the mapping spec, so no generated code and no redeploy is needed).
"""
from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any

import orjson

from ..domain.errors import DECODE_ERROR, NormalizeError, SpecError

Decoder = Callable[[bytes], Any]

MAX_PAYLOAD_BYTES = 64 * 1024
DECODER_TYPES = ("json", "delimited", "kv", "protobuf")


_SEGMENT = re.compile(r"^([^\[\]]*)((?:\[-?[0-9]+\])*)$")
_INDEX = re.compile(r"\[(-?[0-9]+)\]")


def parse_path(path: str) -> tuple[Any, ...]:
    """'a.b[2].c' -> ('a', 'b', 2, 'c'). Rejects anything that is not a plain path."""
    if not isinstance(path, str) or not path or len(path) > 200:
        raise SpecError("path must be a non-empty string of at most 200 chars")
    steps: list[Any] = []
    for part in path.split("."):
        m = _SEGMENT.match(part)
        if not part or m is None or (not m.group(1) and not m.group(2)):
            raise SpecError(f"bad segment {part!r} in path {path!r}")
        if m.group(1):
            steps.append(m.group(1))
        steps.extend(int(i) for i in _INDEX.findall(m.group(2)))
    return tuple(steps)


def make_getter(path: str) -> Callable[[Any], Any]:
    """Compile a path into a function that returns the value or None when absent."""
    steps = parse_path(path)
    if len(steps) == 1:
        key = steps[0]

        def get1(obj: Any) -> Any:
            try:
                return obj[key]
            except (KeyError, IndexError, TypeError):
                return None

        return get1

    def get(obj: Any) -> Any:
        try:
            for s in steps:
                obj = obj[s]
            return obj
        except (KeyError, IndexError, TypeError):
            return None

    return get


def flatten(obj: Any, prefix: str = "", out: dict[str, Any] | None = None, depth: int = 0) -> dict[str, Any]:
    """Flatten a decoded object into {path: leaf}. Used by the profiler, not the hot path."""
    if out is None:
        out = {}
    if depth > 8:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(obj, list) and obj and all(isinstance(x, (dict, list)) for x in obj):
        for i, v in enumerate(obj[:8]):
            flatten(v, f"{prefix}[{i}]", out, depth + 1)
    else:
        out[prefix] = obj
    return out


def _json_decoder(cfg: dict[str, Any]) -> Decoder:
    pivot = cfg.get("pivot")
    if pivot is not None and not isinstance(pivot, dict):
        raise SpecError("json.pivot must be an object")
    if pivot is None:
        def decode(payload: bytes) -> Any:
            try:
                return orjson.loads(payload)
            except orjson.JSONDecodeError as e:
                raise NormalizeError(DECODE_ERROR, "", str(e)[:120]) from None
        return decode

    for k in ("path", "key", "value", "into"):
        if not isinstance(pivot.get(k), str) or not pivot[k]:
            raise SpecError("json.pivot needs string path, key, value and into")
    get_arr = make_getter(pivot["path"])
    key, val, into = pivot["key"], pivot["value"], pivot["into"]

    def decode_pivot(payload: bytes) -> Any:
        try:
            obj = orjson.loads(payload)
            arr = get_arr(obj)
            obj[into] = {item[key]: item[val] for item in arr}
            return obj
        except NormalizeError:
            raise
        except Exception as e:  # malformed array, wrong types
            raise NormalizeError(DECODE_ERROR, pivot["path"], str(e)[:120]) from None

    return decode_pivot


def _delimited_decoder(cfg: dict[str, Any]) -> Decoder:
    delim, cols = cfg.get("delimiter"), cfg.get("columns")
    if not isinstance(delim, str) or not 1 <= len(delim) <= 4:
        raise SpecError("delimited.delimiter must be a 1-4 char string")
    if not isinstance(cols, list) or not cols or len(cols) > 128 or not all(isinstance(c, str) and c for c in cols):
        raise SpecError("delimited.columns must be a non-empty list of names")
    n = len(cols)
    names = tuple(cols)

    def decode(payload: bytes) -> Any:
        try:
            parts = payload.decode("utf-8").rstrip("\r\n").split(delim)
        except UnicodeDecodeError as e:
            raise NormalizeError(DECODE_ERROR, "", str(e)[:120]) from None
        if len(parts) != n:
            raise NormalizeError(DECODE_ERROR, "", f"expected {n} columns, got {len(parts)}")
        return dict(zip(names, parts))

    return decode


def _kv_decoder(cfg: dict[str, Any]) -> Decoder:
    pair_sep, kv_sep = cfg.get("pair_sep", ";"), cfg.get("kv_sep", "=")
    for s in (pair_sep, kv_sep):
        if not isinstance(s, str) or not 1 <= len(s) <= 4:
            raise SpecError("kv separators must be 1-4 char strings")

    def decode(payload: bytes) -> Any:
        try:
            text = payload.decode("utf-8").strip()
            out = {}
            for pair in text.split(pair_sep):
                if not pair:
                    continue
                k, sep, v = pair.partition(kv_sep)
                if not sep:
                    raise ValueError(f"pair without {kv_sep!r}: {pair[:40]!r}")
                out[k.strip()] = v
            return out
        except (UnicodeDecodeError, ValueError) as e:
            raise NormalizeError(DECODE_ERROR, "", str(e)[:120]) from None

    return decode


def build_message_class(descriptor_b64: str, message: str) -> Any:
    """Build a Protobuf message class at runtime from a base64 FileDescriptorSet."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    try:
        fds = descriptor_pb2.FileDescriptorSet.FromString(base64.b64decode(descriptor_b64))
        pool = descriptor_pool.DescriptorPool()
        for fd in fds.file:
            pool.Add(fd)
        return message_factory.GetMessageClass(pool.FindMessageTypeByName(message))
    except Exception as e:
        raise SpecError(f"bad protobuf descriptor: {e}") from None


def _pb_plan(descriptor: Any) -> list[tuple[str, int, Any]]:
    """Per message type, decide once how each field is read. kind: 0 scalar,
    1 optional scalar, 2 repeated scalar, 3 message, 4 repeated message."""
    plan = []
    for f in descriptor.fields:
        sub = _pb_plan(f.message_type) if f.message_type is not None else None
        if f.is_repeated:
            plan.append((f.name, 4 if sub is not None else 2, sub))
        elif sub is not None:
            plan.append((f.name, 3, sub))
        else:
            plan.append((f.name, 1 if f.has_presence else 0, None))
    return plan


def _pb_to_obj(msg: Any, plan: list[tuple[str, int, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, kind, sub in plan:
        if kind == 0:
            out[name] = getattr(msg, name)
        elif kind == 1:
            out[name] = getattr(msg, name) if msg.HasField(name) else None
        elif kind == 2:
            out[name] = list(getattr(msg, name))
        elif kind == 3:
            out[name] = _pb_to_obj(getattr(msg, name), sub) if msg.HasField(name) else None
        else:
            out[name] = [_pb_to_obj(x, sub) for x in getattr(msg, name)]
    return out


def _protobuf_decoder(cfg: dict[str, Any]) -> Decoder:
    desc, name = cfg.get("descriptor_b64"), cfg.get("message")
    if not isinstance(desc, str) or not isinstance(name, str) or len(desc) > 200_000:
        raise SpecError("protobuf decoder needs descriptor_b64 and message")
    cls = build_message_class(desc, name)
    plan = _pb_plan(cls.DESCRIPTOR)
    parse = cls.FromString

    def decode(payload: bytes) -> Any:
        try:
            return _pb_to_obj(parse(payload), plan)
        except Exception as e:
            raise NormalizeError(DECODE_ERROR, "", str(e)[:120]) from None

    return decode


_BUILDERS = {
    "json": _json_decoder,
    "delimited": _delimited_decoder,
    "kv": _kv_decoder,
    "protobuf": _protobuf_decoder,
}


def build_decoder(cfg: dict[str, Any]) -> Decoder:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("type"), str) or cfg["type"] not in _BUILDERS:
        raise SpecError(f"decoder.type must be one of {DECODER_TYPES}")
    return _BUILDERS[cfg["type"]](cfg)
