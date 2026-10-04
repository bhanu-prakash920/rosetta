import { useState } from "react";
import { Card, Code, Empty, Head, Modal, Pill, Seg, Skel, Stat } from "../components/ui";
import { api } from "../lib/api";
import { ago } from "../lib/format";
import { useAction, useApi, useCan } from "../lib/hooks";
import type { Audit, Page } from "../lib/types";

interface Driver { id: number; full_name: string | null; email: string | null; phone: string | null; erased: boolean; erased_at: string | null }

export default function Compliance() {
  const can = useCan();
  const [tab, setTab] = useState<"audit" | "people" | "policy">(can.staff ? "audit" : "people");
  const tabs = [
    ...(can.staff ? [{ v: "audit" as const, l: "Audit trail" }] : []),
    ...(can.people ? [{ v: "people" as const, l: "Personal data" }] : []),
    { v: "policy" as const, l: "Policy" },
  ];
  return (
    <>
      <Head kicker="Compliance" title={<>who saw <em>what</em>, and when</>}
        lede="Every read of fleet data, every change to a mapping and every action of the agent is recorded in a log where each entry carries the hash of the one before it. Remove or edit an entry and the chain breaks."
        right={<Seg value={tab} onChange={setTab} options={tabs} />} />
      {tab === "audit" && can.staff && <AuditTab />}
      {tab === "people" && can.people && <People />}
      {tab === "policy" && <Policy />}
      <div style={{ height: 40 }} />
    </>
  );
}

