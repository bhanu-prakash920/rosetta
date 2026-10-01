import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Card, Code, Empty, Head, Oem, Pill, Seg, Skel } from "../components/ui";
import { api } from "../lib/api";
import { OEM_NAME, ago, compact, ms, pct } from "../lib/format";
import { useAction, useApi, useCan } from "../lib/hooks";
import type { AgentRun, Mapping, Oem as OemT, Step } from "../lib/types";
import { Version } from "./Sources";

const TOOL: Record<string, { title: string; say: (s: Step) => string }> = {
  sample_dead_letters: { title: "Collect what could not be read", say: (s) => `${s.output.samples} samples from ${s.output.tracked_devices} tracked vehicles${s.output.fragments_set_aside ? `; ${s.output.fragments_set_aside} cut-off fragments set aside` : ""}. ${s.output.evidence}.` },
  profile_fields: { title: "Profile every field", say: (s) => `${s.output.fields} fields. ${s.output.physics?.lat ? `Movement says ${s.output.physics.lat} is latitude and ${s.output.physics.lon} is longitude${s.output.physics.heading ? `, agreeing with heading ${s.output.physics.heading} (cos ${s.output.physics.heading_fit})` : ""}.` : "Not enough consecutive messages for physics checks."}` },
  search_memory: { title: "Look for fields seen before", say: (s) => `Searched ${s.output.memory_size} fields from approved mappings.` },
  suggest_mapping: { title: "Propose a mapping", say: (s) => `${s.output.fields?.length} fields chosen jointly with the Hungarian algorithm${s.output.unmapped_required?.length ? `. Still missing: ${s.output.unmapped_required.join(", ")}` : ""}.` },
  learn_event_codes: { title: "Translate event codes", say: (s) => `${Object.keys(s.output.codes ?? {}).length} codes from ${s.output.examples_used} labelled examples${s.output.inherited_from_live_mapping ? `, ${s.output.inherited_from_live_mapping} inherited from the live mapping` : ""}.` },
  validate_mapping: { title: "Test on known answers", say: (s) => `${s.output.passed} of ${s.output.cases} development cases pass (${pct(s.output.pass_rate, 1)}).` },
  submit_draft: { title: "Submit for review", say: (s) => `Version ${s.output.version}: ${s.output.passed} of ${s.output.golden_cases} golden cases pass, half of which the agent never saw. ${s.output.next}.` },
};

