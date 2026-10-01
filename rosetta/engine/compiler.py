"""Compile a mapping spec (data) into an Adapter (fast callable).

Spec shape:
    {
      "oem": "pacifica",
      "decoder": {"type": "json"},
      "fields": {
        "speed_kmh": {"path": "spd_mph",
                      "transforms": [{"op": "unit", "quantity": "speed", "from": "mph"}]},
        ...
      }
    }

Compiling happens once per mapping version. The hot path then only runs
closures: one decode plus one getter and a short transform chain per field.
Time per event O(f * t) for f mapped fields and t transforms per field.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..domain import canonical as C
from ..domain.errors import (
    INVALID,
    OVERSIZE,
    SCHEMA_MISMATCH,
    TRANSFORM_ERROR,
    NormalizeError,
    SpecError,
)
from ..domain.transforms import TransformError, compile_transform
from .decoders import MAX_PAYLOAD_BYTES, build_decoder, make_getter

MAX_TRANSFORMS = 8


def _coerce(kind: str) -> Callable[[Any], Any] | None:
    if kind == "int":
        return lambda v: v if type(v) is int else int(round(float(v)))
    if kind == "float":
        return lambda v: v if type(v) is float else float(v)
    if kind == "str":
        return lambda v: v if type(v) is str else str(v)
    if kind == "list[str]":
        return lambda v: v if isinstance(v, list) else [v]
    return None


class Adapter:
    """A compiled mapping version for one OEM source."""

    __slots__ = ("oem", "version", "spec", "_decode", "_plan", "normalize", "source", "specialised")

    def __init__(self, oem: str, version: int, spec: dict[str, Any], specialise: bool = True) -> None:
        self.oem = oem
        self.version = version
        self.spec = spec
        self._decode = build_decoder(spec.get("decoder") or {})
        self._plan = _build_plan(spec)
        self.normalize = self.normalize_interpreted
        self.source = ""
        self.specialised = False
        if specialise:
            try:
                from .codegen import generate

                self.normalize, self.source = generate(oem, version, spec, self._decode)
                self.specialised = True
            except Exception:
                # Any doubt: fall back to the interpreter, which is always correct.
                self.normalize = self.normalize_interpreted

    def decode(self, payload: bytes) -> Any:
        return self._decode(payload)

    def map_decoded(self, obj: Any) -> dict[str, Any]:
        """Map an already decoded object to a canonical event (no validation)."""
        ev: dict[str, Any] = {}
        for name, getter, chain, required, path in self._plan:
            v = getter(obj)
            if v is None or v == "":
                if required:
                    raise NormalizeError(SCHEMA_MISMATCH, name, f"source field {path!r} is missing")
                continue
            try:
                for fn in chain:
                    v = fn(v)
            except Exception as e:
                raise NormalizeError(TRANSFORM_ERROR, name, f"{path}: {str(e)[:100]}") from None
            if v is not None:
                ev[name] = v
        return ev

    def normalize_interpreted(self, payload: bytes) -> dict[str, Any]:
        """bytes -> validated canonical event. Raises NormalizeError with a reason code.

        Reference implementation. `normalize` points at the specialised version
        when code generation succeeded, and at this method otherwise."""
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise NormalizeError(OVERSIZE, "", f"{len(payload)} bytes")
        ev = self.map_decoded(self._decode(payload))
        bad = C.validate(ev)
        if bad is not None:
            raise NormalizeError(INVALID, bad[1], bad[0])
        ev["oem"] = self.oem
        ev["map_v"] = self.version
        return ev


def _build_plan(spec: dict[str, Any]) -> tuple[tuple[str, Callable, tuple, bool, str], ...]:
    fields = spec.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SpecError("spec.fields must be a non-empty object")
    unknown = sorted(set(fields) - set(C.FIELD_BY_NAME))
    if unknown:
        raise SpecError(f"unknown canonical fields: {unknown}")
    missing = [n for n in C.REQUIRED if n not in fields]
    if missing:
        raise SpecError(f"required canonical fields are not mapped: {missing}")
    plan = []
    for name, fm in fields.items():
        if not isinstance(fm, dict) or "path" not in fm:
            raise SpecError(f"field {name!r} needs a 'path'")
        transforms = fm.get("transforms") or []
        if not isinstance(transforms, list) or len(transforms) > MAX_TRANSFORMS:
            raise SpecError(f"field {name!r}: transforms must be a list of at most {MAX_TRANSFORMS}")
        try:
            chain = [compile_transform(t) for t in transforms]
        except TransformError as e:
            raise SpecError(f"field {name!r}: {e}") from None
        co = _coerce(C.FIELD_BY_NAME[name].kind)
        if co is not None:
            chain.append(co)
        plan.append((name, make_getter(fm["path"]), tuple(chain), C.FIELD_BY_NAME[name].required, fm["path"]))
    # required fields first so a format change fails fast
    order = [f.name for f in C.CANONICAL_FIELDS]
    plan.sort(key=lambda p: (not p[3], order.index(p[0])))   # same order as codegen: same first error
    return tuple(plan)


def compile_spec(oem: str, version: int, spec: dict[str, Any], specialise: bool = True) -> Adapter:
    """Validate and compile. Raises SpecError when the spec is not acceptable."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    try:
        return _compile(oem, version, spec, specialise)
    except SpecError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError, IndexError) as e:
        # Whatever the shape of a malformed spec, the caller sees one kind of error.
        raise SpecError(f"malformed spec: {type(e).__name__}: {e}") from None


def _compile(oem: str, version: int, spec: dict[str, Any], specialise: bool) -> Adapter:
    allowed = {"oem", "decoder", "fields", "notes", "schema_version"}
    extra = sorted(set(spec) - allowed)
    if extra:
        raise SpecError(f"unexpected keys in spec: {extra}")
    return Adapter(oem, version, spec, specialise)
