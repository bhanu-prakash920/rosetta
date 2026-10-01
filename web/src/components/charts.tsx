import { useId, useMemo } from "react";
import { compact } from "../lib/format";

/** Sparkline. Pure SVG, no dependency. */
export function Spark({ data, color = "var(--ink)", fill = true, h = 44 }: { data: number[]; color?: string; fill?: boolean; h?: number }) {
  const id = useId();
  const d = useMemo(() => {
    const n = data.length;
    if (n < 2) return null;
    const max = Math.max(1, ...data);
    const w = 100;
    const pts = data.map((v, i) => [(i / (n - 1)) * w, h - 3 - (v / max) * (h - 8)] as const);
    const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    return { line, area: `${line} L${w},${h} L0,${h} Z` };
  }, [data, h]);
  if (!d) return <svg className="spark" style={{ height: h }} aria-hidden="true" />;
  return (
    <svg className="spark" style={{ height: h }} viewBox={`0 0 100 ${h}`} preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.22" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={d.area} fill={`url(#${id})`} />}
      <path d={d.line} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  );
}

export interface Series { name: string; color: string; data: number[] }

/** Stacked area chart with a value axis. Series share one time base, newest on the right. */
export function Area({ series, height = 220, unit = "/s", stacked = true }: { series: Series[]; height?: number; unit?: string; stacked?: boolean }) {
  const W = 800;
  const H = height;
  const P = { l: 46, r: 10, t: 10, b: 22 };
  const n = Math.max(0, ...series.map((s) => s.data.length));
  const g = useMemo(() => {
    if (n < 2) return null;
    const tot = Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + (s.data[i] ?? 0), 0));
    const top = stacked ? Math.max(...tot) : Math.max(...series.flatMap((s) => s.data));
    const max = niceMax(Math.max(1, top));
    const x = (i: number) => P.l + (i / (n - 1)) * (W - P.l - P.r);
    const y = (v: number) => P.t + (1 - v / max) * (H - P.t - P.b);
    const base = new Array(n).fill(0);
    const layers = series.map((s) => {
      const lo = stacked ? base.slice() : new Array(n).fill(0);
      const hi = lo.map((b, i) => b + (s.data[i] ?? 0));
      if (stacked) hi.forEach((v, i) => (base[i] = v));
      const up = hi.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
      const down = lo.map((_v, i) => `L${x(n - 1 - i).toFixed(1)},${y(lo[n - 1 - i]).toFixed(1)}`).join(" ");
      return { ...s, line: up, area: `${up} ${down} Z` };
    });
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => ({ v: max * t, y: y(max * t) }));
    return { layers, ticks };
  }, [series, n, H, stacked]);
  return (
    <svg className="chart" style={{ height }} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
      aria-label={`Chart of ${series.map((s) => s.name).join(", ")}`}>
      {g?.ticks.map((t) => (
        <g key={t.v}>
          <line x1={P.l} x2={W - P.r} y1={t.y} y2={t.y} stroke="var(--line-2)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
          <text x={P.l - 8} y={t.y + 4} textAnchor="end" fontSize="11" fill="var(--ink-3)">{compact(t.v)}</text>
        </g>
      ))}
      {g?.layers.map((l) => (
        <g key={l.name}>
          <path d={l.area} fill={l.color} opacity={stacked ? 0.82 : 0.14} />
          {!stacked && <path d={l.line} fill="none" stroke={l.color} strokeWidth="1.8" vectorEffect="non-scaling-stroke" />}
        </g>
      ))}
      <text x={P.l} y={H - 5} fontSize="11" fill="var(--ink-3)">{n} s ago</text>
      <text x={W - P.r} y={H - 5} fontSize="11" fill="var(--ink-3)" textAnchor="end">now · events{unit}</text>
    </svg>
  );
}

function niceMax(v: number): number {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/** Two bars, model against baseline. */
export function Versus({ label, a, b, aName = "model", bName = "baseline" }: { label: string; a: number; b: number; aName?: string; bName?: string }) {
  return (
    <div style={{ padding: "10px 0", borderBottom: "1px solid var(--line-2)" }}>
      <div className="row between small" style={{ marginBottom: 6 }}><b>{label}</b></div>
      <div className="row" style={{ gap: 10 }}>
        <span className="small muted" style={{ width: 62 }}>{aName}</span>
        <div className="meter flame grow" style={{ height: 10 }}><i style={{ width: `${a * 100}%` }} /></div>
        <b className="small" style={{ width: 52, textAlign: "right" }}>{(a * 100).toFixed(1)}%</b>
      </div>
      <div className="row" style={{ gap: 10, marginTop: 5 }}>
        <span className="small muted" style={{ width: 62 }}>{bName}</span>
        <div className="meter grow" style={{ height: 10 }}><i style={{ width: `${b * 100}%`, background: "var(--ink-3)" }} /></div>
        <b className="small muted" style={{ width: 52, textAlign: "right" }}>{(b * 100).toFixed(1)}%</b>
      </div>
    </div>
  );
}
