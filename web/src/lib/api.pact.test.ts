// Consumer contract: what the web console needs from the Rosetta API.
//
// Every request goes through the console's real client (`api()` and `login()` in
// ./api.ts), against a Pact mock server. The responses list only the fields the
// pages actually read, with type and format matchers instead of exact values.
// The pact file is written to tests/contract/pacts/ and verified against the real
// FastAPI app by tests/contract/pact/test_http_provider.py.
import { MatchersV3 as M, PactV4 } from "@pact-foundation/pact";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, api, getSession, login, setSession } from "./api";
import type { Alert, DlqFamily, DlqGroup, Oem, Page, Vehicle } from "./types";

// Set PACT_LOG_LEVEL=debug to see why the mock server rejected a request.
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};
const pact = new PactV4({
  consumer: "rosetta-web-console",
  provider: "rosetta-api",
  dir: decodeURIComponent(new URL("../../../tests/contract/pacts", import.meta.url).pathname),
  logLevel: (env.PACT_LOG_LEVEL ?? "warn") as "warn",
});

// The console calls relative URLs (/api/v1/...); point them at the mock server.
const realFetch = globalThis.fetch;
function useMockServer(url: string) {
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(new URL(String(input), url), init)) as typeof fetch;
}
afterEach(() => {
  globalThis.fetch = realFetch;
  setSession(null);
});

// The token is whatever the API issued at sign-in; the provider test signs in for real.
const TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl";
const BEARER = { Authorization: M.regex(/^Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/, `Bearer ${TOKEN}`) };
const JSON_TYPE = { "Content-Type": M.regex(/^application\/json/, "application/json") };
function signedIn() {
  setSession({
    token: TOKEN,
    expires: Date.now() + 3_600_000,
    user: { email: "engineer@rosetta.example", name: "Platform Engineer", roles: ["platform_engineer"], tenant_id: null },
  });
}
const SIGNED_IN = "a platform engineer is signed in";

// The sign-in form. The builder's `body()` stores bytes with a content-type check that a
// form post can never pass, so the text body goes through the core call it wraps.
type FormBuilder = { headers(h: Record<string, string>): FormBuilder; interaction: { withRequestBody(body: string, ct: string): boolean } };
function formPost(fields: Record<string, string>) {
  return (b: unknown) => {
    const fb = b as FormBuilder;
    fb.headers({ "Content-Type": "application/x-www-form-urlencoded" });
    fb.interaction.withRequestBody(new URLSearchParams(fields).toString(), "application/x-www-form-urlencoded");
  };
}
const SOURCE = /^[a-z0-9_]+$/;
const VIN = /^[A-HJ-NPR-Z0-9]{17}$/;
const CURSOR = /^[A-Za-z0-9_=-]+$/;

