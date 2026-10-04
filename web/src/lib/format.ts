const nf = new Intl.NumberFormat("en-IN");
const nf1 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });
export const int = (n: number | null | undefined) => (n == null ? "–" : nf.format(Math.round(n)));
export const one = (n: number | null | undefined) => (n == null ? "–" : nf1.format(n));
export function compact(n: number | null | undefined): string {
  if (n == null) return "–";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 1 : 2) + "M";
  if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "K";
  return nf.format(Math.round(n));
}
export const pct = (x: number | null | undefined, d = 1) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
export function ms(x: number | null | undefined): string {
  if (x == null) return "–";
  if (x >= 1000) return (x / 1000).toFixed(2) + " s";
  return (x < 10 ? x.toFixed(1) : Math.round(x)) + " ms";
}
export function ago(ts: number | string | null | undefined): string {
  if (!ts) return "–";
  const t = typeof ts === "string" ? Date.parse(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z") : ts;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
export const clock = (ts: number) => new Date(ts).toLocaleTimeString("en-GB", { hour12: false });
export function dur(s: number): string {
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
export const OEM_COLOR: Record<string, string> = {
  nordvik: "var(--oem-nordvik)", pacifica: "var(--oem-pacifica)", stellaris: "var(--oem-stellaris)",
  kaizen: "var(--oem-kaizen)", voltaic: "var(--oem-voltaic)", helix: "var(--oem-helix)",
};
export const OEM_HEX: Record<string, string> = {
  nordvik: "#5c88ee", pacifica: "#ff5623", stellaris: "#67c56b", kaizen: "#ffbf33", voltaic: "#8a5a3a", helix: "#232428",
};
export const OEM_NAME: Record<string, string> = {
  nordvik: "Nordvik Motors", pacifica: "Pacifica Auto", stellaris: "Stellaris Group", kaizen: "Kaizen Motor",
  voltaic: "Voltaic EV", helix: "Helix Mobility",
};
export const REASON: Record<string, string> = {
  NO_ADAPTER: "No mapping for this source",
  SCHEMA_MISMATCH: "A required field is missing: the format changed",
  DECODE_ERROR: "The payload could not be parsed",
  TRANSFORM_ERROR: "A value could not be converted",
  INVALID: "Translated, but failed validation",
  OVERSIZE: "Payload too large",
};
export function transformLabel(t: unknown): string {
  if (typeof t === "string") return t.replace(/_/g, " ");
  const o = t as Record<string, any>;
  if (o.op === "unit") return `${o.from} → ${({ speed: "km/h", distance: "km", temperature: "°C", percent: "%", angle: "deg", time: "ms" } as any)[o.quantity] ?? o.quantity}`;
  if (o.op === "scale") return `× ${o.factor}`;
  if (o.op === "affine") return `× ${o.factor} + ${o.offset}`;
  if (o.op === "enum") return `lookup (${Object.keys(o.map ?? {}).length} codes)`;
  if (o.op === "split") return `split "${o.sep}"`;
  return String(o.op);
}
/** Tiny JSON highlighter. Input is escaped first, so payload content is never treated as markup. */
export function highlight(src: string): string {
  const esc = src.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m, str, colon, bool, num) => {
      if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
      if (bool) return `<span class="b">${bool}</span>`;
      if (num) return `<span class="n">${num}</span>`;
      return m;
    },
  );
}
export function pretty(v: unknown): string {
  if (typeof v === "string") {
    try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; }
  }
  return JSON.stringify(v, null, 2);
}
