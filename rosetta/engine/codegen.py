"""Specialise a mapping spec into one straight-line Python function.

The interpreter in compiler.py walks a plan of closures for every event. That
is simple and is the reference behaviour, but it spends most of its time on
loop and call overhead. Here the same plan is turned into source code with the
paths, factors and range checks written out, then compiled once per mapping
version. The result does the same work with a fraction of the calls.

Safety. A mapping spec can come from the AI agent, so nothing from a spec is
ever pasted into the source as text:
  * the spec has already passed compile_spec, so every op is on the whitelist
  * numbers are embedded with repr() after a finite-float check
  * strings (paths, field names) are embedded with repr(), which always yields
    a valid Python string literal and nothing else
  * every non-trivial transform stays a call to the closure built by
    transforms.py: the generated code only refers to it by an index

Equivalence with the interpreter is checked by a differential test on every
dialect (tests/unit/test_codegen.py).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from ..domain import canonical as C
from ..domain.dtc import DTC_RE
from ..domain.errors import INVALID, OVERSIZE, SCHEMA_MISMATCH, TRANSFORM_ERROR, NormalizeError
from ..domain.transforms import UNITS, _num, compile_transform
from ..domain.vin import is_valid_vin
from .decoders import MAX_PAYLOAD_BYTES, parse_path

_VALIDATION_ORDER = [f.name for f in C.CANONICAL_FIELDS]


def _check_vin(v: Any) -> None:
    ok = C._VIN_OK
    if v not in ok:
        if not is_valid_vin(v):
            raise NormalizeError(INVALID, "vin", C.R_VIN)
        if len(ok) >= C._VIN_CACHE_MAX:
            ok.clear()
        ok.add(v)


def _check_dtc(v: Any) -> None:
    if not isinstance(v, list):
        raise NormalizeError(INVALID, "dtc", C.R_TYPE)
    m = DTC_RE.match
    for code in v:
        if not isinstance(code, str) or not m(code):
            raise NormalizeError(INVALID, "dtc", C.R_DTC)


def _numeric_inline(t: Any) -> tuple[float, float, bool] | None:
    """(factor, offset, to_int) for transforms that are plain arithmetic, else None."""
    if isinstance(t, str):
        t = {"op": t}
    op = t.get("op")
    if op == "unit":
        f, o = UNITS[t["quantity"]][t["from"]]
        return (f, o, t["quantity"] == "time")
    if op == "scale":
        return (float(t["factor"]), 0.0, False)
    if op == "affine":
        return (float(t["factor"]), float(t.get("offset", 0.0)), False)
    return None


def generate(oem: str, version: int, spec: dict[str, Any], decode: Callable[[bytes], Any]) -> tuple[Callable, str]:
    """Return (normalize_function, source). Raises if the spec cannot be specialised."""
    fields = spec["fields"]
    ns: dict[str, Any] = {
        "_decode": decode, "_NE": NormalizeError, "_num": _num, "_check_vin": _check_vin,
        "_check_dtc": _check_dtc, "_EVT": frozenset(C.EVENT_TYPES), "_MAX": MAX_PAYLOAD_BYTES,
        "_int": int, "_float": float, "_round": round, "_str": str, "_list": list, "_len": len,
    }
    L: list[str] = ["def normalize(payload):",
                    "    if _len(payload) > _MAX:",
                    f"        raise _NE({OVERSIZE!r}, '', _str(_len(payload)) + ' bytes')",
                    "    obj = _decode(payload)",
                    "    ev = {}"]
    names = sorted(fields, key=lambda n: (not C.FIELD_BY_NAME[n].required, _VALIDATION_ORDER.index(n)))
    for k, name in enumerate(names):
        fm = fields[name]
        fs = C.FIELD_BY_NAME[name]
        path = fm["path"]
        steps = parse_path(path)
        access = "obj" + "".join(f"[{s!r}]" for s in steps)
        var = f"v{k}"
        L += ["    try:",
              f"        {var} = {access}",
              "    except (KeyError, IndexError, TypeError):",
              f"        {var} = None"]
        if fs.required:
            L += [f"    if {var} is None or {var} == '':",
                  f"        raise _NE({SCHEMA_MISMATCH!r}, {name!r}, {('source field ' + repr(path) + ' is missing')!r})"]
            ind = "    "
        else:
            L += [f"    if {var} is not None and {var} != '':"]
            ind = "        "
        body: list[str] = []
        for j, t in enumerate(fm.get("transforms") or []):
            num = _numeric_inline(t)
            if num is not None:
                f, o, to_int = num
                if not (math.isfinite(f) and math.isfinite(o)):
                    raise ValueError("non-finite factor")
                body.append(f"if {var}.__class__ is not float and {var}.__class__ is not int: {var} = _num({var})")
                expr = var if f == 1.0 else f"{var} * {f!r}"
                if o != 0.0:
                    expr += f" + {o!r}"
                if to_int:
                    expr = f"_int(_round({expr}))"
                elif f == 1.0 and o == 0.0:
                    expr = f"_float({var})"
                body.append(f"{var} = {expr}")
            else:
                key = f"_t{k}_{j}"
                ns[key] = compile_transform(t)
                body.append(f"{var} = {key}({var})")
        kind = fs.kind
        if kind == "int":
            body.append(f"if {var}.__class__ is not int: {var} = _int(_round(_float({var})))")
        elif kind == "float":
            body.append(f"if {var}.__class__ is not float: {var} = _float({var})")
        elif kind == "str":
            body.append(f"if {var}.__class__ is not str: {var} = _str({var})")
        elif kind == "list[str]":
            body.append(f"if {var}.__class__ is not list: {var} = [{var}]")
        if body:
            L.append(f"{ind}try:")
            L += [f"{ind}    {b}" for b in body]
            L.append(f"{ind}except Exception as e:")
            L.append(f"{ind}    raise _NE({TRANSFORM_ERROR!r}, {name!r}, {path + ': '!r} + _str(e)[:100]) from None")
        # a transform may legitimately produce None (an enum with no match): then the field is absent
        L.append(f"{ind}if {var} is not None: ev[{name!r}] = {var}")

    # validation, in the same order as canonical.validate so both report the same first error
    L.append("    g = ev.get")
    for name in C.REQUIRED:
        if name not in fields:
            raise ValueError(f"required field {name} not mapped")
        L += [f"    if g({name!r}) is None: raise _NE({INVALID!r}, {name!r}, {C.R_MISSING!r})"]
    L.append("    _check_vin(ev['vin'])")
    for f in C.CANONICAL_FIELDS:
        if f.lo is None or f.name not in fields:
            continue
        L.append(f"    x = g({f.name!r})")
        L.append("    if x is not None:")
        L.append(f"        if x.__class__ is not float and x.__class__ is not int: "
                 f"raise _NE({INVALID!r}, {f.name!r}, {C.R_TYPE!r})")
        L.append(f"        if not ({f.lo!r} <= x <= {f.hi!r}): raise _NE({INVALID!r}, {f.name!r}, {C.R_RANGE!r})")
    if "ignition" in fields:
        L += ["    x = g('ignition')",
              f"    if x is not None and x.__class__ is not bool: raise _NE({INVALID!r}, 'ignition', {C.R_TYPE!r})"]
    if "dtc" in fields:
        L += ["    x = g('dtc')", "    if x: _check_dtc(x)"]
    if "evt" in fields:
        L += ["    x = g('evt')",
              f"    if x is not None and x not in _EVT: raise _NE({INVALID!r}, 'evt', {C.R_EVT!r})"]
    L += [f"    ev['oem'] = {oem!r}", f"    ev['map_v'] = {int(version)!r}", "    return ev"]
    src = "\n".join(L) + "\n"
    # Reviewed (ADR 0001): src holds only repr() of validated literals, never raw input.
    exec(compile(src, f"<adapter {oem} v{int(version)}>", "exec"), ns)  # noqa: S102 # nosec B102 # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    return ns["normalize"], src