export default function Studio() {
  const { oem } = useParams();
  const nav = useNavigate();
  const can = useCan();
  const oems = useApi<{ items: OemT[] }>("/oems", 4000);
  const model = useApi<{ engine: string; meta: any }>("/agent/model");
  const [engine, setEngine] = useState<"auto" | "workflow" | "claude">("auto");
  const k = oem ?? oems.data?.items.find((o) => o.open_dead_letters > 0 && o.active_versions.length === 0)?.key
    ?? oems.data?.items.find((o) => o.pending_review.length > 0)?.key ?? oems.data?.items[0]?.key;
  const runs = useApi<{ items: AgentRun[] }>(k ? `/agent/runs?oem=${encodeURIComponent(k)}` : null, 4000);
  const versions = useApi<{ items: Mapping[] }>(k ? `/mappings?oem=${encodeURIComponent(k)}` : null, 4000);
  const [rid, setRid] = useState<number | null>(null);
  useEffect(() => setRid(null), [k]);
  const latest = rid ?? runs.data?.items[0]?.id ?? null;
  const run = useApi<AgentRun>(latest ? `/agent/runs/${latest}` : null);
  const act = useAction();
  const o = oems.data?.items.find((x) => x.key === k);

  async function start() {
    if (!k) return;
    const r = await act.run("agent", () => api<AgentRun>("/agent/runs", { method: "POST", json: { oem: k, engine } }),
      (x) => ({ title: x.mapping ? `Draft v${x.mapping.version} is ready for review` : "The agent could not finish", body: x.summary }));
    if (r) { setRid(r.id); runs.reload(); versions.reload(); }
  }

  return (
    <>
      <Head kicker="Mapping studio" title={<>teach it a <em>new dialect</em></>}
        lede="The agent studies messages nobody could read, proposes how each field maps to the canonical event, and proves it on known answers. It can only propose. A person approves."
        right={can.operate && <>
          <Seg value={engine} onChange={setEngine} options={[{ v: "auto", l: "Auto" }, { v: "workflow", l: "Deterministic" }, { v: "claude", l: "Claude" }]} />
          <button className="btn flame" disabled={!k || act.busy === "agent"} onClick={start}>{act.busy === "agent" ? "Working" : "Run the agent"}</button>
        </>}>
        <div className="row wrap-x" style={{ marginTop: 20, gap: 6 }}>
          {oems.data?.items.map((x) => (
            <button key={x.key} className={`btn sm ${x.key === k ? "" : "soft"}`} onClick={() => nav(`/app/studio/${x.key}`)}>
              <span className="swatch" style={{ background: `var(--oem-${x.key})` }} />{x.name}
              {x.wire_format === "protobuf" && <span className="tag">binary</span>}
              {x.open_dead_letters > 0 && <span className="pill canary">{compact(x.open_dead_letters)}</span>}
              {x.pending_review.length > 0 && <span className="pill validated">review</span>}
            </button>
          ))}
        </div>
      </Head>

      {model.data && (
        <div className="banner info" style={{ marginBottom: 16 }}>
          Engine in use when set to Auto: <b>{model.data.engine === "claude" ? "Claude, driving the tools itself" : "deterministic workflow"}</b>.
          {model.data.engine !== "claude" && " No model credentials are configured, so the tools run in a fixed order. Set ANTHROPIC_API_KEY to let Claude drive them."}
        </div>
      )}
      {o?.wire_format === "protobuf" && (
        <div className="banner info" style={{ marginBottom: 16 }}>
          {o.name} sends <b>Protobuf</b>, a binary format: messages carry field numbers, not names.{" "}
          {o.versions > 0
            ? "The agent decodes them with the schema (descriptor) registered in this source's mappings, then maps the fields like any other source."
            : "No schema is registered for this source yet, so the agent will refuse until the OEM supplies its descriptor."}
        </div>
      )}

      <div className="grid g-main" style={{ alignItems: "start", marginBottom: 40 }}>
        <div className="stack">
          {!k ? <Skel h={300} /> : !latest ? (
            <Card className="paper">
              <Empty title={`No run yet for ${OEM_NAME[k] ?? k}`}>
                {o?.open_dead_letters ? `${compact(o.open_dead_letters)} messages are parked. Run the agent to work out a mapping.` : "Nothing is parked for this source. Start a scenario on the Live page to see the agent work."}
              </Empty>
            </Card>
          ) : !run.data ? <Skel h={300} /> : (
            <Card title={<>Run {run.data.id} <Pill s={run.data.status} /></>}
              sub={<>{run.data.engine} · {ms(run.data.duration_ms)} · {run.data.steps?.length} tool calls{run.data.tokens_out ? ` · ${compact(run.data.tokens_in + run.data.tokens_out)} tokens` : ""}</>}>
              {run.data.summary && <p className="soft" style={{ marginBottom: 18 }}>{run.data.summary}</p>}
              <div className="trace">
                {run.data.steps?.map((s, i) => {
                  const t = TOOL[s.tool];
                  let say = "";
                  try { say = s.ok ? t?.say(s) ?? "" : String(s.output.error ?? "failed"); } catch { say = ""; }
                  return (
                    <div className={`step ${s.ok ? "" : "err"} ${i === (run.data!.steps!.length - 1) ? "last" : ""}`} key={s.seq}>
                      <div className="top">
                        <span className="h3">{t?.title ?? s.tool}</span>
                        <b className="muted">{s.tool}</b>
                        <span className="small muted">{ms(s.duration_ms)}</span>
                      </div>
                      <div className="why">{say}</div>
                      <details>
                        <summary>What the tool received and returned</summary>
                        {Object.keys(s.input).length > 0 && <><div className="tiny muted" style={{ margin: "10px 0 4px" }}>Input</div><Code value={s.input} light max={180} /></>}
                        <div className="tiny muted" style={{ margin: "10px 0 4px" }}>Output</div><Code value={s.output} light max={300} />
                      </details>
                    </div>
                  );
                })}
              </div>
              <p className="small muted">Every call is also written to the audit log.</p>
            </Card>
          )}
          {k && run.data?.mapping && <Version k={k} v={run.data.mapping.version} operate={can.operate} versions={versions.data?.items ?? []} onChanged={() => { versions.reload(); runs.reload(); oems.reload(); }} />}
        </div>

        <div className="stack">
          <Card title="What the agent may do" className="ink">
            <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }} className="small">
              <li>Read parked messages and known answers</li>
              <li>Choose encodings from a fixed list, never write code</li>
              <li>Test a candidate, as often as it needs</li>
              <li>Submit one draft per run</li>
            </ul>
            <div className="tiny muted" style={{ margin: "16px 0 6px" }}>What it may not</div>
            <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }} className="small">
              <li>Approve, promote or roll back. The registry refuses.</li>
              <li>See the hold-out half of the golden set</li>
              <li>Act without a record</li>
            </ul>
          </Card>
          <Card title="Earlier runs">
            {!runs.data?.items.length ? <Empty title="None" /> : (
              <table className="tbl"><tbody>
                {runs.data.items.slice(0, 8).map((r) => (
                  <tr key={r.id} className={`click ${r.id === latest ? "sel" : ""}`} onClick={() => setRid(r.id)}>
                    <td><b>#{r.id}</b></td><td><Pill s={r.status} /></td>
                    <td className="small">{r.mapping ? `v${r.mapping.version}` : "–"}</td>
                    <td className="small muted right">{ago(r.started_at)}</td>
                  </tr>
                ))}
              </tbody></table>
            )}
          </Card>
          {k && <Card title="Source"><div className="row between"><Oem k={k} name={OEM_NAME[k]} /><Link className="linkish small" to={`/app/sources/${k}`}>All versions</Link></div></Card>}
        </div>
      </div>
    </>
  );
}