describe("sign-in", () => {
  it("exchanges the demo credentials for a session", async () => {
    await pact
      .addInteraction()
      .given("the demo users exist")
      .uponReceiving("a sign-in with valid credentials")
      .withRequest("POST", "/api/v1/auth/token", formPost({ username: "engineer@rosetta.example", password: "rosetta-demo-2026" }))
      .willRespondWith(200, (b) =>
        b.headers(JSON_TYPE).jsonBody({
          access_token: M.regex(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/, TOKEN),
          expires_in: M.integer(3600),
          user: {
            email: M.email("engineer@rosetta.example"),
            name: M.string("Platform Engineer"),
            roles: M.eachLike(M.regex(/^(admin|platform_engineer|analyst|fleet_manager|service)$/, "platform_engineer")),
            tenant_id: M.nullValue(),
          },
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        const s = await login("engineer@rosetta.example", "rosetta-demo-2026");
        expect(s.token).toBe(TOKEN);
        expect(s.user.roles).toContain("platform_engineer");
        expect(s.expires).toBeGreaterThan(Date.now());
        expect(getSession()?.token).toBe(TOKEN);
      });
  });

  it("shows the API's message when the password is wrong", async () => {
    await pact
      .addInteraction()
      .given("the demo users exist")
      .uponReceiving("a sign-in with a wrong password")
      .withRequest("POST", "/api/v1/auth/token", formPost({ username: "analyst@rosetta.example", password: "not-the-password" }))
      .willRespondWith(401, (b) =>
        b.headers({ "Content-Type": "application/problem+json" }).jsonBody({
          title: M.string("Unauthorized"),
          detail: M.string("invalid credentials"),
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        const err = await login("analyst@rosetta.example", "not-the-password").catch((e: unknown) => e);
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(401);
        expect((err as ApiError).message).toBe("invalid credentials");
        expect(getSession()).toBeNull();
      });
  });
});

describe("sources", () => {
  it("lists every telemetry source with its mapping state", async () => {
    await pact
      .addInteraction()
      .given(SIGNED_IN)
      .uponReceiving("a request for the telemetry sources")
      .withRequest("GET", "/api/v1/oems", (b) => b.headers(BEARER))
      .willRespondWith(200, (b) =>
        b.headers(JSON_TYPE).jsonBody({
          items: M.eachLike({
            key: M.regex(SOURCE, "nordvik"),
            name: M.string("Nordvik Motors"),
            wire_format: M.string("json"),
            status: M.string("live"),
            vehicles: M.integer(120),
            active_versions: M.atLeastLike(M.integer(1), 0, 1),
            pending_review: M.atLeastLike(M.integer(2), 0, 1),
            open_dead_letters: M.integer(0),
          }),
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        signedIn();
        const r = await api<{ items: Oem[] }>("/oems");
        expect(r.items[0].key).toBe("nordvik");
        expect(r.items[0].active_versions).toEqual([1]);
      });
  });
});

describe("dead letters", () => {
  it("groups waiting dead letters into families", async () => {
    await pact
      .addInteraction()
      .given(SIGNED_IN)
      .given("messages from an unmapped source are waiting in the dead-letter queue")
      .uponReceiving("a request for the waiting dead-letter groups")
      .withRequest("GET", "/api/v1/dlq/groups", (b) => b.query({ open_only: "true" }).headers(BEARER))
      .willRespondWith(200, (b) =>
        b.headers(JSON_TYPE).jsonBody({
          items: M.eachLike({
            id: M.integer(1),
            oem: M.regex(SOURCE, "helix"),
            reason: M.regex(/^[A-Z_]+$/, "NO_ADAPTER"),
            field: M.string(""),
            count: M.integer(120),
            replayed: M.integer(0),
            open: M.integer(120),
            last_seen: M.integer(1_790_000_000_000),
            family: M.string("helix-A"),
            sample: { text: M.string('{"fahrzeug":{}}'), binary: M.boolean(false), bytes: M.integer(311) },
          }),
          families: M.eachLike({
            family: M.string("helix-A"),
            oem: M.regex(SOURCE, "helix"),
            groups: M.integer(1),
            count: M.integer(120),
            open: M.integer(120),
            reasons: M.eachValueMatches({ NO_ADAPTER: 120 }, [M.integer(120)]),
            mappable: M.boolean(true),
          }),
          totals: { count: M.integer(120), open: M.integer(120) },
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        signedIn();
        const g = await api<{ items: DlqGroup[]; families: DlqFamily[]; totals: { count: number; open: number } }>(
          "/dlq/groups?open_only=true",
        );
        expect(g.families[0].mappable).toBe(true);
        expect(g.totals.open).toBe(120);
      });
  });

  it("queues a replay of one source", async () => {
    await pact
      .addInteraction()
      .given(SIGNED_IN)
      .given("messages from an unmapped source are waiting in the dead-letter queue")
      .uponReceiving("a request to replay the parked messages of a source")
      .withRequest("POST", "/api/v1/dlq/replay", (b) => b.headers({ ...BEARER, ...JSON_TYPE }).jsonBody({ oem: "helix" }))
      .willRespondWith(202, (b) => b.headers(JSON_TYPE).jsonBody({ job: M.integer(7), status: M.string("queued") }))
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        signedIn();
        const r = await api<{ job: number; status: string }>("/dlq/replay", { method: "POST", json: { oem: "helix" } });
        expect(r.status).toBe("queued");
      });
  });

  it("lists replay jobs, newest first", async () => {
    await pact
      .addInteraction()
      .given(SIGNED_IN)
      .given("a replay job exists")
      .uponReceiving("a request for the replay jobs")
      .withRequest("GET", "/api/v1/dlq/replay", (b) => b.headers(BEARER))
      .willRespondWith(200, (b) =>
        b.headers(JSON_TYPE).jsonBody({
          items: M.eachLike({
            id: M.integer(7),
            oem: M.regex(SOURCE, "helix"),
            status: M.regex(/^(queued|running|done|failed)$/, "done"),
            trigger: M.string("manual"),
            republished: M.integer(120),
            // ISO 8601; PostgreSQL adds the offset, SQLite does not, and `ago()` reads both as UTC
            created_at: M.regex(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)?$/, "2026-09-30T10:00:00.000000+00:00"),
          }),
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        signedIn();
        const r = await api<{ items: { id: number; status: string }[] }>("/dlq/replay");
        expect(r.items[0].status).toBe("done");
      });
  });
});

describe("fleet", () => {
  it("pages through vehicles", async () => {
    await pact
      .addInteraction()
      .given(SIGNED_IN)
      .given("the fleet has more than one page of vehicles")
      .uponReceiving("a request for the first page of vehicles")
      .withRequest("GET", "/api/v1/vehicles", (b) => b.query({ limit: "12" }).headers(BEARER))
      .willRespondWith(200, (b) =>
        b.headers(JSON_TYPE).jsonBody({
          items: M.eachLike({
            id: M.integer(1),
            vin: M.regex(VIN, "7NVGT4B73TA000000"),
            vin_ref: M.regex(VIN, "7NVGT4B73TA000000"),
            oem: M.regex(SOURCE, "nordvik"),
            powertrain: M.regex(/^(BEV|PHEV|HEV|ICE)$/, "ICE"),
            fleet: { id: M.integer(3), name: M.string("Northwind Logistics") },
          }),
          next_cursor: M.regex(CURSOR, "MTI"),
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        signedIn();
        const qs = new URLSearchParams({ limit: "12" });
        const v = await api<Page<Vehicle>>(`/vehicles?${qs}`);
        expect(v.items[0].fleet.name).toBe("Northwind Logistics");
        expect(v.next_cursor).toBe("MTI");
      });
  });

  it("shows the newest alerts", async () => {
    await pact
      .addInteraction()
      .given(SIGNED_IN)
      .given("a vehicle has braked hard")
      .uponReceiving("a request for the newest alerts")
      .withRequest("GET", "/api/v1/alerts", (b) => b.query({ limit: "30" }).headers(BEARER))
      .willRespondWith(200, (b) =>
        b.headers(JSON_TYPE).jsonBody({
          items: M.eachLike({
            id: M.integer(1),
            kind: M.regex(/^[A-Z_]+$/, "HARSH_BRAKE"),
            severity: M.regex(/^(info|warning|critical)$/, "warning"),
            ts: M.integer(1_790_000_000_000),
            detection_ms: M.integer(80),
            oem: M.regex(SOURCE, "nordvik"),
            vin: M.regex(VIN, "7NVGT4B73TA000000"),
            detail: M.like({}),
          }),
        }),
      )
      .executeTest(async (mock) => {
        useMockServer(mock.url);
        signedIn();
        const a = await api<Page<Alert>>("/alerts?limit=30");
        expect(a.items[0].kind).toBe("HARSH_BRAKE");
      });
  });
});
