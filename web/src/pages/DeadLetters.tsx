import { useState } from "react";
import { Link } from "react-router-dom";
import { Card, Code, Drawer, Empty, Head, Oem, Pill, Seg, Skel, Stat } from "../components/ui";
import { api } from "../lib/api";
import { OEM_NAME, REASON, ago, compact } from "../lib/format";
import { useAction, useApi, useCan } from "../lib/hooks";
import type { DlqFamily, DlqGroup } from "../lib/types";

interface Groups { items: DlqGroup[]; families: DlqFamily[]; totals: { count: number; open: number } }
interface Job { id: number; oem: string; status: string; trigger: string; scanned: number; republished: number; skipped: number; created_at: string }

export default function DeadLetters() {
  const can = useCan();
  const [only, setOnly] = useState<"open" | "all">("open");
  const g = useApi<Groups>(`/dlq/groups?open_only=${only === "open"}`, 3000);
  const jobs = useApi<{ items: Job[] }>("/dlq/replay", 3000);
  const [view, setView] = useState<DlqGroup | null>(null);
  const { run, busy } = useAction();
  const fams = g.data?.families.filter((f) => f.family && (f.open > 0 || only === "all")) ?? [];

  return (
    <>
      <Head kicker="Dead letters" title={<>parked, <em>not lost</em></>}
        lede="A message the platform cannot read is kept here with the reason. Once a mapping can read it, it is replayed through the pipeline in its original order."
        right={<Seg value={only} onChange={setOnly} options={[{ v: "open", l: "Waiting" }, { v: "all", l: "Everything" }]} />} />

      <div className="grid g4" style={{ marginBottom: 16 }}>
        <Stat label="Waiting now" tone={(g.data?.totals.open ?? 0) > 0 ? "warn" : "ok"} value={compact(g.data?.totals.open)} sub="Messages no live mapping can read" />
        <Stat label="Ever parked" value={compact(g.data?.totals.count)} sub="In the groups shown" />
        <Stat label="Unreadable formats" value={fams.filter((f) => f.mappable).length} sub="Families a new mapping could read" />
        <Stat className="paper" label="Replay jobs" value={jobs.data?.items.length ?? 0} sub={jobs.data?.items[0] ? `last one ${ago(jobs.data.items[0].created_at)}` : "none yet"} />
      </div>

      <div className="grid g-main" style={{ marginBottom: 16, alignItems: "start" }}>
        <Card title="Families" sub="Shapes that differ only by optional fields are one format, joined with union-find. Damaged messages are kept as evidence, no mapping can read them.">
          {!g.data ? <Skel h={160} /> : fams.length === 0 ? <Empty title="Nothing is waiting">Every message that arrived could be read, or was replayed.</Empty> : fams.map((f) => (
            <div key={f.family} className="row between wrap-x" style={{ padding: "14px 0", borderBottom: "1px solid var(--line-2)" }}>
              <div className="grow">
                <div className="row wrap-x"><Oem k={f.oem} name={OEM_NAME[f.oem]} /><span className="tag">{f.mappable ? f.family : "damaged in transit"}</span><span className="small muted">{f.groups} shape{f.groups > 1 ? "s" : ""}</span></div>
                <div className="row wrap-x small" style={{ marginTop: 6, gap: 6 }}>
                  {Object.entries(f.reasons).map(([r, n]) => <Pill key={r} s={r === "NO_ADAPTER" || r === "SCHEMA_MISMATCH" ? "canary" : "draft"}>{r} {compact(n)}</Pill>)}
                </div>
              </div>
              <div className="right"><b style={{ fontSize: 22, letterSpacing: "-0.03em" }}>{compact(f.open)}</b><div className="small muted">waiting</div></div>
              {can.operate && f.mappable && (
                <div className="row">
                  <Link className="btn sm" to={`/app/studio/${f.oem}`}>Map it</Link>
                  <button className="btn ghost sm" disabled={busy === f.family || f.open === 0}
                    onClick={() => run(f.family, () => api("/dlq/replay", { method: "POST", json: { oem: f.oem } }), () => ({ title: "Replay queued", body: `${OEM_NAME[f.oem] ?? f.oem}: parked messages are going through the pipeline again.` }))}>
                    Replay
                  </button>
                </div>
              )}
            </div>
          ))}
        </Card>
        <Card title="Replay jobs">
          {!jobs.data?.items.length ? <Empty title="No replay has run">Approving a mapping starts one automatically.</Empty> : (
            <table className="tbl">
              <thead><tr><th>Source</th><th>Trigger</th><th className="num">Replayed</th><th>Status</th></tr></thead>
              <tbody>{jobs.data.items.slice(0, 8).map((j) => (
                <tr key={j.id}><td><Oem k={j.oem} /></td><td className="small muted">{j.trigger}</td><td className="num">{compact(j.republished)}</td><td><Pill s={j.status} /></td></tr>
              ))}</tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="Groups" sub="Source, reason, field and payload shape" className="pad0" >
        <div className="scroll-x scroll-y">
          <table className="tbl">
            <thead><tr><th>Source</th><th>Reason</th><th>Field</th><th>Family</th><th className="num">Waiting</th><th className="num">Total</th><th>Last seen</th></tr></thead>
            <tbody>
              {g.data?.items.map((r) => (
                <tr key={r.id} className="click" onClick={() => setView(r)}>
                  <td><Oem k={r.oem} /></td>
                  <td><b className="mono small">{r.reason}</b><div className="small muted">{REASON[r.reason]}</div></td>
                  <td className="mono small">{r.field || "–"}</td>
                  <td><span className="tag">{r.family || r.shape}</span></td>
                  <td className="num"><b>{compact(r.open)}</b></td>
                  <td className="num muted">{compact(r.count)}</td>
                  <td className="small muted nowrap">{ago(r.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {g.data && g.data.items.length === 0 && <Empty title="No groups" />}
        </div>
      </Card>
      <div style={{ height: 40 }} />
      {view && <Samples g={view} onClose={() => setView(null)} />}
    </>
  );
}

function Samples({ g, onClose }: { g: DlqGroup; onClose: () => void }) {
  const s = useApi<{ items: { device: string; ts: number; text: string; binary: boolean; bytes: number }[] }>(`/dlq/groups/${g.id}/samples?limit=8`);
  return (
    <Drawer onClose={onClose} label="Dead-letter group">
      <h2 className="h2">{OEM_NAME[g.oem] ?? g.oem}</h2>
      <div className="row wrap-x" style={{ margin: "10px 0 18px" }}>
        <Pill s="failed">{g.reason}</Pill>{g.field && <span className="tag">{g.field}</span>}<span className="tag">{g.shape}</span>
      </div>
      <p className="soft">{REASON[g.reason]}{g.detail ? `. ${g.detail}` : ""}</p>
      <div className="tiny muted" style={{ margin: "22px 0 8px" }}>Sample payloads, exactly as they arrived</div>
      {!s.data ? <Skel /> : s.data.items.length === 0 ? <Code value={g.sample.text} /> : s.data.items.map((x, i) => (
        <div key={i} style={{ marginBottom: 12 }}>
          <div className="row between small muted" style={{ marginBottom: 4 }}><span className="mono">{x.device || "unknown device"}</span><span>{x.bytes} bytes{x.binary ? " · binary, shown as hex" : ""} · {ago(x.ts)}</span></div>
          <Code value={x.text} max={200} />
        </div>
      ))}
    </Drawer>
  );
}
