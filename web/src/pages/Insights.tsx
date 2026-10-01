import { Versus } from "../components/charts";
import { Card, Empty, Head, Meter, Oem, Skel, Stat } from "../components/ui";
import { OEM_NAME, compact, ms, one, pct } from "../lib/format";
import { useApi } from "../lib/hooks";

export default function Insights() {
  const q = useApi<any>("/batch/quality", 15000);
  const ev = useApi<{ items: { oem: string; event: string; count: number }[] }>("/batch/events", 15000);
  const hot = useApi<{ items: { geohash: string; lat: number; lon: number; events: number }[] }>("/batch/hotspots", 15000);
  const model = useApi<any>("/agent/model");
  const e = model.data?.evaluation;
  const maxEv = Math.max(1, ...(ev.data?.items.map((x) => x.count) ?? [1]));

  return (
    <>
      <Head kicker="Insights" title={<>what the <em>history</em> says</>}
        lede="The stream answers what is happening now. These numbers are computed in batch over the Parquet archive, reading only the columns each question needs." />

      <div className="grid g4" style={{ marginBottom: 16 }}>
        <Stat label="Events in the archive" value={compact(q.data?.rows)} sub={q.data ? `${q.data.files} Parquet files` : ""} />
        <Stat label="Stored per event" value={q.data ? one(q.data.bytes_per_row) : "–"} unit="bytes" sub="Columnar, zstd. The raw JSON is about 330." />
        <Stat label="Scan time" value={q.data ? ms(q.data.seconds * 1000) : "–"} sub="Whole archive, group by source" />
        <Stat className="ink" label="Archive size" value={q.data ? compact(q.data.bytes / 1e6) : "–"} unit="MB" sub="Oldest files are removed first" />
      </div>

      <Card title="Data quality by source" sub="Completeness is the share of events that carry each optional field" className="pad0">
        <div className="scroll-x">
          {!q.data ? <div style={{ padding: 22 }}><Skel h={160} /></div> : q.data.sources.length === 0 ? <Empty title="The archive is empty">It fills a few seconds after the pipeline starts.</Empty> : (
            <table className="tbl">
              <thead><tr><th>Source</th><th className="num">Events</th><th className="num">Vehicles</th><th className="num">Normalise, mean</th><th className="num">Arrival delay</th><th>Heading</th><th>Charge</th><th>Fuel</th><th>Events</th><th className="num">Mapping</th></tr></thead>
              <tbody>{q.data.sources.map((s: any) => (
                <tr key={s.oem}>
                  <td><Oem k={s.oem} name={OEM_NAME[s.oem]} /></td>
                  <td className="num">{compact(s.rows)}</td><td className="num">{compact(s.vehicles)}</td>
                  <td className="num">{ms(s.normalize_ms_mean)}</td><td className="num">{ms(s.arrival_delay_ms_mean)}</td>
                  {["heading_deg", "soc_pct", "fuel_pct", "evt"].map((c) => (
                    <td key={c} style={{ minWidth: 90 }}><Meter v={s.completeness[c]} tone={c === "evt" ? "amber" : "leaf"} /><span className="small muted">{pct(s.completeness[c], c === "evt" ? 2 : 0)}</span></td>
                  ))}
                  <td className="num">v{s.latest_mapping_version}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </Card>

      <div className="grid g2" style={{ margin: "16px 0" }}>
        <Card title="Driving events" sub="Counted over the archive">
          {!ev.data ? <Skel /> : ev.data.items.length === 0 ? <Empty title="None yet" /> : ev.data.items.slice(0, 12).map((x) => (
            <div key={x.oem + x.event} className="row" style={{ padding: "6px 0", gap: 12 }}>
              <span style={{ width: 150 }} className="small"><Oem k={x.oem} /></span>
              <span style={{ width: 130 }} className="small mono">{x.event}</span>
              <div className="grow"><Meter v={x.count / maxEv} tone="flame" /></div>
              <b className="small" style={{ width: 50, textAlign: "right" }}>{compact(x.count)}</b>
            </div>
          ))}
        </Card>
        <Card title="Where harsh driving concentrates" sub="Geohash cells of about five kilometres">
          {!hot.data ? <Skel /> : hot.data.items.length === 0 ? <Empty title="No harsh events recorded yet" /> : (
            <table className="tbl">
              <thead><tr><th>Cell</th><th>Centre</th><th className="num">Events</th></tr></thead>
              <tbody>{hot.data.items.slice(0, 10).map((h) => (
                <tr key={h.geohash}><td className="mono">{h.geohash}</td><td className="small muted">{h.lat}, {h.lon}</td><td className="num"><b>{h.events}</b></td></tr>
              ))}</tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="The field-mapping model, against a baseline" sub={e ? `${e.model} · ${compact(e.data.train_rows)} training fields · ${compact(e.data.test_rows)} test fields from ${e.data.test_dialects} unseen dialects` : ""}>
        {!e ? <Empty title="No evaluation file">Run <span className="mono">python -m rosetta train</span>.</Empty> : (
          <div className="grid g2" style={{ gap: 36 }}>
            <div>
              <Versus label="Field and unit both right" a={e.model_scores.exact_label_accuracy} b={e.baseline_scores.exact_label_accuracy} />
              <Versus label="Field right" a={e.model_scores.field_accuracy} b={e.baseline_scores.field_accuracy} />
              <Versus label="Unit right, when the field is right" a={e.model_scores.unit_accuracy_when_field_right} b={e.baseline_scores.unit_accuracy_when_field_right} />
              <Versus label="Whole dialect mapped without a single error" a={e.whole_dialect.model_fully_correct} b={e.whole_dialect.baseline_fully_correct} />
              <p className="small muted" style={{ marginTop: 12 }}>Baseline: {e.baseline}.</p>
            </div>
            <div>
              <div className="tiny muted" style={{ marginBottom: 8 }}>Which evidence carries the result</div>
              {Object.entries(e.ablations).map(([k, v]: [string, any]) => (
                <div key={k} className="row" style={{ padding: "5px 0", gap: 10 }}>
                  <span className="small" style={{ width: 150 }}>{k.replace(/_/g, " ")}</span>
                  <div className="grow"><Meter v={v.exact_label_accuracy} /></div>
                  <b className="small" style={{ width: 52, textAlign: "right" }}>{pct(v.exact_label_accuracy, 1)}</b>
                </div>
              ))}
              <div className="banner info" style={{ marginTop: 16 }}>
                Read this with care. The test dialects use names the model never saw, but every value comes from one simulator.
                Real manufacturer feeds are messier, so expect lower accuracy there. That is why a proposal must also pass the
                golden set and a human review before it touches traffic.
              </div>
              <div className="tiny muted" style={{ margin: "16px 0 6px" }}>How leakage was prevented</div>
              <ul className="small soft" style={{ margin: 0, paddingLeft: 18 }}>{e.data.leakage_controls.map((c: string) => <li key={c.slice(0, 40)}>{c.length > 150 ? c.slice(0, 150) + "…" : c}</li>)}</ul>
            </div>
          </div>
        )}
      </Card>
      <div style={{ height: 40 }} />
    </>
  );
}
