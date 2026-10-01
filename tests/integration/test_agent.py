"""The mapping agent on the two demo cases, its repair loop, and the model-driven engine
against a stub of the Claude client."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from rosetta.agent.toolbox import Toolbox, label_for
from rosetta.db.models import AgentRun, AgentStep, AuditLog
from rosetta.db.session import session_scope
from rosetta.services import agent_service, registry
from rosetta.simulator.dialects import SPEC_HELIX, SPEC_KAIZEN, SPEC_PACIFICA_V2
from tests.support import ALL_SOURCES, World


@pytest.fixture(scope="module")
def parked():
    """A world where helix is unknown and 35% of pacifica runs the new firmware."""
    w = World(vehicles=6000, sources=ALL_SOURCES, mode="realistic")
    w.sim.s.drift_pct = 35
    for _ in range(45):
        w.tick()
        w.pump(rounds=1)
    yield w
    w.close()


def truth(spec: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {c: (f["path"], label_for(c, f.get("transforms") or [])) for c, f in spec["fields"].items()}


@pytest.mark.parametrize("source,spec,label", [("helix", SPEC_HELIX, "helix"),
                                               ("pacifica", SPEC_PACIFICA_V2, "pacifica_v2")])
def test_workflow_maps_the_demo_cases(parked: World, source, spec, label):
    with session_scope() as s:
        run = agent_service.run_agent(s, source, requested_by="test", engine="workflow")
        res = run.__dict__["_result"]
        assert run.status == "awaiting_approval", run.summary
        assert res["result"]["state"] == "validated"
        assert res["result"]["passed"] == res["result"]["golden_cases"] == 200
        final = {f["canonical"]: (f["path"], f["encoding"]) for f in res["final"]}
        assert final == truth(spec)
        steps = s.execute(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)).scalars().all()
        assert [st.tool for st in steps][:3] == ["sample_dead_letters", "profile_fields", "suggest_mapping"]
        assert steps[-1].tool == "submit_draft" and all(st.ok for st in steps)
        assert steps[0].output["golden_label"] == label          # it picked the right certification kit
        assert steps[0].output["golden_cases_visible_to_agent"] == 100
        # every tool call is in the audit log, attributed to the agent
        n = s.execute(select(AuditLog).where(AuditLog.actor_kind == "agent",
                                             AuditLog.resource == f"agent_run/{run.id}")).scalars().all()
        assert len([a for a in n if a.action.startswith("agent.tool.")]) == len(steps)


def test_physics_identifies_units_without_name_hints(parked: World):
    with session_scope() as s:
        run = AgentRun(oem_id=registry.get_oem(s, "helix").id)
        s.add(run)
        s.flush()
        tb = Toolbox(s, "helix", run)
        tb.call("sample_dead_letters")
        prof = tb.call("profile_fields")
        assert prof["physics"]["lat"] == "ort.breite" and prof["physics"]["lon"] == "ort.laenge"
        assert prof["physics"]["heading_fit"] > 0.8
        by = {p["path"]: p for p in prof["profile"]}
        ratio = 10 ** (by["fahrt.v"]["physics"]["speed_ratio_log"] * 4)
        assert ratio == pytest.approx(1 / 3.6, rel=0.05)              # metres per second
        per_km = 10 ** (by["fahrt.strecke"]["physics"]["dist_ratio_log"] * 4)
        assert per_km == pytest.approx(1000, rel=0.05)                # metres
        s.rollback()


def test_repair_fixes_a_wrong_first_proposal(parked: World, monkeypatch):
    """Sabotage the classifier's answer for two fields. Golden feedback must repair them."""
    from rosetta.agent import workflow

    real = Toolbox.t_suggest_mapping

    def sabotaged(self):
        out = real(self)
        for f in out["fields"]:
            if f["canonical"] == "speed_kmh":
                f["encoding"] = "speed|mph"                       # wrong unit
            if f["canonical"] == "soc_pct":
                f["path"] = "tank.stand"                          # wrong field
            if f["canonical"] == "fuel_pct":
                f["path"] = "akku.ladung"
        return out

    monkeypatch.setattr(Toolbox, "t_suggest_mapping", sabotaged)
    with session_scope() as s:
        run = agent_service.run_agent(s, "helix", requested_by="test", engine="workflow")
        res = run.__dict__["_result"]
        assert res["rounds"] >= 1
        assert res["result"]["state"] == "validated" and res["result"]["pass_rate"] == 1.0
        final = {f["canonical"]: (f["path"], f["encoding"]) for f in res["final"]}
        assert final == truth(SPEC_HELIX)
        repaired = [f for f in res["final"] if f["rationale"].startswith("repaired")]
        assert {f["canonical"] for f in repaired} >= {"speed_kmh"}
        assert workflow.MAX_ROUNDS >= res["rounds"]


