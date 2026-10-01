export interface Lat { n: number; p50: number; p95: number; p99: number; max: number }
export interface OemLive {
  oem: string; ok_per_s: number; failed_per_s: number; dup_per_s: number; sent_per_s: number;
  success_rate: number | null; total_ok: number; total_failed: number; total_duplicates: number;
  versions: { version: number; events: number; per_s: number }[]; reasons: Record<string, number>;
}
export interface Proc { name: string; pid: number | null; alive: boolean; restarts: number; last_exit: number | null; uptime_s: number }
export interface SimSettings {
  mode?: string; hz?: number; enabled?: string[]; drift_pct?: number; dup_pct?: number; reorder_pct?: number;
  malformed_pct?: number; outage_pct?: number; burst?: number; paused?: boolean; max_eps?: number;
}
export interface Overview {
  now: number; uptime_s: number;
  throughput: { ok_per_s: number; failed_per_s: number; dup_per_s: number; sent_per_s: number; processed_per_s: number };
  totals: { ok: number; failed: number; duplicates: number; sent: number };
  counters: Record<string, number>;
  latency_ms: { normalize: Lat; ingest_to_dashboard: Lat; alert: Lat };
  lag: { normalizer: number; processor: number };
  oems: OemLive[];
  routing: Record<string, { active: number[]; canary: number | null; canary_pct: number }>;
  workers: { svc: string; id: string; ts: number; lag_records: number; epoch: number | null; pid: number }[];
  top_failing_fields: { key: string; count: number }[];
  simulator: SimSettings;
  processes: Proc[];
}
export interface Oem {
  key: string; name: string; wmi: string; wire_format: string; status: string; vehicles: number;
  active_versions: number[]; canary: { version: number; pct: number } | null; pending_review: number[];
  versions: number; open_dead_letters: number; live: OemLive | null;
}
export interface FieldMap {
  canonical: string; path: string; transforms: unknown[]; confidence: number | null; rationale: string | null;
  required: boolean; unit: string;
}
export interface Mapping {
  oem: string; version: number; state: string; canary_pct: number; source: string; parent_version: number | null;
  spec_hash: string; created_at: string; activated_at: string | null; fields: number;
  decoder?: Record<string, unknown>; field_map?: FieldMap[];
  actions?: { action: string; from: string; to: string; actor_kind: string; comment: string; ts: string }[];
  validation_runs?: { total: number; passed: number; field_accuracy: Record<string, number>; failures: unknown[]; ts: string }[];
  canary_health?: { ok: number; failed: number; failure_rate: number | null; window_s: number } | null;
}
export interface DlqGroup {
  id: number; oem: string; reason: string; field: string; shape: string; count: number; replayed: number; open: number;
  first_seen: number; last_seen: number; detail: string; family: string;
  sample: { text: string; binary: boolean; bytes: number };
}
export interface DlqFamily { family: string; oem: string; groups: number; count: number; open: number; reasons: Record<string, number>; mappable: boolean }
export interface Step { seq: number; tool: string; ok: boolean; duration_ms: number; input: Record<string, unknown>; output: Record<string, any>; ts: string }
export interface AgentRun {
  id: number; oem: string; status: string; engine: string; trigger: string; summary: string; tokens_in: number;
  tokens_out: number; started_at: string; finished_at: string | null; duration_ms: number | null;
  mapping: { version: number; state: string } | null; steps?: Step[];
}
export interface Page<T> { items: T[]; next_cursor: string | null; limit: number }
export interface LiveState {
  ts: number; lat: number; lon: number; speed_kmh: number | null; heading_deg: number | null; odo_km: number;
  soc_pct: number | null; fuel_pct: number | null; ambient_c: number | null; ignition: boolean; oem: string | null;
  evt: string | null; dtc_count: number; map_v: number; events: number; seen_ts: number; rx_ts: number;
  location_masked?: boolean; geohash?: string;
}
export interface Vehicle {
  id: number; vin: string; vin_ref: string | null; oem: string; powertrain: string; model_year: number;
  fleet: { id: number; name: string }; tenant_id: number; live?: LiveState;
}
export interface Alert { id: number; kind: string; severity: string; ts: number; detected_ts: number; detection_ms: number; oem: string; vin: string; detail: Record<string, any> }
export interface Audit { id: number; ts: string; actor_kind: string; actor: string; action: string; resource: string; outcome: string; tenant_id: number | null; detail: Record<string, unknown>; hash: string; prev_hash: string }
