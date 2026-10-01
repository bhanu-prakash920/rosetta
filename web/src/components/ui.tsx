import { useEffect, useMemo, type ReactNode } from "react";
import { highlight, pretty } from "../lib/format";

export function Head({ kicker, title, lede, right, children }: { kicker: string; title: ReactNode; lede?: ReactNode; right?: ReactNode; children?: ReactNode }) {
  return (
    <header className="head">
      <div className="row between wrap-x" style={{ alignItems: "flex-end", gap: 24 }}>
        <div className="grow">
          <div className="kicker">{kicker}</div>
          <h1 className="title">{title}</h1>
          {lede && <p className="lede">{lede}</p>}
        </div>
        {right && <div className="row wrap-x">{right}</div>}
      </div>
      {children}
    </header>
  );
}

export function Card({ title, right, children, className = "", sub }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; sub?: ReactNode }) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <div className="card-h">
          <div>
            <h2 className="h3">{title}</h2>
            {sub && <div className="small muted" style={{ marginTop: 2 }}>{sub}</div>}
          </div>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, unit, sub, tone, className = "", children }: { label: ReactNode; value: ReactNode; unit?: string; sub?: ReactNode; tone?: "ok" | "warn" | "bad"; className?: string; children?: ReactNode }) {
  return (
    <div className={`card stat ${className}`}>
      <div className="l">{tone && <span className={`dot ${tone}`} />}{label}</div>
      <div className="v">{value}{unit && <small>{unit}</small>}</div>
      {sub && <div className="s">{sub}</div>}
      {children}
    </div>
  );
}

export const Pill = ({ s, children }: { s: string; children?: ReactNode }) => (
  <span className={`pill ${s}`}>{children ?? s.replace(/_/g, " ")}</span>
);

export function Code({ value, light, max }: { value: unknown; light?: boolean; max?: number }) {
  const html = useMemo(() => highlight(pretty(value)), [value]);
  // The highlighter escapes its input before adding spans, so this is safe for payload text.
  return <pre className={`code${light ? " light" : ""}`} style={max ? { maxHeight: max } : undefined} dangerouslySetInnerHTML={{ __html: html }} />;
}

export function Meter({ v, tone = "" }: { v: number; tone?: string }) {
  return <div className={`meter ${tone}`} role="img" aria-label={`${Math.round(v * 100)} percent`}><i style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} /></div>;
}

export function Empty({ title, children, icon = "/img/asterisk.svg" }: { title: string; children?: ReactNode; icon?: string }) {
  return (
    <div className="empty">
      <img src={icon} alt="" />
      <div className="h3" style={{ color: "var(--ink)" }}>{title}</div>
      {children && <div className="small" style={{ marginTop: 6, maxWidth: 420, marginInline: "auto" }}>{children}</div>}
    </div>
  );
}

export const Skel = ({ h = 120 }: { h?: number }) => <div className="skel" style={{ height: h }} aria-busy="true" />;

export function Drawer({ onClose, children, label }: { onClose: () => void; children: ReactNode; label: string }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", k); document.body.style.overflow = prev; };
  }, [onClose]);
  return (
    <div className="veil" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={label}>
        <div className="row between" style={{ marginBottom: 18 }}>
          <span className="tiny muted">{label}</span>
          <button className="btn ghost sm" onClick={onClose}>Close</button>
        </div>
        {children}
      </aside>
    </div>
  );
}

export function Modal({ onClose, title, children }: { onClose: () => void; title: string; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="veil modal-c" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h2 className="h2" style={{ marginBottom: 14 }}>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { v: T; l: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg" role="tablist">
      {options.map((o) => (
        <button key={o.v} role="tab" aria-selected={o.v === value} className={o.v === value ? "on" : ""} onClick={() => onChange(o.v)}>{o.l}</button>
      ))}
    </div>
  );
}

export const Oem = ({ k, name }: { k: string; name?: string }) => (
  <span className="row" style={{ gap: 8 }}>
    <span className="swatch" style={{ background: `var(--oem-${k}, var(--ink-3))` }} />
    <b>{name ?? k}</b>
  </span>
);
