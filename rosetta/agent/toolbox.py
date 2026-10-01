"""The agent's tools, and the guardrails around them.

Both engines (the deterministic workflow and the Claude tool-use loop) work
through this one class, so they have exactly the same powers:

  read      sample dead letters, profile fields, search the mapping memory,
            ask the classifier for a proposal, learn event codes from examples
  test      run a candidate mapping against the development half of the golden set
  write     submit ONE draft mapping, which is then validated on the full golden set

What the agent cannot do, by construction:
  * write transforms. It picks an encoding from a fixed list (for example
    "speed|mph"); code turns that into transforms. There is no free-form field.
  * touch production. A draft is inert. Approval, promotion and rollback are
    refused for non-human actors inside the registry itself.
  * act unobserved. Every tool call is stored as an agent step with its input,
    output and duration, and written to the hash-chained audit log.
  * be steered by payload content. Field names and values from vehicles are
    returned as data inside JSON. Nothing in them is ever executed or used as
    an instruction, and whatever a model concludes still has to pass the golden set.
"""
from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AgentRun, AgentStep, DlqGroup, DlqSample, MappingVersion, Oem
from ..domain import canonical as C
from ..domain.canonical import EVENT_TYPES
from ..engine.router import ST_ACTIVE
from ..ml import model as mlmodel
from ..ml.labels import CANONICAL_OF, LABELS, transforms_of
from ..ml.profile import PHYSICS_FEATURES, VALUE_FEATURES, FieldProfile, add_physics, build_profiles, make_samples
from ..pipeline.dlq import FORMAT_REASONS, shape_of
from ..services import audit, registry
from ..services.golden import run_golden
from . import formats, memory

MAX_STEPS = 40
ENCODINGS: dict[str, list[str]] = {}
for _label, (_canon, _t) in LABELS.items():
    ENCODINGS.setdefault(_canon, []).append(_label)


class ToolError(Exception):
    """A tool was called with something it cannot act on. Returned to the agent as an error."""


def _short(v: Any, n: int = 80) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "..."
    if isinstance(v, bytes):
        return v[:n].decode("utf-8", "replace")
    return v


