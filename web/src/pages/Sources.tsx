import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Card, Code, Empty, Head, Meter, Modal, Oem, Pill, Skel } from "../components/ui";
import { api } from "../lib/api";
import { OEM_NAME, REASON, ago, compact, int, pct, transformLabel } from "../lib/format";
import { useAction, useApi, useCan } from "../lib/hooks";
import type { Mapping, Oem as OemT } from "../lib/types";

export default function Sources() {
  const { oem } = useParams();
  const list = useApi<{ items: OemT[] }>("/oems", 3000);
  const can = useCan();
  if (oem) return <Detail k={oem} oems={list.data?.items ?? []} staff={can.staff} operate={can.operate} />;
  return (
    <>
      <Head kicker="Sources" title={<>six makers, <em>six dialects</em></>}
        lede="A source is one car maker's data feed. Each has its own wire format, field names and units, and one or more mapping versions that translate it." />
      {!list.data ? <Skel h={300} /> : (
        <div className="grid g3" style={{ marginBottom: 40 }}>
          {list.data.items.map((o) => {
            const t = (o.live?.ok_per_s ?? 0) + (o.live?.failed_per_s ?? 0);
            const ok = t ? (o.live?.ok_per_s ?? 0) / t : null;
            return (
              <Link key={o.key} to={`/app/sources/${o.key}`} className="card" style={{ display: "block" }}>
                <div className="row between" style={{ marginBottom: 18 }}>
                  <Oem k={o.key} name={o.name} />
                  <Pill s={o.status} />
                </div>
                <div className="stat"><div className="v">{int(o.live?.ok_per_s ?? 0)}<small>events / s</small></div></div>
                <div style={{ margin: "14px 0 8px" }}><Meter v={ok ?? 0} tone={ok == null ? "" : ok > 0.99 ? "leaf" : ok > 0.5 ? "amber" : "flame"} /></div>
                <div className="row between small muted">
                  <span>{ok == null ? "silent" : pct(ok, 1) + " readable"}</span>
                  <span>{compact(o.vehicles)} vehicles</span>
                </div>
                <div className="row wrap-x" style={{ marginTop: 14, gap: 6 }}>
                  <span className="tag">{o.wire_format}</span>
                  {o.active_versions.map((v) => <Pill key={v} s="active">v{v} live</Pill>)}
                  {o.canary && <Pill s="canary">v{o.canary.version} canary {o.canary.pct}%</Pill>}
                  {o.pending_review.map((v) => <Pill key={v} s="validated">v{v} awaits review</Pill>)}
                  {o.open_dead_letters > 0 && <Pill s="failed">{compact(o.open_dead_letters)} parked</Pill>}
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </>
  );
}

function Detail({ k, oems, staff, operate }: { k: string; oems: OemT[]; staff: boolean; operate: boolean }) {
  const o = oems.find((x) => x.key === k);
  const nav = useNavigate();
  const versions = useApi<{ items: Mapping[] }>(staff ? `/mappings?oem=${encodeURIComponent(k)}` : null, 4000);
  const [sel, setSel] = useState<number | null>(null);
  useEffect(() => setSel(null), [k]);
  const items = versions.data?.items ?? [];
  const chosen = sel ?? items.find((v) => v.state === "validated" || v.state === "canary")?.version ?? items.find((v) => v.state === "active")?.version ?? items[0]?.version ?? null;

  return (
    <>
      <Head kicker={`Source · ${o?.wire_format ?? ""}`} title={<>{(OEM_NAME[k] ?? k).toLowerCase()}</>}
        right={<>
          <button className="btn ghost" onClick={() => nav("/app/sources")}>All sources</button>
          {operate && <Link className="btn" to={`/app/studio/${k}`}>Open in mapping studio</Link>}
        </>}>
        <div className="row wrap-x" style={{ marginTop: 18, gap: 8 }}>
          {o && <Pill s={o.status} />}
          <span className="tag">VIN prefix {o?.wmi}</span>
          <span className="tag">{compact(o?.vehicles)} vehicles</span>
          <span className="tag">{int(o?.live?.ok_per_s ?? 0)} events / s</span>
        </div>
      </Head>
      {!staff ? <Empty title="Mapping details are for platform staff">Your role sees the health of each source, not how it is translated.</Empty> : (
        <div className="grid g-main" style={{ marginBottom: 40, alignItems: "start" }}>
          <div className="stack">
            {chosen != null ? <Version k={k} v={chosen} operate={operate} versions={items} onChanged={versions.reload} /> : <Card><Empty title="No mapping yet">Messages from this source are being parked. Ask the agent for a mapping in the studio.</Empty></Card>}
          </div>
          <div className="stack">
            <Card title="Versions" sub="Newest first">
              {items.length === 0 ? <Empty title="None" /> : (
                <table className="tbl"><tbody>
                  {items.map((v) => (
                    <tr key={v.version} className={`click ${v.version === chosen ? "sel" : ""}`} onClick={() => setSel(v.version)}>
                      <td><b>v{v.version}</b></td>
                      <td><Pill s={v.state}>{v.state}{v.state === "canary" ? ` ${v.canary_pct}%` : ""}</Pill></td>
                      <td><Pill s={v.source === "agent" ? "agent" : "draft"}>{v.source}</Pill></td>
                      <td className="small muted right">{ago(v.created_at)}</td>
                    </tr>
                  ))}
                </tbody></table>
              )}
            </Card>
            {o?.live && Object.keys(o.live.reasons).length > 0 && (
              <Card title="Why messages were parked">
                {Object.entries(o.live.reasons).sort((a, b) => b[1] - a[1]).map(([r, n]) => (
                  <div key={r} className="row between" style={{ padding: "8px 0", borderBottom: "1px solid var(--line-2)" }}>
                    <div><b className="mono small">{r}</b><div className="small muted">{REASON[r]}</div></div>
                    <b>{compact(n)}</b>
                  </div>
                ))}
              </Card>
            )}
          </div>
        </div>
      )}
    </>
  );
}

const NEXT: Record<string, { action: string; label: string; tone: string; text: string }[]> = {
  validated: [
    { action: "approve", label: "Approve", tone: "flame", text: "Release to a share of vehicles. Parked messages are replayed." },
    { action: "reject", label: "Reject", tone: "ghost", text: "Discard this proposal." },
  ],
  canary: [
    { action: "promote", label: "Promote to live", tone: "flame", text: "Make it a live version for every vehicle." },
    { action: "rollback", label: "Roll back", tone: "ghost", text: "Take it out of traffic. Live versions are untouched." },
  ],
  active: [{ action: "retire", label: "Retire", tone: "ghost", text: "Stop using this version." }],
  draft: [{ action: "reject", label: "Reject", tone: "ghost", text: "Discard this draft." }],
};

export function Version({ k, v, operate, versions, onChanged }: { k: string; v: number; operate: boolean; versions: Mapping[]; onChanged: () => void }) {
  const m = useApi<Mapping>(`/mappings/${encodeURIComponent(k)}/${v}`, 5000);
  const prev = versions.filter((x) => x.version < v && x.state !== "rejected").sort((a, b) => b.version - a.version)[0];
  const diff = useApi<{ fields: { canonical: string; change: string; before: any; after: any }[] }>(prev ? `/mappings/${encodeURIComponent(k)}/${v}/diff?against=${prev.version}` : null);
  const { run, busy } = useAction();
  const [ask, setAsk] = useState<{ action: string; label: string; text: string } | null>(null);
  const [comment, setComment] = useState("");
  const [canary, setCanary] = useState(100);
  const [shadow, setShadow] = useState<any>(null);
  useEffect(() => setShadow(null), [k, v]);
  if (!m.data) return <Skel h={400} />;
  const d = m.data;
  const vr = d.validation_runs?.[0];
  const hasLive = versions.some((x) => x.state === "active");

  async function act() {
    if (!ask) return;
    const a = ask;
    setAsk(null);
    await run(a.action, () => api(`/mappings/${encodeURIComponent(k)}/${v}/actions/${a.action}`, { method: "POST", json: { comment, ...(a.action === "approve" ? { canary_pct: canary } : {}) } }),
      (r: any) => ({ title: `v${v} is now ${r.state}`, body: r.replay_job ? "Workers reload within a second. Parked messages are being replayed." : "Workers reload within a second." }));
    setComment("");
    m.reload();
    onChanged();
  }

  return (
    <>
      <Card title={<>Version {d.version} <Pill s={d.state}>{d.state}{d.state === "canary" ? ` ${d.canary_pct}%` : ""}</Pill></>}
        sub={<>{d.source === "agent" ? "Proposed by the mapping agent" : d.source === "seed" ? "Known at platform start" : "Written by an engineer"} · {ago(d.created_at)} · hash <span className="mono">{d.spec_hash}</span></>}
        right={operate && (
          <div className="row wrap-x">
            {(d.state === "validated" || d.state === "draft") && (
              <button className="btn soft sm" disabled={busy === "shadow"} onClick={async () => setShadow(await run("shadow", () => api(`/mappings/${encodeURIComponent(k)}/${v}/shadow`, { method: "POST", json: { max_records: 20000 } })))}>
                {busy === "shadow" ? "Running" : "Try on parked traffic"}
              </button>
            )}
            {(NEXT[d.state] ?? []).map((n) => (
              <button key={n.action} className={`btn ${n.tone} sm`} disabled={!!busy} onClick={() => { setCanary(hasLive ? 10 : 100); setAsk(n); }}>{n.label}</button>
            ))}
          </div>
        )}>
        {d.state === "validated" && <div className="banner info" style={{ marginBottom: 14 }}>This version passed its tests and waits for a person. Nothing reads it until someone approves.</div>}
        {d.canary_health && <div className="banner ok" style={{ marginBottom: 14 }}>Canary, last {d.canary_health.window_s} s: {compact(d.canary_health.ok)} translated, {compact(d.canary_health.failed)} invalid{d.canary_health.failure_rate != null ? ` (${pct(d.canary_health.failure_rate, 2)})` : ""}. It is rolled back automatically above 5%.</div>}
        {shadow && (
          <div className={`banner ${shadow.would_fail === 0 ? "ok" : "info"}`} style={{ marginBottom: 14 }}>
            Dry run on {compact(shadow.parked_messages_read)} messages parked for their format: {compact(shadow.would_succeed)} would translate, {compact(shadow.would_fail)} would not
            {shadow.first_error ? `. First problem: ${shadow.first_error.reason} ${shadow.first_error.field}` : ""}.
            {shadow.damaged_in_transit ? ` ${compact(shadow.damaged_in_transit)} damaged messages were set aside.` : ""} Nothing was written.
          </div>
        )}
        <div className="tiny muted" style={{ marginBottom: 4 }}>Wire format</div>
        <div className="row wrap-x" style={{ marginBottom: 16 }}>
          {Object.entries(d.decoder ?? {}).map(([a, b]) => <span key={a} className="tag">{a}: {typeof b === "object" ? JSON.stringify(b) : String(b)}</span>)}
        </div>
        <div className="row small muted" style={{ padding: "0 0 6px", borderBottom: "1px solid var(--line)" }}>
          <span style={{ flex: 1.1 }}>SOURCE FIELD</span><span style={{ width: 20 }} /><span style={{ flex: 1 }}>CANONICAL · BAR SHOWS CLASSIFIER CONFIDENCE</span><span style={{ flex: 0.5 }} className="right">TESTED</span>
        </div>
        {d.field_map?.map((f) => {
          const acc = vr?.field_accuracy?.[f.canonical];
          return (
            <div className="map-line" key={f.canonical} title={f.rationale ?? undefined}>
              <div className="clip">
                <span className="mono">{f.path}</span>
                <div className="small muted clip">{f.transforms.length ? f.transforms.map(transformLabel).join(", ") : "as is"}</div>
              </div>
              <span className="arrow">→</span>
              <div><b>{f.canonical}</b>{f.unit && <span className="muted small"> {f.unit}</span>}{f.required && <span className="muted small"> · required</span>}
                {f.confidence != null && <div style={{ marginTop: 4 }}><Meter v={f.confidence} tone={f.confidence > 0.8 ? "leaf" : f.confidence > 0.5 ? "amber" : "flame"} /></div>}
              </div>
              <div className="right small">{acc == null ? <span className="muted">–</span> : <b style={{ color: acc >= 0.99 ? "var(--leaf-ink)" : "var(--flame-ink)" }}>{pct(acc, 0)}</b>}</div>
            </div>
          );
        })}
      </Card>

      {vr && (
        <Card title="Golden set" sub={`Known payloads with known answers · ${ago(vr.ts)}`} right={<Pill s={vr.passed === vr.total ? "ok" : "failed"}>{vr.passed} / {vr.total} passed</Pill>}>
          <Meter v={vr.total ? vr.passed / vr.total : 0} tone={vr.passed / Math.max(1, vr.total) >= 0.99 ? "leaf" : "flame"} />
          {vr.failures.length > 0 && <div style={{ marginTop: 14 }}><Code value={vr.failures} light max={220} /></div>}
        </Card>
      )}

      {prev && diff.data && (
        <Card title={`What changed since v${prev.version}`} sub={diff.data.fields.length ? `${diff.data.fields.length} field${diff.data.fields.length > 1 ? "s" : ""}` : "Nothing"}>
          {diff.data.fields.length === 0 ? <Empty title="Identical field map" /> : diff.data.fields.map((f) => (
            <div key={f.canonical} style={{ padding: "10px 0", borderBottom: "1px solid var(--line-2)" }}>
              <div className="row between"><b>{f.canonical}</b><Pill s={f.change === "removed" ? "failed" : f.change === "added" ? "ok" : "canary"}>{f.change}</Pill></div>
              <div className="small" style={{ marginTop: 4 }}>
                {f.before && <div className="muted"><s className="mono">{f.before.path}</s> {(f.before.transforms ?? []).map(transformLabel).join(", ")}</div>}
                {f.after && <div><span className="mono">{f.after.path}</span> {(f.after.transforms ?? []).map(transformLabel).join(", ") || "as is"}</div>}
              </div>
            </div>
          ))}
        </Card>
      )}

      <Card title="History" sub="Every change, who made it and why">
        <div className="trace">
          {d.actions?.map((a, i) => (
            <div className={`step ${i === (d.actions?.length ?? 0) - 1 ? "last" : ""}`} key={i}>
              <div className="row wrap-x" style={{ gap: 8 }}>
                <b>{a.action}</b><Pill s={a.actor_kind === "agent" ? "agent" : "draft"}>{a.actor_kind}</Pill>
                {a.from && <span className="small muted">{a.from} → {a.to}</span>}
                <span className="small muted">{ago(a.ts)}</span>
              </div>
              {a.comment && <div className="why">{a.comment}</div>}
            </div>
          ))}
        </div>
      </Card>

      {ask && (
        <Modal title={`${ask.label} v${v}?`} onClose={() => setAsk(null)}>
          <p className="soft">{ask.text}</p>
          {ask.action === "approve" && (
            <div className="field" style={{ marginTop: 16 }}>
              <label htmlFor="cp">Share of vehicles that try this version first: {canary}%</label>
              <input id="cp" type="range" min={1} max={100} value={canary} onChange={(e) => setCanary(Number(e.target.value))} />
              <span className="small muted">{hasLive ? "A live version exists, so start small. Vehicles outside this share keep using it." : "This source has no live version. Messages no version can read fall through to this one."}</span>
            </div>
          )}
          <div className="field" style={{ marginTop: 16 }}>
            <label htmlFor="cm">Reason, for the audit log</label>
            <input id="cm" className="input" maxLength={500} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Reviewed the field map and the golden results" />
          </div>
          <div className="row" style={{ marginTop: 20, justifyContent: "flex-end" }}>
            <button className="btn ghost" onClick={() => setAsk(null)}>Cancel</button>
            <button className="btn flame" onClick={act}>{ask.label}</button>
          </div>
        </Modal>
      )}
    </>
  );
}