def test_toolbox_refuses_what_is_outside_the_whitelist(parked: World):
    with session_scope() as s:
        run = AgentRun(oem_id=registry.get_oem(s, "helix").id)
        s.add(run)
        s.flush()
        tb = Toolbox(s, "helix", run)
        assert "error" in tb.call("validate_mapping", fields=[])           # nothing sampled yet
        tb.call("sample_dead_letters")
        tb.call("profile_fields")
        tb.call("learn_event_codes", path="ereignis")
        good = [{"canonical": c, "path": p, "encoding": e} for c, (p, e) in truth(SPEC_HELIX).items()]
        for bad in (
            [{**good[0], "canonical": "password"}],
            [{**good[0], "encoding": "speed|mph"}],                        # encoding of another field
            [{**good[0], "path": "__class__.__init__"}],                   # a path that is not in the samples
            [good[0], good[0]],                                            # mapped twice
            [{**good[0], "encoding": "os.system('id')"}],
        ):
            assert "error" in tb.call("validate_mapping", fields=bad)
        assert "error" in tb.call("drop_database")                         # no such tool
        assert "error" in tb.call("submit_draft", fields=good, evt_map={"X": "NOT_AN_EVENT"})
        assert tb.submitted is None
        out = tb.call("submit_draft", fields=good)
        assert out["state"] == "validated"
        assert "error" in tb.call("submit_draft", fields=good)             # one draft per run
        s.rollback()


def test_step_limit_stops_a_runaway_agent(parked: World):
    from rosetta.agent import toolbox

    with session_scope() as s:
        run = AgentRun(oem_id=registry.get_oem(s, "helix").id)
        s.add(run)
        s.flush()
        tb = Toolbox(s, "helix", run)
        outs = [tb.call("sample_dead_letters", limit=20) for _ in range(toolbox.MAX_STEPS + 3)]
        assert all("error" not in o for o in outs[: toolbox.MAX_STEPS])
        assert all("step limit" in o["error"] for o in outs[toolbox.MAX_STEPS:])
        s.rollback()


def test_binary_format_is_reported_not_guessed(parked: World):
    from rosetta.agent import formats

    with pytest.raises(formats.UnsupportedFormat, match="descriptor"):
        formats.detect([bytes([i % 256 for i in range(40, 90)]) + b"\x00\x01\xfe"] * 20)


def test_workflow_maps_protobuf_with_the_registered_schema(parked: World):
    with session_scope() as s:
        run = agent_service.run_agent(s, "kaizen", requested_by="test", engine="workflow")
        res = run.__dict__["_result"]
        assert run.status == "awaiting_approval", run.summary
        assert res["result"]["passed"] == res["result"]["golden_cases"] == 200
        assert {f["canonical"]: (f["path"], f["encoding"]) for f in res["final"]} == truth(SPEC_KAIZEN)
        first = s.execute(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.seq == 1)).scalar_one()
        assert first.output["wire_format"]["type"] == "protobuf"
        assert "registered in kaizen mapping v" in first.output["evidence"]