class Toolbox:
    def __init__(self, s: Session, oem_key: str, run: AgentRun, label: str | None = None,
                 actor: str = "agent@rosetta.example") -> None:
        self.s = s
        self.oem = oem_key
        self.run = run
        self.label = label
        self.actor = actor
        self.steps = 0
        self.payloads: list[bytes] = []
        self.devices: list[str] = []
        self.decoder: dict[str, Any] | None = None
        self.profiles: dict[str, FieldProfile] = {}
        self.physics: dict[str, Any] = {}
        self.golden_dev: list[tuple[bytes, dict[str, Any]]] = []
        self.golden_n = 0
        self.submitted: MappingVersion | None = None
        self.evt_map: dict[str, str] = {}

    # ------------------------------------------------------------ bookkeeping
    def call(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        """Run one tool, record the step, write the audit entry. Never raises ToolError."""
        fn: Callable[..., dict[str, Any]] | None = getattr(self, f"t_{tool}", None)
        self.steps += 1
        t0 = time.perf_counter()
        ok = True
        if fn is None:
            out: dict[str, Any] = {"error": f"unknown tool {tool!r}"}
            ok = False
        elif self.steps > MAX_STEPS:
            out = {"error": f"step limit of {MAX_STEPS} reached"}
            ok = False
        else:
            try:
                out = fn(**kwargs)
            except ToolError as e:
                out, ok = {"error": str(e)}, False
            except registry.RegistryError as e:
                out, ok = {"error": str(e), "code": e.code}, False
            except TypeError as e:
                out, ok = {"error": f"bad arguments: {e}"}, False
        ms = (time.perf_counter() - t0) * 1000.0
        self.s.add(AgentStep(run_id=self.run.id, seq=self.steps, tool=tool, input=_jsonable(kwargs),
                             output=_jsonable(out), ok=ok, duration_ms=round(ms, 2)))
        audit.record(self.s, actor_kind="agent", actor=self.actor, action=f"agent.tool.{tool}",
                     resource=f"agent_run/{self.run.id}", outcome="ok" if ok else "error",
                     detail={"oem": self.oem, "step": self.steps, "ms": round(ms, 1)})
        self.s.flush()
        return out

    # ------------------------------------------------------------------ tools
    def t_sample_dead_letters(self, limit: int = 900) -> dict[str, Any]:
        """Collect payloads the platform could not read, and work out their wire format."""
        limit = max(10, min(int(limit), 3000))
        groups = self.s.execute(select(DlqGroup).where(DlqGroup.oem_key == self.oem)
                                .order_by(DlqGroup.last_seen.desc())).scalars().all()
        fmt = [g for g in groups if g.reason in FORMAT_REASONS]
        ids = [g.id for g in fmt]
        rows = []
        if ids:
            rows = self.s.execute(select(DlqSample.payload, DlqSample.device, DlqSample.tracked)
                                  .where(DlqSample.group_id.in_(ids)).order_by(DlqSample.id).limit(limit)).all()
        self.payloads = [bytes(r[0]) for r in rows]
        self.devices = [r[1] or f"?{i}" for i, r in enumerate(rows)]

        labels = registry.golden_labels(self.s, self.oem)
        if not self.label and labels:
            self.label = self._pick_label(labels)
        cases = registry.golden_cases(self.s, self.oem, self.label) if self.label else []
        self.golden_n = len(cases)
        # Development half only. The other half is never shown to the agent, so the
        # final validation measures the mapping, not how well it was fitted.
        self.golden_dev = cases[0::2]
        for i, (p, _e) in enumerate(self.golden_dev):
            self.payloads.append(p)
            self.devices.append(f"golden-{i}")
        if not self.payloads:
            raise ToolError(f"nothing to learn from: no dead letters and no golden cases for {self.oem!r}")
        try:
            det = formats.detect(self.payloads, self._registered_binary_decoders())
        except formats.UnsupportedFormat as e:
            raise ToolError(str(e)) from None
        self.decoder = det["decoder"]
        tracked = len({self.devices[i] for i, r in enumerate(rows) if r[2]})
        set_aside = self._set_aside_fragments(len(rows))
        if not self.payloads:
            raise ToolError("every parked sample is a fragment of a message, and there are no golden cases")
        return {
            "source": self.oem,
            "dead_letter_groups": [{"reason": g.reason, "field": g.field, "count": g.count,
                                    "open": g.count - g.replayed} for g in fmt[:8]],
            "samples": len(self.payloads), "from_dead_letters": len(rows) - set_aside,
            "fragments_set_aside": set_aside,
            "tracked_devices": tracked,
            "golden_label": self.label, "golden_cases_total": self.golden_n,
            "golden_cases_visible_to_agent": len(self.golden_dev),
            "wire_format": self.decoder, "evidence": det["evidence"],
            "examples": [_short(p, 400) for p in self.payloads[:2]],
        }

    def _set_aside_fragments(self, n_dead: int) -> int:
        """Drop parked samples that decode to far fewer fields than a whole message.

        Those are copies cut short in transit, not a dialect, and they would skew every
        statistic the profiler computes. Golden cases are always kept. Returns the count.
        """
        from ..engine.decoders import build_decoder, flatten

        dec = build_decoder(self.decoder or {})
        sizes: list[int | None] = []
        for p in self.payloads:
            try:
                o = dec(p)
            except Exception:
                o = None
            # Count values that carry information: Protobuf fills absent scalars with 0.
            sizes.append(sum(1 for v in flatten(o).values() if v not in (None, "", 0, False, [], {}))
                         if isinstance(o, dict) else None)
        golden = [n for n in sizes[n_dead:] if n]
        known = [n for n in sizes if n]
        if not known:
            return 0
        whole = float(np.median(golden)) if golden else float(np.percentile(known, 90))
        keep = [i for i, n in enumerate(sizes) if i >= n_dead or n is None or n >= 0.6 * whole]
        dropped = len(sizes) - len(keep)
        if dropped:
            self.payloads = [self.payloads[i] for i in keep]
            self.devices = [self.devices[i] for i in keep]
        return dropped

    def _registered_binary_decoders(self) -> list[tuple[str, dict[str, Any]]]:
        """Binary decoders (with their schema) in this source's mappings, live ones first."""
        rows = self.s.execute(
            select(MappingVersion.version, MappingVersion.state, MappingVersion.spec)
            .join(Oem, Oem.id == MappingVersion.oem_id).where(Oem.key == self.oem)
            .order_by(MappingVersion.version.desc())).all()
        rows.sort(key=lambda r: r[1] != ST_ACTIVE)
        out, seen = [], set()
        for version, _state, spec in rows:
            dec = (spec or {}).get("decoder") or {}
            if dec.get("type") == "protobuf" and dec.get("descriptor_b64"):
                key = (dec.get("message"), dec["descriptor_b64"])
                if key not in seen:
                    seen.add(key)
                    out.append((f"{self.oem} mapping v{version}", dec))
        return out

    def _pick_label(self, labels: list[str]) -> str:
        if len(labels) == 1:
            return labels[0]
        mine = Counter(shape_of(p)[1] for p in self.payloads[:200]) if self.payloads else Counter()
        if not mine:
            return labels[-1]
        keys = set().union(*mine.keys())
        best, best_j = labels[0], -1.0
        for lab in labels:
            gk: set[str] = set()
            for p, _ in registry.golden_cases(self.s, self.oem, lab)[:40]:
                gk |= shape_of(p)[1]
            j = len(keys & gk) / len(keys | gk) if keys | gk else 0.0
            if j > best_j:
                best, best_j = lab, j
        return best

    def _need_profiles(self) -> None:
        if not self.profiles:
            raise ToolError("call profile_fields first")

    def t_profile_fields(self) -> dict[str, Any]:
        """Statistics and physics evidence for every field in the samples."""
        if self.decoder is None:
            raise ToolError("call sample_dead_letters first")
        objs, devs, bad = formats.decode_all(self.decoder, self.payloads, self.devices)
        if not objs:
            raise ToolError("no sample could be decoded with the detected wire format")
        samples = make_samples(objs, devs)
        self.profiles = build_profiles(samples)
        self.physics = add_physics(self.profiles, samples)
        return {"decoded": len(objs), "undecodable": bad, "fields": len(self.profiles),
                "physics": _jsonable(self.physics),
                "profile": [self.profiles[p].summary() for p in self.profiles]}

    def t_search_memory(self, paths: list[str] | None = None, k: int = 2) -> dict[str, Any]:
        """Nearest fields from mappings a human already approved."""
        self._need_profiles()
        mem = memory.make_memory(self.s)
        live = {(o, v) for o, v in self.s.execute(
            select(Oem.key, MappingVersion.version).join(Oem, Oem.id == MappingVersion.oem_id)
            .where(MappingVersion.state == ST_ACTIVE))}
        out = {}
        for p in (paths or list(self.profiles)):
            prof = self.profiles.get(p)
            if prof is None:
                continue
            out[p] = [{"similarity": round(n.similarity, 3), "known_as": f"{n.oem} v{n.version}: {n.path}",
                       "encoding": n.label, "canonical": n.canonical}
                      for n in mem.search(memory.embed(prof), k=max(1, min(int(k), 5)), allowed=live)]
        return {"memory_size": mem.count(), "neighbours": out}

    def t_suggest_mapping(self) -> dict[str, Any]:
        """The classifier's proposal: one source field per canonical field, chosen jointly."""
        self._need_profiles()
        mapper = mlmodel.load()
        chosen = mapper.assign(self.profiles)
        cands = mapper.candidates(self.profiles, top=3)
        mem = self.t_search_memory(k=1)["neighbours"]
        fields = []
        for c in sorted(chosen, key=lambda c: list(CANONICAL_OF.values()).index(c.field)):
            prof = self.profiles[c.path]
            phys = {k: round(float(v), 3) for k, v in
                    zip(PHYSICS_FEATURES, prof.features[len(VALUE_FEATURES):]) if v}
            near = (mem.get(c.path) or [None])[0]
            fields.append({"canonical": c.field, "path": c.path, "encoding": c.label,
                           "confidence": round(c.prob, 3),
                           "alternatives": [{"encoding": a.label, "p": round(a.prob, 3)}
                                            for a in cands[c.path] if a.label != c.label and a.prob > 0.02][:2],
                           "examples": prof.examples[:3], "physics": phys, "memory": near})
        mapped = {f["canonical"] for f in fields}
        return {"model": mapper.meta.get("scores", {}), "fields": fields,
                "unmapped_required": [n for n in C.REQUIRED if n not in mapped],
                "unmapped_source_fields": [p for p in self.profiles if p not in {f["path"] for f in fields}]}

    def t_learn_event_codes(self, path: str) -> dict[str, Any]:
        """Translate the OEM's event codes by majority vote over labelled examples."""
        if self.decoder is None:
            raise ToolError("call sample_dead_letters first")
        if not self.golden_dev:
            raise ToolError("no labelled examples available")
        out = self.event_table(path)
        self.evt_map = out["codes"]
        return out

    def event_table(self, path: str) -> dict[str, Any]:
        """The event-code table for `path`, without changing the toolbox's state."""
        from ..engine.decoders import build_decoder, make_getter

        dec, get = build_decoder(self.decoder), make_getter(path)
        votes: dict[str, Counter] = {}
        for payload, expected in self.golden_dev:
            try:
                v = get(dec(payload))
            except Exception:
                continue
            want = expected.get("evt")
            if v is None or v == "" or want is None or isinstance(v, (dict, list)):
                continue    # an event code is a scalar; a whole sub-message would be memorised
            votes.setdefault(str(v), Counter())[want] += 1
        learned = {code: c.most_common(1)[0][0] for code, c in votes.items()}
        agree = sum(c.most_common(1)[0][1] for c in votes.values())
        total = sum(sum(c.values()) for c in votes.values())
        # A firmware update rarely renames event codes. Start from the table of the
        # live mapping of this source, when it reads events from the same field, and
        # let the labelled examples overrule it.
        inherited: dict[str, str] = {}
        for mv in self.s.execute(
                select(MappingVersion).join(Oem, Oem.id == MappingVersion.oem_id)
                .where(Oem.key == self.oem, MappingVersion.state == ST_ACTIVE)
                .order_by(MappingVersion.version.desc())).scalars():
            ef = (mv.spec.get("fields") or {}).get("evt")
            if ef and ef.get("path") == path:
                for t in ef.get("transforms") or []:
                    if isinstance(t, dict) and t.get("op") == "enum":
                        inherited = {str(k): v for k, v in t["map"].items() if v in EVENT_TYPES}
                break
        table = {**inherited, **learned}
        return {"path": path, "codes": table, "examples_used": total,
                "learned_from_examples": len(learned),
                "inherited_from_live_mapping": len(set(inherited) - set(learned)),
                "agreement": round(agree / total, 4) if total else None,
                "event_types_without_example": sorted(set(EVENT_TYPES) - set(table.values()))}

    def build_spec(self, fields: list[dict[str, Any]], evt_map: dict[str, str] | None = None) -> dict[str, Any]:
        if self.decoder is None:
            raise ToolError("call sample_dead_letters first")
        if not isinstance(fields, list) or not fields:
            raise ToolError("fields must be a non-empty list")
        spec_fields: dict[str, Any] = {}
        meta: dict[str, Any] = {}
        for f in fields:
            canon, path, enc = f.get("canonical"), f.get("path"), f.get("encoding")
            if canon not in C.FIELD_BY_NAME:
                raise ToolError(f"unknown canonical field {canon!r}")
            if enc not in ENCODINGS.get(canon, ()):
                raise ToolError(f"encoding {enc!r} is not valid for {canon}. Choose from {ENCODINGS.get(canon)}")
            if canon in spec_fields:
                raise ToolError(f"{canon} is mapped twice")
            if self.profiles and path not in self.profiles:
                raise ToolError(f"source field {path!r} does not exist in the samples")
            tr = transforms_of(enc)
            if canon == "evt":
                table = evt_map if evt_map is not None else self.evt_map
                identity = table and all(k == v for k, v in table.items())
                if table and not identity:
                    bad = sorted(set(table.values()) - set(EVENT_TYPES))
                    if bad:
                        raise ToolError(f"unknown event types in evt_map: {bad}")
                    tr = [{"op": "enum", "map": dict(table)}]
            spec_fields[canon] = {"path": path, "transforms": tr}
            meta[canon] = {"confidence": f.get("confidence"), "rationale": _short(f.get("rationale") or "", 600)}
        return {"oem": self.oem, "decoder": self.decoder, "fields": spec_fields, "_meta": {"fields": meta}}

    def t_validate_mapping(self, fields: list[dict[str, Any]],
                           evt_map: dict[str, str] | None = None) -> dict[str, Any]:
        """Run a candidate against the development half of the golden set."""
        if not self.golden_dev:
            raise ToolError("no golden cases available for this source")
        spec = self.build_spec(fields, evt_map)
        rep = run_golden(self.oem, {k: v for k, v in spec.items() if k != "_meta"}, self.golden_dev)
        if rep.error:
            raise ToolError(rep.error)
        return {"cases": rep.total, "passed": rep.passed, "pass_rate": round(rep.pass_rate, 4),
                "field_accuracy": rep.field_accuracy(), "failures": rep.failures[:6],
                "note": "development half of the golden set; the final check uses all cases"}

    def t_submit_draft(self, fields: list[dict[str, Any]], evt_map: dict[str, str] | None = None,
                       summary: str = "") -> dict[str, Any]:
        """Store the mapping as a draft and run the full golden set. Ends the agent's work."""
        if self.submitted is not None:
            raise ToolError("a draft was already submitted in this run")
        spec = self.build_spec(fields, evt_map)
        mv = registry.create_version(self.s, self.oem, spec, source="agent", actor=self.actor,
                                     actor_kind="agent", comment=_short(summary, 400) or "proposed by the mapping agent")
        mv, rep = registry.validate_version(self.s, self.oem, mv.version, actor=self.actor,
                                            actor_kind="agent", label=self.label)
        self.submitted = mv
        self.run.mapping_version_id = mv.id
        if self.profiles:
            mem = memory.make_memory(self.s)
            for f in fields:
                prof = self.profiles.get(f["path"])
                if prof is not None:
                    mem.add(self.oem, mv.version, f["path"], f["encoding"], f["canonical"], memory.embed(prof))
        return {"version": mv.version, "state": mv.state, "golden_cases": rep.total, "passed": rep.passed,
                "pass_rate": round(rep.pass_rate, 4), "field_accuracy": rep.field_accuracy(),
                "failures": rep.failures[:5],
                "next": ("waiting for a platform engineer to approve" if mv.state == "validated"
                         else "did not reach the required pass rate; a human has to look at it")}


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, bytes):
        return o[:400].decode("utf-8", "replace")
    if isinstance(o, float) and o != o:
        return None
    return o


def label_for(canonical: str, transforms: list[Any]) -> str:
    """The encoding label that corresponds to a field of an existing spec."""
    for lab in ENCODINGS.get(canonical, ()):
        if LABELS[lab][1] == transforms:
            return lab
    units = [t for t in transforms if isinstance(t, dict) and t.get("op") == "unit"]
    for lab in ENCODINGS.get(canonical, ()):
        if units and any(isinstance(t, dict) and t.get("from") == units[0].get("from") for t in LABELS[lab][1]):
            return lab
    if "iso8601" in transforms:
        return "ts|iso8601"
    return ENCODINGS[canonical][0]
