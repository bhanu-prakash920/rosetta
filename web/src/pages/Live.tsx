import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Area, Spark } from "../components/charts";
import { Card, Empty, Meter, Modal, Oem, Pill, Skel, Stat } from "../components/ui";
import { api } from "../lib/api";
import { OEM_HEX, OEM_NAME, compact, dur, int, ms, pct } from "../lib/format";
import { useAction, useApi, useCan, useLive } from "../lib/hooks";
import type { Overview } from "../lib/types";

const SCENARIOS: { key: string; off?: string; title: string; text: string; on: (o: Overview) => boolean }[] = [
  { key: "launch_helix", off: "stop_helix", title: "A new maker starts sending", text: "Helix Mobility comes online. The platform has no mapping for it.", on: (o) => !!o.simulator.enabled?.includes("helix") },
  { key: "ota_drift", off: "ota_reset", title: "Firmware update", text: "35% of Pacifica vehicles rename two fields and switch to metric.", on: (o) => (o.simulator.drift_pct ?? 0) > 0 },
  { key: "outage", off: "recover", title: "Network outage", text: "20% of devices go offline and buffer. Recovery floods the intake.", on: (o) => (o.simulator.outage_pct ?? 0) > 0 },
  { key: "shift_start", off: "calm", title: "Shift start", text: "Parked vehicles start within seconds of each other.", on: (o) => (o.simulator.burst ?? 1) > 1 },
  { key: "pause", off: "resume", title: "Pause the fleet", text: "Stop the simulator. The pipeline drains and idles.", on: (o) => !!o.simulator.paused },
];

function insights(ov: Overview) {
  const out: { t: string; v: string; w: string; a: string; to: string }[] = [];
  for (const o of ov.oems) {
    const open = (o.reasons.NO_ADAPTER ?? 0) + (o.reasons.SCHEMA_MISMATCH ?? 0);
    const route = ov.routing[o.oem];
    if (o.reasons.NO_ADAPTER && (!route || route.active.length === 0) && o.failed_per_s > 0)
      out.push({ t: `${OEM_NAME[o.oem] ?? o.oem}`, v: compact(open), w: "parked, no mapping", a: "Ask the agent for a mapping", to: `/app/studio/${o.oem}` });
    else if (o.reasons.SCHEMA_MISMATCH && o.failed_per_s / Math.max(1, o.failed_per_s + o.ok_per_s) > 0.02)
      out.push({ t: `${OEM_NAME[o.oem] ?? o.oem} format drift`, v: pct(o.failed_per_s / Math.max(1, o.failed_per_s + o.ok_per_s), 0), w: "of messages unreadable", a: "Review the new format", to: `/app/studio/${o.oem}` });
    else if (route?.canary)
      out.push({ t: `${OEM_NAME[o.oem] ?? o.oem} canary v${route.canary}`, v: `${route.canary_pct}%`, w: "of vehicles", a: "Promote or roll back", to: `/app/sources/${o.oem}` });
  }
  if (!out.length) {
    const l = ov.latency_ms.ingest_to_dashboard;
    out.push({ t: "Ingest to dashboard", v: ms(l.p95), w: "p95, target 2 s", a: "All sources healthy", to: "/app/sources" });
    out.push({ t: "Duplicates dropped", v: compact(ov.totals.duplicates), w: "exactly, no loss", a: "See how", to: "/app/insights" });
  }
  return out.slice(0, 2);
}