def test_cut_off_fragments_are_set_aside_before_learning(parked: World):
    from rosetta.engine.decoders import build_message_class
    from rosetta.simulator.dialects import KAIZEN_DESCRIPTOR_B64

    cls = build_message_class(KAIZEN_DESCRIPTOR_B64, "kaizen.v1.Telemetry")
    with session_scope() as s:
        run = AgentRun(oem_id=registry.get_oem(s, "kaizen").id)
        s.add(run)
        s.flush()
        tb = Toolbox(s, "kaizen", run)
        golden = [p for p, _ in registry.golden_cases(s, "kaizen")[:20]]
        frags = [cls(vin="JKZGT4B74TA014884", time_us=1_790_000_000_000_000 + i, counter=i).SerializeToString()
                 for i in range(1, 31)]
        whole = golden[:5]                    # a complete message that was parked is still evidence
        tb.decoder = SPEC_KAIZEN["decoder"]
        tb.payloads = frags + whole + golden
        tb.devices = [f"d{i}" for i in range(len(tb.payloads))]
        assert tb._set_aside_fragments(len(frags) + len(whole)) == len(frags)
        assert tb.payloads == whole + golden


def test_a_whole_sub_message_is_never_an_event_code(parked: World):
    with session_scope() as s:
        run = AgentRun(oem_id=registry.get_oem(s, "kaizen").id)
        s.add(run)
        s.flush()
        tb = Toolbox(s, "kaizen", run)
        tb.call("sample_dead_letters")
        assert tb.event_table("pos")["examples_used"] == 0
        assert tb.event_table("event")["examples_used"] > 0


def test_repair_finds_a_required_field_the_proposal_missed(parked: World, monkeypatch):
    original = Toolbox.t_suggest_mapping

    def without_seq(self):
        out = original(self)
        out["fields"] = [f for f in out["fields"] if f["canonical"] != "seq"]
        return out

    monkeypatch.setattr(Toolbox, "t_suggest_mapping", without_seq)
    with session_scope() as s:
        run = agent_service.run_agent(s, "kaizen", requested_by="test", engine="workflow")
        res = run.__dict__["_result"]
        assert run.status == "awaiting_approval", run.summary
        assert {f["canonical"]: (f["path"], f["encoding"]) for f in res["final"]} == truth(SPEC_KAIZEN)
        tools = [st.tool for st in s.execute(select(AgentStep).where(AgentStep.run_id == run.id)
                                             .order_by(AgentStep.seq)).scalars()]
        assert tools.count("validate_mapping") >= 2       # rejected for the missing field, then repaired


def test_a_refused_format_is_recorded_as_a_failed_step(parked: World, monkeypatch):
    monkeypatch.setattr(Toolbox, "_registered_binary_decoders", lambda self: [])
    with session_scope() as s:
        run = agent_service.run_agent(s, "kaizen", requested_by="test", engine="workflow")
        assert run.status == "failed" and "descriptor" in run.summary
        steps = s.execute(select(AgentStep).where(AgentStep.run_id == run.id)).scalars().all()
        assert [(st.tool, st.ok) for st in steps] == [("sample_dead_letters", False)]
        assert "descriptor" in steps[0].output["error"]
        logged = s.execute(select(AuditLog).where(AuditLog.resource == f"agent_run/{run.id}",
                                                  AuditLog.action == "agent.tool.sample_dead_letters")).scalar_one()
        assert logged.outcome == "error"


