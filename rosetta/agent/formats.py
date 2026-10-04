"""Wire-format detection: look at raw bytes and decide how to decode them."""
from __future__ import annotations

from collections import Counter
from typing import Any

import orjson

from ..engine.decoders import build_decoder

DELIMS = ("|", ";", ",", "\t")


class UnsupportedFormat(Exception):
    """The payloads cannot be decoded without more information from the OEM."""


def _pivot(objs: list[Any]) -> dict[str, str] | None:
    """Find a list of {name, value} objects, the 'signal array' style."""
    for obj in objs[:20]:
        if not isinstance(obj, dict):
            continue
        for k, v in obj.items():
            if isinstance(v, list) and len(v) >= 3 and all(isinstance(i, dict) and len(i) == 2 for i in v):
                keys = list(v[0].keys())
                if any(list(i.keys()) != keys for i in v):
                    continue
                for name_key, val_key in ((keys[0], keys[1]), (keys[1], keys[0])):
                    names = [i[name_key] for i in v]
                    if all(isinstance(n, str) for n in names) and len(set(names)) == len(names):
                        return {"path": k, "key": name_key, "value": val_key, "into": "sig"}
    return None


def _try_registered(sample: list[bytes], known: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    """Decode binary samples with a schema the OEM already registered, if one fits."""
    for where, cfg in known:
        try:
            dec = build_decoder(cfg)
        except Exception:
            continue
        ok = 0
        for p in sample:
            try:
                o = dec(p)
            except Exception:
                continue
            ok += isinstance(o, dict) and bool(o)
        if ok >= 0.8 * len(sample):
            return {"decoder": cfg,
                    "evidence": f"payloads are binary; {ok}/{len(sample)} decode with the "
                                f"{cfg['type']} schema '{cfg.get('message', '?')}' registered in {where}"}
    return None


def detect(payloads: list[bytes], known: list[tuple[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Return a decoder config for the payloads, with the evidence that led to it.

    `known` holds binary decoder configs the OEM already supplied (with their
    schema), as (where it was found, config). They are the only way to read binary.
    """
    if not payloads:
        raise UnsupportedFormat("no sample payloads")
    sample = payloads[:200]
    json_objs = []
    for p in sample:
        if p[:1] == b"{":
            try:
                json_objs.append(orjson.loads(p))
            except orjson.JSONDecodeError:
                pass
    if len(json_objs) >= 0.8 * len(sample):
        cfg: dict[str, Any] = {"type": "json"}
        piv = _pivot(json_objs)
        if piv:
            cfg["pivot"] = piv
        return {"decoder": cfg, "evidence": f"{len(json_objs)}/{len(sample)} samples parse as JSON objects"
                + (f"; signal array at '{piv['path']}' pivoted by '{piv['key']}'" if piv else "")}
    texts = []
    for p in sample:
        try:
            t = p.decode("utf-8")
        except UnicodeDecodeError:
            continue
        t = t.rstrip("\r\n")
        if t.isprintable() or "\t" in t:
            texts.append(t)
    if len(texts) >= 0.8 * len(sample):
        if sum(1 for t in texts if "=" in t) >= 0.8 * len(texts):
            for sep in (";", "&", ","):
                if sum(1 for t in texts if t.count(sep) >= 3) >= 0.8 * len(texts):
                    return {"decoder": {"type": "kv", "pair_sep": sep, "kv_sep": "="},
                            "evidence": f"key=value pairs separated by {sep!r}"}
        for d in DELIMS:
            counts = Counter(t.count(d) for t in texts)
            n, freq = counts.most_common(1)[0]
            if n >= 3 and freq >= 0.8 * len(texts):
                return {"decoder": {"type": "delimited", "delimiter": d,
                                    "columns": [f"c{i}" for i in range(n + 1)]},
                        "evidence": f"{freq}/{len(texts)} samples have {n + 1} columns separated by {d!r}"}
    found = _try_registered(sample, known or [])
    if found:
        return found
    raise UnsupportedFormat(
        "payloads look binary and no registered schema decodes them. A Protobuf or other "
        "binary format needs the schema descriptor from the OEM before it can be mapped")


def decode_all(decoder_cfg: dict[str, Any], payloads: list[bytes], devices: list[str]) -> tuple[list[Any], list[str], int]:
    """Decode what can be decoded. Returns (objects, devices, number that failed)."""
    dec = build_decoder(decoder_cfg)
    objs, devs, bad = [], [], 0
    for p, d in zip(payloads, devices):
        try:
            o = dec(p)
        except Exception:
            bad += 1
            continue
        if isinstance(o, dict):
            if decoder_cfg.get("type") == "json" and decoder_cfg.get("pivot"):
                o = {k: v for k, v in o.items() if k != decoder_cfg["pivot"]["path"]}
            objs.append(o)
            devs.append(d)
        else:
            bad += 1
    return objs, devs, bad
