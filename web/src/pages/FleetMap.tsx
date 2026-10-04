import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useRef, useState } from "react";
import { Card, Code, Drawer, Empty, Head, Oem, Pill, Seg, Skel } from "../components/ui";
import { api } from "../lib/api";
import { OEM_HEX, OEM_NAME, ago, clock, compact, dur, int, ms, one } from "../lib/format";
import { useApi, useCan } from "../lib/hooks";
import type { Alert, Page, Vehicle } from "../lib/types";

const CITIES: [string, number, number][] = [
  ["Chennai", 13.0827, 80.2707], ["Bengaluru", 12.9716, 77.5946], ["Mumbai", 19.076, 72.8777], ["Delhi", 28.6139, 77.209],
  ["Hyderabad", 17.385, 78.4867], ["Pune", 18.5204, 73.8567], ["Surat", 21.1702, 72.8311], ["Kolkata", 22.5726, 88.3639],
  ["Ahmedabad", 23.0225, 72.5714], ["Jaipur", 26.9124, 75.7873], ["Kochi", 9.9975, 76.2995], ["Coimbatore", 11.0168, 76.9558],
];

interface Points { count: number; total_in_view: number; masked: boolean; oems: string[]; points: number[][]; vins?: string[] }

export default function FleetMap() {
  const can = useCan();
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const [info, setInfo] = useState<{ shown: number; total: number; masked: boolean } | null>(null);
  const [tilesDown, setTilesDown] = useState(false);
  const [oem, setOem] = useState("");
  const [vin, setVin] = useState<string | null>(null);
  const [tab, setTab] = useState<"alerts" | "vehicles">("alerts");
  const oemRef = useRef(oem);
  oemRef.current = oem;

  useEffect(() => {
    if (!box.current || map.current) return;
    const m = L.map(box.current, { center: [13.0, 80.2], zoom: 10, minZoom: 4, maxZoom: 15, preferCanvas: true, zoomControl: false, attributionControl: true });
    L.control.zoom({ position: "bottomright" }).addTo(m);
    // Base map: Esri's light gray canvas (no key needed), OpenStreetMap as a fallback,
    // and plain city labels if neither can be reached (offline demo, blocked network).
    const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
    const base = L.tileLayer(`${esri}World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`, {
      maxZoom: 16, attribution: "Tiles © Esri, HERE, Garmin, © OpenStreetMap contributors",
    }).addTo(m);
    const labels = L.tileLayer(`${esri}World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 16 }).addTo(m);
    let failures = 0;
    let stage = 0;
    const cityLabels = () => {
      for (const [name, la, lo] of CITIES) {
        L.marker([la, lo], { interactive: false, opacity: 0, keyboard: false })
          .bindTooltip(name, { permanent: true, direction: "right", offset: [8, 0], className: "city-label" }).addTo(m);
      }
    };
    const onError = () => {
      failures += 1;
      if (failures < 6) return;
      failures = 0;
      if (stage === 0) {
        stage = 1;
        m.removeLayer(base);
        m.removeLayer(labels);
        const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19, attribution: "© OpenStreetMap contributors", referrerPolicy: "strict-origin-when-cross-origin",
        } as L.TileLayerOptions).addTo(m);
        osm.on("tileerror", onError);
      } else if (stage === 1) {
        stage = 2;
        m.eachLayer((l) => { if (l instanceof L.TileLayer) m.removeLayer(l); });
        cityLabels();
        setTilesDown(true);
      }
    };
    base.on("tileerror", onError);
    layer.current = L.layerGroup().addTo(m);
    map.current = m;
    let timer: ReturnType<typeof setTimeout>;
    let dead = false;
    const renderer = L.canvas({ padding: 0.3 });
    const load = async () => {
      if (dead) return;
      try {
        const b = m.getBounds();
        const q = new URLSearchParams({ south: String(Math.max(-90, b.getSouth())), north: String(Math.min(90, b.getNorth())), west: String(Math.max(-180, b.getWest())), east: String(Math.min(180, b.getEast())), limit: "5000" });
        if (oemRef.current) q.set("oem", oemRef.current);
        const d = await api<Points>(`/map/points?${q}`);
        if (dead || !layer.current) return;
        layer.current.clearLayers();
        d.points.forEach((p, i) => {
          const [la, lo, spd, o, ign, evt, dtc] = p;
          const color = OEM_HEX[d.oems[o]] ?? "#7b7c80";
          const c = L.circleMarker([la, lo], {
            renderer, radius: evt ? 5 : d.masked ? 5 : ign ? 3 : 2, weight: evt ? 1.5 : 0,
            color: "#232428", fillColor: color, fillOpacity: d.masked ? 0.35 : ign ? 0.8 : 0.35,
          });
          const v = d.vins?.[i];
          c.bindTooltip(`${OEM_NAME[d.oems[o]] ?? "?"} · ${Math.round(spd)} km/h${dtc ? ` · ${dtc} fault code${dtc > 1 ? "s" : ""}` : ""}`, { direction: "top" });
          if (v) c.on("click", () => setVin(v));
          c.addTo(layer.current!);
        });
        setInfo({ shown: d.count, total: d.total_in_view, masked: d.masked });
      } catch { /* keep the last frame */ }
      timer = setTimeout(load, document.hidden ? 6000 : 2000);
    };
    m.on("moveend", () => { clearTimeout(timer); load(); });
    load();
    return () => { dead = true; clearTimeout(timer); m.remove(); map.current = null; };
  }, []);
  useEffect(() => { map.current?.fire("moveend"); }, [oem]);

  async function recentre() {
    // The densest cell of the vehicles this user may see.
    try {
      const c = await api<{ cells: { lat: number; lon: number }[] }>("/map/cells?precision=3");
      if (c.cells.length && map.current) map.current.flyTo([c.cells[0].lat, c.cells[0].lon], 10);
    } catch { /* keep the current view */ }
  }

  return (
    <>
      <Head kicker="Fleet" title={<>every vehicle, <em>one map</em></>}
        lede={can.precise ? "Positions come from the hot state, a store that holds the latest event of each vehicle. Click a vehicle for its history and trips."
          : "Your role sees positions snapped to cells of about five kilometres, and shortened vehicle numbers. Precise data is for people who operate the vehicles."}
        right={<select className="select" style={{ width: 210 }} value={oem} onChange={(e) => setOem(e.target.value)} aria-label="Source">
          <option value="">All sources</option>
          {Object.entries(OEM_NAME).map(([k, n]) => <option key={k} value={k}>{n}</option>)}
        </select>} />
      <div className="grid g-main" style={{ alignItems: "start", marginBottom: 40 }}>
        <div className="mapbox">
          <div ref={box} style={{ height: "100%" }} />
          <div className="map-over">
            <div className="map-chip">{info ? `${int(info.shown)} of ${int(info.total)} vehicles in view` : "Loading"}</div>
            {info && info.total === 0 && (
              <button className="map-chip btn-chip" onClick={recentre}>No vehicles in this area. Show my vehicles</button>
            )}
            {info?.masked && <div className="map-chip" style={{ background: "var(--amber)" }}>Locations masked for your role</div>}
            {tilesDown && <div className="map-chip">Map tiles unavailable: showing positions only</div>}
            {can.tenant != null && <div className="map-chip">Your tenant only</div>}
          </div>
        </div>
        <div className="stack">
          <Card right={<Seg value={tab} onChange={setTab} options={[{ v: "alerts", l: "Alerts" }, { v: "vehicles", l: "Vehicles" }]} />} title={tab === "alerts" ? "Alerts" : "Vehicles"} sub={tab === "alerts" ? "Raised from the stream within seconds" : "Keyset paginated"} className="pad0" >
            <div style={{ padding: "0 0 8px" }}>{tab === "alerts" ? <Alerts /> : <Vehicles onPick={setVin} precise={can.precise} />}</div>
          </Card>
          <Card className="flat"><div className="legend">{Object.entries(OEM_NAME).map(([k, n]) => <span key={k}><i className="swatch" style={{ background: OEM_HEX[k] }} />{n}</span>)}</div></Card>
        </div>
      </div>
      {vin && <VehicleDrawer vin={vin} onClose={() => setVin(null)} />}
    </>
  );
}

function Alerts() {
  const a = useApi<Page<Alert>>("/alerts?limit=30", 3000);
  if (!a.data) return <div style={{ padding: 22 }}><Skel /></div>;
  if (!a.data.items.length) return <Empty title="No alerts">Harsh braking, speeding, low charge and new fault codes appear here.</Empty>;
  return (
    <div className="scroll-y" style={{ maxHeight: 560 }}>
      <table className="tbl">
        <thead><tr><th>Alert</th><th>Vehicle</th><th className="num">Detected in</th></tr></thead>
        <tbody>{a.data.items.map((x) => (
          <tr key={x.id}>
            <td><Pill s={x.severity}>{x.kind.replace(/_/g, " ").toLowerCase()}</Pill><div className="small muted" style={{ marginTop: 3 }}>{clock(x.ts)}{x.detail.codes ? ` · ${x.detail.codes.join(", ")}` : x.detail.speed_kmh != null ? ` · ${Math.round(x.detail.speed_kmh)} km/h` : ""}</div></td>
            <td className="mono small">{x.vin}<div><Oem k={x.oem} /></div></td>
            <td className="num small">{ms(x.detection_ms)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Vehicles({ onPick, precise }: { onPick: (v: string) => void; precise: boolean }) {
  const [cursors, setCursors] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const cur = cursors[cursors.length - 1];
  const qs = new URLSearchParams({ limit: "12" });
  if (cur) qs.set("cursor", cur);
  if (q.length >= 3) qs.set("q", q);
  const v = useApi<Page<Vehicle>>(`/vehicles?${qs}`);
  return (
    <>
      {precise && <div style={{ padding: "0 22px 10px" }}><input className="input" placeholder="VIN starts with" value={q} maxLength={17} onChange={(e) => { setCursors([]); setQ(e.target.value.replace(/[^A-Za-z0-9]/g, "").toUpperCase()); }} aria-label="Search by VIN" /></div>}
      {!v.data ? <div style={{ padding: 22 }}><Skel /></div> : (
        <table className="tbl">
          <thead><tr><th>Vehicle</th><th>Fleet</th><th className="num">Speed</th></tr></thead>
          <tbody>{v.data.items.map((x) => (
            <tr key={x.id} className={x.vin_ref ? "click" : ""} onClick={() => x.vin_ref && onPick(x.vin_ref)}>
              <td className="mono small">{x.vin}<div><Oem k={x.oem} /> <span className="muted">{x.powertrain}</span></div></td>
              <td className="small">{x.fleet.name}</td>
              <td className="num small">{x.live ? `${Math.round(x.live.speed_kmh ?? 0)} km/h` : <span className="muted">no data</span>}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
      <div className="row between" style={{ padding: "10px 22px" }}>
        <button className="btn ghost sm" disabled={!cursors.length} onClick={() => setCursors((c) => c.slice(0, -1))}>Previous</button>
        <button className="btn ghost sm" disabled={!v.data?.next_cursor} onClick={() => v.data?.next_cursor && setCursors((c) => [...c, v.data!.next_cursor!])}>Next</button>
      </div>
    </>
  );
}

function VehicleDrawer({ vin, onClose }: { vin: string; onClose: () => void }) {
  const v = useApi<any>(`/vehicles/${vin}`, 2000);
  const t = useApi<any>(`/vehicles/${vin}/trips`);
  const h = useApi<any>(`/vehicles/${vin}/history?limit=5`);
  const l = v.data?.live;
  return (
    <Drawer onClose={onClose} label="Vehicle">
      <h2 className="h2 mono" style={{ letterSpacing: 0 }}>{vin}</h2>
      {v.error ? <div className="banner" style={{ marginTop: 14 }}>{v.error.message}</div> : !v.data ? <Skel h={200} /> : (
        <>
          <div className="row wrap-x" style={{ margin: "10px 0 20px" }}>
            <Oem k={v.data.oem} name={OEM_NAME[v.data.oem]} /><span className="tag">{v.data.powertrain}</span><span className="tag">{v.data.model_year}</span><span className="tag">{v.data.fleet?.name}</span>
          </div>
          {!l ? <Empty title="No event received yet" /> : (
            <div className="grid g3">
              <div className="card flat stat"><div className="l">Speed</div><div className="v">{Math.round(l.speed_kmh ?? 0)}<small>km/h</small></div></div>
              <div className="card flat stat"><div className="l">{l.soc_pct != null ? "Charge" : "Fuel"}</div><div className="v">{one(l.soc_pct ?? l.fuel_pct)}<small>%</small></div></div>
              <div className="card flat stat"><div className="l">Odometer</div><div className="v">{compact(l.odo_km)}<small>km</small></div></div>
            </div>
          )}
          {l && <p className="small muted" style={{ marginTop: 12 }}>Last event {ago(l.ts)}, translated by mapping v{l.map_v}, visible {ms(l.seen_ts - l.rx_ts)} after it arrived. {int(l.events)} events this session.</p>}
          <div className="tiny muted" style={{ margin: "24px 0 8px" }}>Trips and stops</div>
          {!t.data ? <Skel /> : t.data.segments.length === 0 ? <Empty title="Not enough history yet" /> : (
            <>
              <p className="small soft" style={{ marginBottom: 8 }}>
                Dynamic programming found <b>{t.data.segments.length}</b> {t.data.segments.length === 1 ? "segment" : "segments"} in {t.data.points} points. A plain speed threshold cuts the same track into <b>{t.data.baseline_segments}</b>.
              </p>
              <table className="tbl"><tbody>{t.data.segments.slice(-8).map((s: any, i: number) => (
                <tr key={i}><td><Pill s={s.kind === "trip" ? "ok" : "draft"}>{s.kind}</Pill></td><td className="small">{clock(s.start_ts)}</td><td className="small">{dur(s.duration_s)}</td><td className="num small">{s.kind === "trip" ? `${s.distance_km} km · max ${Math.round(s.max_speed_kmh)}` : ""}</td></tr>
              ))}</tbody></table>
            </>
          )}
          <div className="tiny muted" style={{ margin: "24px 0 8px" }}>Latest canonical events</div>
          {h.data ? <Code value={h.data.items.slice(0, 3)} max={300} /> : <Skel />}
        </>
      )}
    </Drawer>
  );
}
