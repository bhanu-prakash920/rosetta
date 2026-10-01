import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, api, getSession, onSession, setSession, stream, type Role, type Session } from "./api";
import type { Overview } from "./types";

// ------------------------------------------------------------------ session
const SessionCtx = createContext<Session | null>(null);
export function SessionProvider({ children }: { children: ReactNode }) {
  const [s, set] = useState<Session | null>(getSession());
  useEffect(() => onSession(set), []);
  return <SessionCtx.Provider value={s}>{children}</SessionCtx.Provider>;
}
export const useSession = () => useContext(SessionCtx);
export const signOut = () => setSession(null);
export function useCan() {
  const s = useSession();
  const roles = s?.user.roles ?? [];
  const has = (...r: Role[]) => r.some((x) => roles.includes(x));
  return {
    operate: has("admin", "platform_engineer"),
    staff: has("admin", "platform_engineer", "analyst"),
    people: has("admin", "fleet_manager"),
    precise: has("admin", "platform_engineer", "fleet_manager"),
    tenant: s?.user.tenant_id ?? null,
    role: roles[0] ?? "",
  };
}

// ------------------------------------------------------------------- toasts
interface Toast { id: number; title: string; body?: string; err?: boolean }
const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => undefined);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, set] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    set((x) => [...x.slice(-3), { ...t, id }]);
    setTimeout(() => set((x) => x.filter((i) => i.id !== id)), t.err ? 7000 : 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast${t.err ? " err" : ""}`}>
            <b>{t.title}</b>
            {t.body}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
export const useToast = () => useContext(ToastCtx);
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const run = useCallback(
    async <T,>(key: string, fn: () => Promise<T>, ok?: (r: T) => { title: string; body?: string }): Promise<T | undefined> => {
      setBusy(key);
      try {
        const r = await fn();
        if (ok) toast(ok(r));
        return r;
      } catch (e) {
        const a = e as ApiError;
        toast({ title: a.title ?? "Something went wrong", body: a.message, err: true });
        return undefined;
      } finally {
        setBusy(null);
      }
    },
    [toast],
  );
  return { run, busy };
}

// -------------------------------------------------------------------- fetch
export interface Loaded<T> { data: T | null; error: ApiError | null; loading: boolean; reload: () => void }
export function useApi<T>(path: string | null, everyMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(!!path);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  useEffect(() => {
    if (!path) { setData(null); setLoading(false); return; }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const go = async () => {
      try {
        const d = await api<T>(path);
        if (!cancelled) { setData(d); setError(null); }
      } catch (e) {
        if (!cancelled) setError(e as ApiError);
      } finally {
        if (!cancelled) {
          setLoading(false);
          // Poll only while the tab is visible.
          if (everyMs) timer = setTimeout(go, document.hidden ? Math.max(everyMs, 5000) : everyMs);
        }
      }
    };
    setLoading(true);
    go();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [path, everyMs, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return useMemo(() => ({ data, error, loading, reload }), [data, error, loading, reload]);
}

// --------------------------------------------------------------------- live
interface Live { ov: Overview | null; up: boolean; history: number[]; failed: number[] }
const LiveCtx = createContext<Live>({ ov: null, up: false, history: [], failed: [] });
export function LiveProvider({ children }: { children: ReactNode }) {
  const s = useSession();
  const [live, setLive] = useState<Live>({ ov: null, up: false, history: [], failed: [] });
  useEffect(() => {
    if (!s) return;
    return stream(
      "/stream",
      (name, data) => {
        if (name !== "overview") return;
        const ov = data as Overview;
        setLive((l) => ({
          ov, up: true,
          history: [...l.history.slice(-119), ov.throughput.ok_per_s],
          failed: [...l.failed.slice(-119), ov.throughput.failed_per_s],
        }));
      },
      (up) => setLive((l) => ({ ...l, up })),
    );
  }, [s?.token]);
  return <LiveCtx.Provider value={live}>{children}</LiveCtx.Provider>;
}
export const useLive = () => useContext(LiveCtx);