# ----------------------------------------------------- the model-driven engine
class StubClaude:
    """Stands in for anthropic.Anthropic(). Replays a scripted conversation and records
    what it was sent. It checks the request shape the real API requires."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        assert kw["model"] and kw["max_tokens"] > 0
        assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
        assert "thinking" not in kw and "temperature" not in kw and "tool_choice" not in kw
        assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in kw["tools"])
        msgs = kw["messages"]
        assert msgs[0]["role"] == "user"
        for a, b in zip(msgs, msgs[1:]):
            if a["role"] == "assistant" and any(x.type == "tool_use" for x in a["content"]):
                ids = {x.id for x in a["content"] if x.type == "tool_use"}
                assert b["role"] == "user" and {r["tool_use_id"] for r in b["content"]} == ids
        self.calls.append({"n": len(msgs), "last": msgs[-1]})
        step = self.script.pop(0)
        return step(msgs) if callable(step) else step


def use(name, args, i):
    return SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=args)


def reply(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, stop_details=None,
                           usage=SimpleNamespace(input_tokens=1200, output_tokens=300))


def test_claude_engine_drives_the_tools(parked: World):
    import json

    def submit_from_suggestion(msgs):
        sug = json.loads(msgs[-1]["content"][0]["content"])
        fields = [{"canonical": f["canonical"], "path": f["path"], "encoding": f["encoding"],
                   "confidence": f["confidence"], "rationale": "classifier and physics agree"}
                  for f in sug["fields"]]
        return reply(SimpleNamespace(type="thinking", thinking="", signature="sig"),
                     use("validate_mapping", {"fields": fields, "event_codes": []}, 4),
                     use("submit_draft", {"fields": fields, "event_codes": [], "summary": "all fields mapped"}, 5))

    stub = StubClaude([
        reply(use("sample_dead_letters", {"limit": 900}, 1), use("profile_fields", {}, 2)),
        reply(use("learn_event_codes", {"path": "ereignis"}, 3), use("suggest_mapping", {}, 6)),
        lambda msgs: submit_from_suggestion([{"content": [msgs[-1]["content"][1]]}]),
        reply(SimpleNamespace(type="text", text="Mapped 14 fields. Review the event codes first."), stop="end_turn"),
    ])
    with session_scope() as s:
        run = agent_service.run_agent(s, "helix", requested_by="test", engine="claude", client=stub)
        assert run.status == "awaiting_approval", run.summary
        assert run.engine.startswith("claude") and run.tokens_in == 4800 and run.tokens_out == 1200
        assert "Review the event codes" in run.summary
        tools = [st.tool for st in s.execute(select(AgentStep).where(AgentStep.run_id == run.id)
                                             .order_by(AgentStep.seq)).scalars()]
        assert tools == ["sample_dead_letters", "profile_fields", "learn_event_codes", "suggest_mapping",
                         "validate_mapping", "submit_draft"]
        assert len(stub.calls) == 4
        # thinking blocks were passed back unchanged, history was only ever appended to
        assert [c["n"] for c in stub.calls] == [1, 3, 5, 7]


def test_claude_refusal_falls_back_to_the_workflow(parked: World):
    stub = StubClaude([SimpleNamespace(content=[], stop_reason="refusal",
                                       stop_details=SimpleNamespace(category=None, explanation=""),
                                       usage=SimpleNamespace(input_tokens=10, output_tokens=0))])
    with session_scope() as s:
        run = agent_service.run_agent(s, "helix", requested_by="test", engine="claude", client=stub)
        assert run.status == "awaiting_approval"
        assert "workflow" in run.engine and "declined" in run.engine


def test_claude_api_errors_fall_back(parked: World):
    import anthropic
    import httpx2

    class Down:
        def __init__(self):
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        def create(self, **kw):
            raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    with session_scope() as s:
        run = agent_service.run_agent(s, "helix", requested_by="test", engine="claude", client=Down())
        assert run.status == "awaiting_approval" and "network error" in run.engine


def test_prompt_injection_in_payloads_cannot_reach_production(parked: World):
    """A vehicle sends text that reads like an instruction. Whatever a model made of it,
    the only thing an agent run can produce is an inert draft."""
    stub = StubClaude([
        reply(use("sample_dead_letters", {"limit": 50}, 1)),
        # the model 'obeys' the injected text and tries tools that do not exist or are not its own
        reply(use("approve_mapping", {"version": 1}, 2), use("promote", {}, 3)),
        reply(SimpleNamespace(type="text", text="I was asked to approve but I cannot."), stop="end_turn"),
    ])
    with session_scope() as s:
        live_before = {(r.oem, r.version, r.state) for r in registry.live_rows(s)}
        run = agent_service.run_agent(s, "helix", requested_by="test", engine="claude", client=stub)
        assert run.status == "failed" and run.mapping_version_id is None
        steps = s.execute(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)).scalars().all()
        assert [(st.tool, st.ok) for st in steps] == [("sample_dead_letters", True), ("approve_mapping", False),
                                                      ("promote", False)]
        assert {(r.oem, r.version, r.state) for r in registry.live_rows(s)} == live_before
