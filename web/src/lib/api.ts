// One place for every request: adds the bearer token, turns problem+json into
// errors with a readable message, and signs the user out on a 401.

const KEY = "rosetta.session";
const BASE = "/api/v1";

export type Role = "admin" | "platform_engineer" | "analyst" | "fleet_manager" | "service";
export interface Session {
  token: string;
  expires: number;
  user: { email: string; name: string; roles: Role[]; tenant_id: number | null };
}

export class ApiError extends Error {
  status: number;
  title: string;
  extra: Record<string, unknown>;
  constructor(status: number, title: string, detail: string, extra: Record<string, unknown> = {}) {
    super(detail || title);
    this.status = status;
    this.title = title;
    this.extra = extra;
  }
}

let session: Session | null = load();
const listeners = new Set<(s: Session | null) => void>();

function load(): Session | null {
  try {
    // sessionStorage: the token does not outlive the tab and is never sent
    // automatically, so a cross-site request cannot use it.
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    return s.expires > Date.now() ? s : null;
  } catch {
    return null;
  }
}

export function getSession(): Session | null {
  if (session && session.expires <= Date.now()) setSession(null);
  return session;
}

export function setSession(s: Session | null): void {
  session = s;
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: the session lives in memory only */
  }
  listeners.forEach((fn) => fn(s));
}

export function onSession(fn: (s: Session | null) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

async function toError(r: Response): Promise<ApiError> {
  let title = r.statusText || "Request failed";
  let detail = "";
  let extra: Record<string, unknown> = {};
  try {
    const j = await r.json();
    title = j.title ?? title;
    detail = j.detail ?? "";
    extra = j;
    if (Array.isArray(j.errors) && j.errors.length) {
      detail = j.errors.map((e: { field: string; problem: string }) => `${e.field}: ${e.problem}`).join("; ");
    }
  } catch {
    /* not JSON */
  }
  return new ApiError(r.status, title, detail, extra);
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const s = getSession();
  if (s) headers.set("Authorization", `Bearer ${s.token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const r = await fetch(BASE + path, { ...init, headers, body });
  if (r.status === 401 && s) setSession(null);
  if (!r.ok) throw await toError(r);
  return (r.status === 204 ? undefined : await r.json()) as T;
}

export async function login(email: string, password: string): Promise<Session> {
  const form = new URLSearchParams({ username: email, password });
  const r = await fetch(`${BASE}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });
  if (!r.ok) throw await toError(r);
  const j = await r.json();
  const s: Session = { token: j.access_token, expires: Date.now() + (j.expires_in - 30) * 1000, user: j.user };
  setSession(s);
  return s;
}

/** Server-sent events over fetch, so the token travels in a header and never in the URL. */
export function stream(path: string, onEvent: (name: string, data: unknown) => void, onState?: (up: boolean) => void): () => void {
  const ctl = new AbortController();
  let stopped = false;
  (async () => {
    while (!stopped) {
      try {
        const s = getSession();
        if (!s) return;
        const r = await fetch(BASE + path, { headers: { Authorization: `Bearer ${s.token}` }, signal: ctl.signal });
        if (r.status === 401) { setSession(null); return; }
        if (!r.ok || !r.body) throw new Error(String(r.status));
        onState?.(true);
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i: number;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i + 2);
            let name = "message";
            let data = "";
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) name = line.slice(6).trim();
              else if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (data) {
              try { onEvent(name, JSON.parse(data)); } catch { /* skip a malformed frame */ }
            }
          }
        }
      } catch {
        if (stopped) return;
      }
      onState?.(false);
      await new Promise((res) => setTimeout(res, 2000));   // reconnect with a pause
    }
  })();
  return () => { stopped = true; ctl.abort(); };
}