function AuditTab() {
  const [kind, setKind] = useState("");
  const [cursors, setCursors] = useState<string[]>([]);
  const cur = cursors[cursors.length - 1];
  const qs = new URLSearchParams({ limit: "25" });
  if (kind) qs.set("actor_kind", kind);
  if (cur) qs.set("cursor", cur);
  const a = useApi<Page<Audit>>(`/audit?${qs}`);
  const v = useApi<{ valid: boolean; checked: number; head?: string; broken_at_id?: number }>("/audit/verify");
  const [open, setOpen] = useState<Audit | null>(null);
  return (
    <>
      <div className="grid g3" style={{ marginBottom: 16 }}>
        <Stat label="Hash chain" tone={v.data ? (v.data.valid ? "ok" : "bad") : undefined} value={v.data ? (v.data.valid ? "intact" : "broken") : "…"} sub={v.data?.valid ? `${v.data.checked} entries recomputed from the first one` : v.data ? `breaks at entry ${v.data.broken_at_id}` : ""} />
        <Stat label="Chain head" value={<span className="mono" style={{ fontSize: 22, letterSpacing: 0 }}>{v.data?.head?.slice(0, 16) ?? "…"}</span>} sub="SHA-256 of the newest entry" />
        <Stat className="paper" label="Who is recorded" value="everyone" sub="People, the agent, services and automatic guards" />
      </div>
      <Card title="Entries" sub="Newest first" className="pad0"
        right={<div className="row">
          <Seg value={kind} onChange={(k) => { setCursors([]); setKind(k); }} options={[{ v: "", l: "All" }, { v: "user", l: "People" }, { v: "agent", l: "Agent" }, { v: "system", l: "System" }]} />
          <button className="btn ghost sm" onClick={() => { a.reload(); v.reload(); }}>Refresh</button>
        </div>}>
        <div className="scroll-x">
          {!a.data ? <div style={{ padding: 22 }}><Skel h={200} /></div> : a.data.items.length === 0 ? <Empty title="No entries" /> : (
            <table className="tbl">
              <thead><tr><th>When</th><th>Who</th><th>Did</th><th>To</th><th>Outcome</th><th>Hash</th></tr></thead>
              <tbody>{a.data.items.map((r) => (
                <tr key={r.id} className="click" onClick={() => setOpen(r)}>
                  <td className="small muted nowrap">{ago(r.ts)}</td>
                  <td><Pill s={r.actor_kind === "agent" ? "agent" : "draft"}>{r.actor_kind}</Pill> <span className="small">{r.actor}</span></td>
                  <td className="mono small">{r.action}</td>
                  <td className="mono small clip" style={{ maxWidth: 280 }}>{r.resource}</td>
                  <td><Pill s={r.outcome} /></td>
                  <td className="mono small muted">{r.hash}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
        <div className="row between" style={{ padding: "10px 22px" }}>
          <button className="btn ghost sm" disabled={!cursors.length} onClick={() => setCursors((c) => c.slice(0, -1))}>Newer</button>
          <button className="btn ghost sm" disabled={!a.data?.next_cursor} onClick={() => a.data?.next_cursor && setCursors((c) => [...c, a.data!.next_cursor!])}>Older</button>
        </div>
      </Card>
      {open && (
        <Modal title={open.action} onClose={() => setOpen(null)}>
          <Code value={open} light max={420} />
          <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}><button className="btn" onClick={() => setOpen(null)}>Close</button></div>
        </Modal>
      )}
    </>
  );
}

function People() {
  const d = useApi<Page<Driver>>("/drivers?limit=12");
  const e = useApi<{ items: any[] }>("/compliance/erasure");
  const { run, busy } = useAction();
  const [ask, setAsk] = useState<Driver | null>(null);
  return (
    <div className="grid g-main" style={{ alignItems: "start" }}>
      <Card title="Drivers" sub="Names, e-mail addresses and phone numbers are personal data" className="pad0">
        <div className="scroll-x">
          {!d.data ? <div style={{ padding: 22 }}><Skel h={200} /></div> : (
            <table className="tbl">
              <thead><tr><th>Driver</th><th>Contact</th><th /></tr></thead>
              <tbody>{d.data.items.map((x) => (
                <tr key={x.id}>
                  <td>{x.erased ? <span className="muted">erased</span> : <b>{x.full_name}</b>}<div className="small muted">#{x.id}</div></td>
                  <td className="small">{x.erased ? <span className="muted">Nothing is stored. Erased {ago(x.erased_at)}.</span> : <>{x.email}<br /><span className="muted">{x.phone}</span></>}</td>
                  <td className="right">{x.erased ? <Pill s="completed">erased</Pill> : <button className="btn ghost sm" disabled={busy === `e${x.id}`} onClick={() => setAsk(x)}>Erase</button>}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </Card>
      <Card title="Erasure requests" sub="Each one keeps the proof of what was removed">
        {!e.data?.items.length ? <Empty title="None yet">Erasing a driver removes their identity and cuts every link from vehicle data to them.</Empty> : e.data.items.map((r) => (
          <div key={r.id} style={{ padding: "12px 0", borderBottom: "1px solid var(--line-2)" }}>
            <div className="row between"><b>{r.subject}</b><Pill s={r.status} /></div>
            <div className="small muted" style={{ margin: "4px 0 8px" }}>{r.legal_basis} · {ago(r.completed_at)}</div>
            <div className="row wrap-x small" style={{ gap: 6 }}>
              {r.evidence.fields_erased?.map((f: string) => <span className="tag" key={f}>{f}</span>)}
              <span className="tag">{r.evidence.assignments_removed} vehicle links cut</span>
              <span className="tag">{r.evidence.verified ? "verified by reading back" : "not verified"}</span>
            </div>
          </div>
        ))}
      </Card>
      {ask && (
        <Modal title={`Erase ${ask.full_name}?`} onClose={() => setAsk(null)}>
          <p className="soft">Their name, e-mail, phone and licence hash are removed, and every link from vehicles and trips to them is cut. This cannot be undone. The record that an erasure happened is kept.</p>
          <div className="row" style={{ marginTop: 20, justifyContent: "flex-end" }}>
            <button className="btn ghost" onClick={() => setAsk(null)}>Cancel</button>
            <button className="btn flame" onClick={async () => {
              const x = ask; setAsk(null);
              await run(`e${x.id}`, () => api("/compliance/erasure", { method: "POST", json: { driver_id: x.id } }), () => ({ title: "Erased and verified", body: "The evidence is in the list of requests." }));
              d.reload(); e.reload();
            }}>Erase permanently</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Policy() {
  const p = useApi<any>("/compliance/policy");
  if (!p.data) return <Skel h={240} />;
  return (
    <div className="grid g-main" style={{ alignItems: "start" }}>
      <Card title="Personal data, where it lives and who sees it" className="pad0">
        <div className="scroll-x">
          <table className="tbl">
            <thead><tr><th>Data</th><th>Stored in</th><th>Kept for</th><th>Visible to</th></tr></thead>
            <tbody>{p.data.personal_data.map((r: any) => (
              <tr key={r.data}><td><b>{r.data}</b></td><td className="small">{r.store}</td><td className="small">{r.retention}</td><td className="small">{r.visible_to.join(", ")}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </Card>
      <div className="stack">
        <Card title="Masking" className="flat"><p className="soft small">Analysts: {p.data.masking.analyst}.</p></Card>
        <Card title="Erasure" className="flat"><p className="soft small">{p.data.erasure}.</p></Card>
        <Card title="Rules this follows" className="ink"><ul className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>{p.data.legal_basis.map((l: string) => <li key={l}>{l}</li>)}</ul></Card>
      </div>
    </div>
  );
}
