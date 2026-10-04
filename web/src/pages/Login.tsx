import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Brand } from "../App";
import { ApiError, login } from "../lib/api";
import { useSession } from "../lib/hooks";

const ACCOUNTS = [
  { email: "engineer@rosetta.example", role: "Platform engineer", can: "approves mappings, runs the agent" },
  { email: "analyst@rosetta.example", role: "Analyst", can: "read only, masked locations" },
  { email: "manager@northwind.example", role: "Fleet manager", can: "one tenant, precise locations" },
  { email: "admin@rosetta.example", role: "Admin", can: "everything" },
];

export default function Login() {
  const s = useSession();
  const nav = useNavigate();
  const loc = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const from = (loc.state as { from?: string } | null)?.from;
  // Only same-site paths are followed after sign-in.
  const next = from && from.startsWith("/app") ? from : "/app";
  if (s) return <Navigate to={next} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await login(email.trim(), password);
      nav(next, { replace: true });
    } catch (x) {
      const a = x as ApiError;
      setErr(a.status === 429 ? "Too many attempts. Wait a moment and try again." : a.status === 401 ? "That e-mail and password do not match." : a.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="art">
        <Brand />
        <div style={{ position: "relative", zIndex: 1, maxWidth: 460 }}>
          <h1 className="title">turn every dialect into one <em style={{ color: "var(--paper)" }}>event</em></h1>
          <p style={{ marginTop: 16, maxWidth: "40ch" }}>Sign in to watch 100,000 simulated vehicles being translated live, and to onboard a maker the platform has never seen.</p>
        </div>
        <img className="truck" src="/img/truck-top.webp" alt="" />
      </div>
      <form className="form" onSubmit={submit} noValidate>
        <div>
          <div className="kicker">Console</div>
          <h2 className="h2">Sign in</h2>
        </div>
        {err && <div className="banner" role="alert">{err}</div>}
        <div className="field">
          <label htmlFor="email">E-mail</label>
          <input id="email" className="input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="pw">Password</label>
          <input id="pw" className="input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <button className="btn flame lg" disabled={busy || !email || !password}>{busy ? "Signing in" : "Sign in"}</button>
        <div>
          <div className="tiny muted" style={{ marginBottom: 8 }}>Demo accounts</div>
          <div className="acct">
            {ACCOUNTS.map((a) => (
              <button type="button" key={a.email} onClick={() => setEmail(a.email)}>
                <span><b>{a.role}</b><br /><span className="muted small">{a.can}</span></span>
                <span className="mono muted">{a.email}</span>
              </button>
            ))}
          </div>
          <p className="small muted" style={{ marginTop: 10 }}>The demo password is printed by <span className="tag">python -m rosetta up</span> and listed in the README.</p>
        </div>
      </form>
    </div>
  );
}