export default function Live() {
  const { ov, history } = useLive();
  const can = useCan();
  const series = useApi<{ ok: Record<string, number[]>; failed: number[] }>("/series/all?seconds=120", 2000);
  const { run, busy } = useAction();
  const [kill, setKill] = useState<string | null>(null);

  const chart = useMemo(() => {
    if (!series.data) return [];
    const s = Object.entries(series.data.ok).map(([k, d]) => ({ name: OEM_NAME[k] ?? k, color: OEM_HEX[k] ?? "#999", data: d }));
    s.push({ name: "Dead-lettered", color: "#efb8b1", data: series.data.failed });
    return s;
  }, [series.data]);

  if (!ov) return <div style={{ padding: "60px 0" }}><Skel h={420} /></div>;
  const idle = ov.throughput.ok_per_s === 0 && ov.totals.ok === 0;
  const e2e = ov.latency_ms.ingest_to_dashboard;
  const total = ov.throughput.ok_per_s + ov.throughput.failed_per_s;
  const success = total ? ov.throughput.ok_per_s / total : null;
  const open = Math.max(0, ov.totals.failed - (ov.counters.replayed_ok ?? 0));
  const cards = insights(ov);

  return (
    <>
      <header className="head">
        <div className="kicker">Live, {dur(ov.uptime_s)} on air</div>
        <div className="hero-num">
          <div className="big" aria-live="off">{compact(ov.throughput.ok_per_s)}</div>
          <div className="unit">events translated every second, from {ov.oems.filter((o) => o.ok_per_s > 0).length} car makers into one format</div>
        </div>
      </header>

      {idle && <div className="banner info" style={{ marginBottom: 16 }}>The pipeline is idle. Start everything with <span className="mono">python -m rosetta up</span>, or resume the simulator below.</div>}

      <div className="grid g4" style={{ marginBottom: 16 }}>
        <Stat label="Ingest to dashboard" tone={e2e.p95 < 2000 ? "ok" : "bad"} value={ms(e2e.p95)} sub={`p95. p99 ${ms(e2e.p99)}, target 2 s`} />
        <Stat label="Translated successfully" tone={success == null ? undefined : success > 0.99 ? "ok" : "warn"} value={pct(success, 2)} sub={`${compact(ov.totals.ok)} events so far`}>
          <Spark data={history} color="var(--leaf-ink)" h={36} />
        </Stat>
        <Stat label="Parked in dead letters" tone={open > 1000 ? "warn" : "ok"} value={compact(open)} sub={`${compact(ov.counters.replayed_ok ?? 0)} replayed successfully`} />
        <Stat className="ink" label="Waiting in the queue" value={compact(ov.lag.normalizer + ov.lag.processor)} unit="events" sub={`normalise ${compact(ov.lag.normalizer)} · store ${compact(ov.lag.processor)}`} />
      </div>

      <div className="grid g-main" style={{ marginBottom: 16 }}>
        <Card title="Throughput by source" sub="Events per second, last two minutes"
          right={<div className="legend">{chart.map((s) => <span key={s.name}><i className="swatch" style={{ background: s.color }} />{s.name}</span>)}</div>}>
          {chart.length ? <Area series={chart} height={390} /> : <Skel h={390} />}
        </Card>
        <div className="stage">
          <div className="rings" />
          <div className="cap">
            <div className="kicker">Right now</div>
            <h2 className="h2">{cards.length && cards[0].w.includes("parked") ? "Someone new is talking" : "What needs attention"}</h2>
          </div>
          <img className="truck" src="/img/truck-top.webp" alt="" />
          {cards.map((c, i) => (
            <Link to={c.to} key={c.t} className={`float ${i ? "b" : ""}`} style={i ? { left: "6%", bottom: "9%" } : { left: "6%", top: "30%" }}>
              <div className="t"><img src={i ? "/img/icon-translate.svg" : "/img/icon-signal.svg"} alt="" width={14} />{c.t}</div>
              <div className="v">{c.v}</div><div className="w">{c.w}</div>
              <span className="a">{c.a}</span>
            </Link>
          ))}
        </div>
      </div>

      <div className="grid g-main" style={{ marginBottom: 16 }}>
        <Card title="Sources" sub="Each one speaks its own dialect" right={<Link className="linkish small" to="/app/sources">All sources</Link>}>
          {ov.oems.length === 0 && <Empty title="No traffic yet" />}
          {ov.oems.map((o) => {
            const r = ov.routing[o.oem];
            const t = o.ok_per_s + o.failed_per_s;
            return (
              <div className="oem-row" key={o.oem}>
                <Link to={`/app/sources/${o.oem}`}><Oem k={o.oem} name={OEM_NAME[o.oem]} /></Link>
                <div><b>{int(o.ok_per_s)}</b><span className="muted small"> /s</span></div>
                <div>
                  <Meter v={t ? o.ok_per_s / t : 0} tone={t && o.ok_per_s / t > 0.99 ? "leaf" : t && o.ok_per_s / t > 0.5 ? "amber" : "flame"} />
                  <div className="small muted" style={{ marginTop: 4 }}>{t ? pct(o.ok_per_s / t, 1) + " readable" : "silent"}</div>
                </div>
                <div className="row wrap-x" style={{ gap: 4 }}>
                  {r?.active.map((v) => <Pill key={v} s="active">v{v}</Pill>)}
                  {r?.canary && <Pill s="canary">v{r.canary} · {r.canary_pct}%</Pill>}
                  {!r && <Pill s="rejected">no mapping</Pill>}
                </div>
                <span className="small muted right nowrap">{compact(o.total_failed)} parked</span>
              </div>
            );
          })}
        </Card>
        <Card title="Fields that fail most" sub="Heavy hitters, tracked with a Count-Min sketch">
          {ov.top_failing_fields.length === 0 ? <Empty title="Nothing is failing" /> : (
            <table className="tbl"><tbody>
              {ov.top_failing_fields.slice(0, 8).map((f) => (
                <tr key={f.key}><td className="mono clip" style={{ maxWidth: 260 }}>{f.key}</td><td className="num">{compact(f.count)}</td></tr>
              ))}
            </tbody></table>
          )}
        </Card>
      </div>

      {can.operate && (
        <div className="grid g-main" style={{ marginBottom: 16 }}>
          <Card title="Make something happen" sub="Scenarios change what the simulated fleet does">
            <div className="scen">
              {SCENARIOS.map((s) => {
                const on = s.on(ov);
                const target = on && s.off ? s.off : s.key;
                return (
                  <button key={s.key} className={on ? "on" : ""} disabled={busy === s.key || (on && !s.off)}
                    onClick={() => run(s.key, () => api(`/simulator/scenario/${target}`, { method: "POST" }), () => ({ title: on ? "Scenario ended" : s.title, body: "Takes effect within a second." }))}>
                    <b>{s.title}{on ? " · on" : ""}</b><span>{on && s.off ? "Click to end it." : s.text}</span>
                  </button>
                );
              })}
            </div>
            <div className="row wrap-x small muted" style={{ marginTop: 14 }}>
              <span>Injected faults:</span>
              <span className="tag">{ov.simulator.dup_pct ?? 0}% duplicates</span>
              <span className="tag">{ov.simulator.reorder_pct ?? 0}% out of order</span>
              <span className="tag">{ov.simulator.malformed_pct ?? 0}% malformed</span>
              <span>so far {compact(ov.counters.sim_duplicates)} duplicates, {compact(ov.counters.sim_reordered)} reordered, {compact(ov.counters.sim_malformed)} damaged</span>
            </div>
          </Card>
          <Card title="Processes" sub="Hover one and kill it. The supervisor brings it back.">
            {ov.processes.length === 0 ? <Empty title="Not supervised here">The pipeline runs as separate services in this deployment.</Empty> : (
              <div className="proc">
                {ov.processes.map((p) => (
                  <div className="p" key={p.name}>
                    <b><span className={`dot ${p.alive ? "ok" : "bad"}`} style={{ marginRight: 6 }} />{p.name}</b>
                    <span className="muted">up {dur(p.uptime_s)}{p.restarts ? ` · ${p.restarts} restart${p.restarts > 1 ? "s" : ""}` : ""}</span>
                    {!p.name.startsWith("simulator") && <button className="x" onClick={() => setKill(p.name)}>kill</button>}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {kill && (
        <Modal title={`Kill ${kill}?`} onClose={() => setKill(null)}>
          <p className="soft">This sends SIGKILL, the same as a machine dying. The process gets no chance to clean up. Watch the queue grow, then drain once the supervisor restarts it from its last checkpoint.</p>
          <div className="row" style={{ marginTop: 20, justifyContent: "flex-end" }}>
            <button className="btn ghost" onClick={() => setKill(null)}>Cancel</button>
            <button className="btn flame" onClick={async () => {
              const name = kill; setKill(null);
              await run("kill", () => api("/system/chaos/kill", { method: "POST", json: { name } }), () => ({ title: `${name} killed`, body: "Restarting from its checkpoint." }));
            }}>Kill it</button>
          </div>
        </Modal>
      )}
    </>
  );
}
