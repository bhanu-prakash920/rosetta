import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, NavLink, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Skel } from "./components/ui";
import { compact } from "./lib/format";
import { LiveProvider, signOut, useCan, useLive, useSession } from "./lib/hooks";
import Landing from "./pages/Landing";
import Login from "./pages/Login";

const Live = lazy(() => import("./pages/Live"));
const Sources = lazy(() => import("./pages/Sources"));
const DeadLetters = lazy(() => import("./pages/DeadLetters"));
const Studio = lazy(() => import("./pages/Studio"));
const FleetMap = lazy(() => import("./pages/FleetMap"));
const Compliance = lazy(() => import("./pages/Compliance"));
const Insights = lazy(() => import("./pages/Insights"));

export function Brand({ to = "/" }: { to?: string }) {
  return (
    <Link to={to} className="brand" aria-label="Rosetta, home">
      rosetta<img src="/img/asterisk.svg" alt="" /><span className="bar" />
    </Link>
  );
}

function Shell() {
  const s = useSession();
  const can = useCan();
  const { ov, up } = useLive();
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => setOpen(false), [loc.pathname]);
  useEffect(() => {
    const h = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);
  if (!s) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  const dead = ov ? Math.max(0, ov.totals.failed - (ov.counters.replayed_ok ?? 0)) : 0;
  const cls = ({ isActive }: { isActive: boolean }) => (isActive ? "on" : "");
  return (
    <>
      <div className="topbar">
        <div className="wrap">
          <Brand to="/app" />
          <nav className="nav" aria-label="Sections">
            <NavLink to="/app" end className={cls}>Live</NavLink>
            <NavLink to="/app/sources" className={cls}>Sources</NavLink>
            {can.staff && <NavLink to="/app/dead-letters" className={cls}>Dead letters{dead > 0 && <span className="n">{compact(dead)}</span>}</NavLink>}
            {can.staff && <NavLink to="/app/studio" className={cls}>Mapping studio</NavLink>}
            <NavLink to="/app/fleet" className={cls}>Fleet</NavLink>
            {can.staff && <NavLink to="/app/insights" className={cls}>Insights</NavLink>}
            <NavLink to="/app/compliance" className={cls}>Compliance</NavLink>
          </nav>
          <div className="who" ref={ref}>
            <span className={`dot ${up ? "ok" : "bad"}`} title={up ? "Live stream connected" : "Reconnecting"} />
            <div className="txt">
              <b>{s.user.name || s.user.email}</b>
              <div className="role small">{can.role.replace(/_/g, " ")}</div>
            </div>
            <button className="orb" aria-label="Menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}><span /></button>
            {open && (
              <div className="menu" role="menu">
                <div className="hint">{s.user.email}</div>
                <a href="/api/docs" target="_blank" rel="noreferrer" role="menuitem">API reference</a>
                <a href="/metrics" target="_blank" rel="noreferrer" role="menuitem">Prometheus metrics</a>
                <Link to="/" role="menuitem">About Rosetta</Link>
                <div className="sep" />
                <button role="menuitem" onClick={() => { signOut(); nav("/login"); }}>Sign out</button>
              </div>
            )}
          </div>
        </div>
      </div>
      <main className="wrap ruled" style={{ minHeight: "70vh" }}>
        <Suspense fallback={<div style={{ padding: "60px 0" }}><Skel h={300} /></div>}>
          <Outlet />
        </Suspense>
      </main>
      <footer className="foot wrap">
        <div className="row between wrap-x">
          <span>Rosetta. An independent academic project. All vehicles, people and brands in it are simulated.</span>
          <span className="mono">{ov ? `${compact(ov.totals.ok)} events translated this session` : "waiting for the pipeline"}</span>
        </div>
      </footer>
    </>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/login" element={<Login />} />
      <Route path="/app" element={<LiveProvider><Shell /></LiveProvider>}>
        <Route index element={<Live />} />
        <Route path="sources" element={<Sources />} />
        <Route path="sources/:oem" element={<Sources />} />
        <Route path="dead-letters" element={<DeadLetters />} />
        <Route path="studio" element={<Studio />} />
        <Route path="studio/:oem" element={<Studio />} />
        <Route path="fleet" element={<FleetMap />} />
        <Route path="insights" element={<Insights />} />
        <Route path="compliance" element={<Compliance />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
