"""rosetta.api.support: problem details, cursors, paging, rate limiting, headers and the read audit."""
from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rosetta import config
from rosetta.api import support
from rosetta.api.support import TokenBucket, decode_cursor, encode_cursor, page, problem


# -------------------------------------------------------------------- problem
def test_problem_details():
    r = problem(404, "Not found", "no vehicle with that VIN", vin="X")
    assert r.status_code == 404
    assert r.media_type == "application/problem+json" == support.PROBLEM
    assert json.loads(r.body) == {"type": "https://rosetta.example/problems/not-found", "title": "Not found",
                                  "status": 404, "detail": "no vehicle with that VIN", "vin": "X"}


def test_problem_without_detail():
    body = json.loads(problem(429, "Too many requests").body)
    assert body["detail"] == "" and body["type"].endswith("/too-many-requests")


def test_titles_cover_the_status_codes_the_api_uses():
    assert set(support.TITLES) == {400, 401, 403, 404, 409, 413, 422, 429, 503}
    assert all(t[0].isupper() for t in support.TITLES.values())


# -------------------------------------------------------------------- cursors
@pytest.mark.parametrize("parts", [(42,), ("2026-09-25T10:00:00Z", 17), (1790000000000, "1HGCM82633A004352"),
                                   (1.5, -3, "x"), ("",), ("with spaces / and + signs",), ("über",)])
def test_cursor_round_trip(parts):
    cursor = encode_cursor(*parts)
    assert decode_cursor(cursor, n=len(parts)) == list(parts)


def test_cursor_is_url_safe_and_unpadded():
    for k in range(40):
        cursor = encode_cursor("x" * k, k)
        assert "=" not in cursor and "+" not in cursor and "/" not in cursor


@pytest.mark.parametrize("cursor", [None, ""])
def test_no_cursor_means_the_first_page(cursor):
    assert decode_cursor(cursor) is None


def forged(value: Any) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(value)).decode().rstrip("=")


@pytest.mark.parametrize("cursor", [
    "!!!", "not base64 at all", "AAAA", forged({"id": 1}), forged("text"), forged(5), forged(None),
    forged([]), forged([1, 2]), forged([[1]]), forged([{"a": 1}]), forged([None]),
    base64.urlsafe_b64encode(b"\xff\xfe").decode(), base64.urlsafe_b64encode(b"[1").decode(),
])
def test_forged_cursor_is_a_400(cursor):
    with pytest.raises(HTTPException) as info:
        decode_cursor(cursor, n=1)
    assert info.value.status_code == 400 and info.value.detail == "invalid cursor"


def test_cursor_with_the_wrong_number_of_parts_is_a_400():
    with pytest.raises(HTTPException):
        decode_cursor(encode_cursor(1, 2), n=3)
    with pytest.raises(HTTPException):
        decode_cursor(encode_cursor(1, 2, 3), n=2)
    assert decode_cursor(encode_cursor(1, 2), n=2) == [1, 2]


# ----------------------------------------------------------------------- page
def cursor_of(row: dict[str, int]) -> str:
    return encode_cursor(row["id"])


def rows(n: int) -> list[dict[str, int]]:
    return [{"id": i} for i in range(n)]


def test_page_with_more_to_come():
    p = page(rows(11), limit=10, cursor_of=cursor_of)
    assert p["items"] == rows(10) and p["limit"] == 10
    assert decode_cursor(p["next_cursor"]) == [9], "the cursor points at the last row that was returned"


@pytest.mark.parametrize("n", [0, 1, 9, 10])
def test_last_page_has_no_cursor(n):
    p = page(rows(n), limit=10, cursor_of=cursor_of)
    assert p == {"items": rows(n), "next_cursor": None, "limit": 10}


def test_page_with_limit_zero():
    assert page(rows(3), limit=0, cursor_of=cursor_of) == {"items": [], "next_cursor": None, "limit": 0}


def test_paging_through_a_list_visits_every_row_once():
    data = rows(47)
    seen, after = [], -1
    for _ in range(20):
        chunk = [r for r in data if r["id"] > after][:11]
        p = page(chunk, limit=10, cursor_of=cursor_of)
        seen += p["items"]
        if p["next_cursor"] is None:
            break
        after = decode_cursor(p["next_cursor"])[0]
    assert seen == data


# ---------------------------------------------------------------- TokenBucket
@pytest.fixture()
def clock(monkeypatch):
    c = SimpleNamespace(now=5000.0)
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: c.now))
    return c


def test_bucket_sizes():
    assert (TokenBucket(600).rate, TokenBucket(600).burst) == (10.0, 150.0)
    assert TokenBucket(12).burst == 10.0, "never less than ten"
    assert TokenBucket(10, burst=5).burst == 5.0


def test_burst_is_allowed_then_requests_are_refused(clock):
    b = TokenBucket(60, burst=5)
    assert [b.take("caller")[0] for _ in range(5)] == [True] * 5
    ok, wait = b.take("caller")
    assert ok is False
    assert wait == pytest.approx(1.0), "one token per second at 60 per minute"


def test_tokens_come_back_with_time(clock):
    b = TokenBucket(60, burst=5)
    for _ in range(5):
        b.take("caller")
    clock.now += 0.5
    ok, wait = b.take("caller")
    assert ok is False and wait == pytest.approx(0.5)
    clock.now += 0.5
    assert b.take("caller") == (True, 0.0)
    assert b.take("caller")[0] is False


def test_a_refused_request_costs_nothing(clock):
    b = TokenBucket(60, burst=2)
    b.take("caller")
    b.take("caller")
    for _ in range(50):
        assert b.take("caller")[0] is False
    clock.now += 1.0
    assert b.take("caller")[0] is True


def test_bucket_never_holds_more_than_the_burst(clock):
    b = TokenBucket(60, burst=3)
    b.take("caller")
    clock.now += 3600.0
    assert [b.take("caller")[0] for _ in range(4)] == [True, True, True, False]


def test_callers_have_separate_buckets(clock):
    b = TokenBucket(60, burst=2)
    assert [b.take("a")[0] for _ in range(3)] == [True, True, False]
    assert [b.take("b")[0] for _ in range(3)] == [True, True, False]


def test_cost(clock):
    b = TokenBucket(60, burst=10)
    assert b.take("caller", cost=7.0) == (True, 0.0)
    ok, wait = b.take("caller", cost=7.0)
    assert ok is False and wait == pytest.approx(4.0)
    assert b.take("caller", cost=3.0)[0] is True


def test_sustained_rate_matches_the_limit(clock):
    b = TokenBucket(120, burst=10)
    served = 0
    for _ in range(60 * 20):                              # one minute, 20 attempts per second
        served += b.take("caller")[0]
        clock.now += 0.05
    assert served == pytest.approx(10 + 120, abs=2)


def test_memory_is_bounded_by_the_number_of_callers(clock):
    b = TokenBucket(60, burst=5, max_keys=100)
    for i in range(1000):
        b.take(f"caller-{i}")
        assert len(b.state) <= 100
    assert "caller-999" in b.state, "the newest caller is kept"
    assert "caller-0" not in b.state, "the oldest are forgotten"


# ---------------------------------------------------------------- middlewares
def make_app(*middlewares) -> FastAPI:
    app = FastAPI()
    for m in middlewares:
        app.add_middleware(m)

    @app.get("/api/v1/vehicles")
    def vehicles():
        return {"items": []}

    @app.post("/api/v1/auth/token")
    def token():
        return {"access_token": "x"}

    @app.get("/api/v1/system/health")
    def health():
        return {"ok": True}

    @app.get("/api/v1/cached")
    def cached():
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True}, headers={"Cache-Control": "max-age=60", "X-Frame-Options": "SAMEORIGIN"})

    @app.get("/index.html")
    def index():
        return {"page": "spa"}

    return app


def test_security_headers_on_api_responses():
    r = TestClient(make_app(support.SecurityHeadersMiddleware)).get("/api/v1/vehicles")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "geolocation=()" in r.headers["Permissions-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_security_headers_on_pages():
    r = TestClient(make_app(support.SecurityHeadersMiddleware)).get("/index.html")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp and "style-src 'self';" in csp
    assert "Cache-Control" not in r.headers
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_security_headers_do_not_override_what_a_route_set():
    r = TestClient(make_app(support.SecurityHeadersMiddleware)).get("/api/v1/cached")
    assert r.headers["Cache-Control"] == "max-age=60"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.fixture()
def limited(monkeypatch) -> TestClient:
    monkeypatch.setenv("ROSETTA_RATE_LIMIT_PER_MIN", "60")           # burst of 15
    config.reset_settings()
    return TestClient(make_app(support.RateLimitMiddleware))


def test_rate_limit_answers_429_with_problem_details(limited):
    codes = [limited.get("/api/v1/vehicles").status_code for _ in range(16)]
    assert codes == [200] * 15 + [429]
    r = limited.get("/api/v1/vehicles")
    assert r.status_code == 429
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert (body["title"], body["status"], body["detail"]) == ("Too many requests", 429, "rate limit exceeded")
    assert 0 < body["retry_after_s"] <= 1.0
    assert r.headers["Retry-After"] == "1"


def test_rate_limit_is_per_token(limited):
    alice = {"Authorization": "Bearer " + "a" * 40}
    bob = {"Authorization": "Bearer " + "b" * 40}
    assert [limited.get("/api/v1/vehicles", headers=alice).status_code for _ in range(16)][-1] == 429
    assert limited.get("/api/v1/vehicles", headers=bob).status_code == 200
    assert limited.get("/api/v1/vehicles").status_code == 200, "anonymous callers are counted by address"


def test_login_attempts_have_a_limit_of_their_own(limited):
    codes = [limited.post("/api/v1/auth/token").status_code for _ in range(42)]
    assert codes == [200] * 40 + [429, 429], "forty per address, then the account limit in the endpoint"
    assert limited.get("/api/v1/vehicles").status_code == 200, "the API limit is separate"


def test_health_checks_and_pages_are_never_limited(limited):
    assert {limited.get("/api/v1/system/health").status_code for _ in range(40)} == {200}
    assert {limited.get("/index.html").status_code for _ in range(40)} == {200}


# ---------------------------------------------------------------------- audit
class Recorder:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.fail = False

    def record(self, session, **kw):
        if self.fail:
            raise RuntimeError("database is down")
        self.entries.append(kw)


@pytest.fixture()
def audit_log(monkeypatch) -> Recorder:
    rec = Recorder()

    @contextmanager
    def fake_scope():
        yield object()

    monkeypatch.setattr(support, "session_scope", fake_scope)
    monkeypatch.setattr(support.audit, "record", rec.record)
    monkeypatch.setattr(support, "_recent", {})
    return rec


def request(path: str, method: str = "GET", principal: Any | None = None, query: dict | None = None,
            host: str | None = "10.0.0.7") -> Any:
    state = SimpleNamespace()
    if principal is not None:
        state.principal = principal
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method, state=state,
                           client=SimpleNamespace(host=host) if host else None,
                           query_params=query or {})


ANALYST = SimpleNamespace(email="analyst@rosetta.example", tenant_id=None)
MANAGER = SimpleNamespace(email="manager@northwind.example", tenant_id=3)


def test_read_of_vehicle_data_is_audited(audit_log):
    support.audit_request(request("/api/v1/vehicles/1HGCM82633A004352", principal=MANAGER, query={"limit": "50"}), 200)
    assert audit_log.entries == [{
        "actor_kind": "user", "actor": "manager@northwind.example", "tenant_id": 3, "action": "api.get",
        "resource": "/api/v1/vehicles/1HGCM82633A004352", "outcome": "ok", "ip": "10.0.0.7",
        "detail": {"status": 200, "query": {"limit": "50"}}}]


@pytest.mark.parametrize("path", ["/api/v1/system/overview", "/api/v1/metrics/series", "/api/v1/auth/token",
                                  "/api/v1/dlq/groups", "/metrics", "/index.html"])
def test_operational_endpoints_are_not_audited(audit_log, path):
    support.audit_request(request(path, principal=ANALYST), 200)
    assert audit_log.entries == []


@pytest.mark.parametrize("path", ["/api/v1/audit/verify", "/api/v1/compliance/policy"])
def test_exempt_paths_inside_audited_areas(audit_log, path):
    support.audit_request(request(path, principal=ANALYST), 200)
    assert audit_log.entries == []


def test_repeated_identical_reads_are_recorded_once_per_window(audit_log, clock):
    for _ in range(30):
        support.audit_request(request("/api/v1/map", principal=ANALYST), 200)
        clock.now += 1.9
    assert len(audit_log.entries) == 1
    clock.now += 10.0
    support.audit_request(request("/api/v1/map", principal=ANALYST), 200)
    assert len(audit_log.entries) == 2


def test_the_window_is_per_caller_and_per_path(audit_log, clock):
    support.audit_request(request("/api/v1/map", principal=ANALYST), 200)
    support.audit_request(request("/api/v1/map", principal=MANAGER), 200)
    support.audit_request(request("/api/v1/alerts", principal=ANALYST), 200)
    support.audit_request(request("/api/v1/map"), 200)
    assert [(e["actor"], e["resource"]) for e in audit_log.entries] == [
        ("analyst@rosetta.example", "/api/v1/map"), ("manager@northwind.example", "/api/v1/map"),
        ("analyst@rosetta.example", "/api/v1/alerts"), ("anonymous", "/api/v1/map")]


@pytest.mark.parametrize("status,outcome", [(401, "denied"), (403, "denied"), (404, "error"), (500, "error")])
def test_failed_reads_are_always_recorded(audit_log, clock, status, outcome):
    for _ in range(3):
        support.audit_request(request("/api/v1/vehicles", principal=ANALYST), status)
    assert [e["outcome"] for e in audit_log.entries] == [outcome] * 3


def test_anonymous_caller(audit_log):
    support.audit_request(request("/api/v1/vehicles", host=None), 401)
    entry = audit_log.entries[0]
    assert (entry["actor_kind"], entry["actor"], entry["tenant_id"], entry["ip"]) == ("anonymous", "anonymous", None, "")


def test_successful_mutations_write_their_own_entry_elsewhere(audit_log):
    support.audit_request(request("/api/v1/mappings/helix", method="POST", principal=ANALYST), 201)
    support.audit_request(request("/api/v1/dlq/replay", method="POST", principal=ANALYST), 202)
    assert audit_log.entries == []


def test_failed_mutations_are_recorded(audit_log):
    support.audit_request(request("/api/v1/mappings/helix", method="POST", principal=ANALYST), 403)
    assert [(e["action"], e["outcome"]) for e in audit_log.entries] == [("api.post", "denied")]


def test_ingest_is_recorded_although_it_is_a_post(audit_log):
    support.audit_request(request("/api/v1/ingest/helix", method="POST", principal=ANALYST), 202)
    assert [(e["action"], e["outcome"]) for e in audit_log.entries] == [("api.post", "ok")]


def test_only_the_first_eight_query_parameters_are_kept(audit_log):
    query = {f"p{i}": str(i) for i in range(20)}
    support.audit_request(request("/api/v1/vehicles", principal=ANALYST, query=query), 200)
    assert audit_log.entries[0]["detail"]["query"] == {f"p{i}": str(i) for i in range(8)}


def test_audit_failure_never_breaks_the_request(audit_log):
    audit_log.fail = True
    support.audit_request(request("/api/v1/vehicles", principal=ANALYST), 200)
    assert audit_log.entries == []


def test_recent_read_table_is_bounded(audit_log, clock):
    support._recent.update({(f"user{i}", "/api/v1/map"): clock.now for i in range(20_001)})
    support.audit_request(request("/api/v1/vehicles", principal=ANALYST), 200)
    assert list(support._recent) == [("analyst@rosetta.example", "/api/v1/vehicles")]


def test_audit_middleware_audits_api_calls_only(monkeypatch):
    calls = []
    monkeypatch.setattr(support, "audit_request", lambda req, status: calls.append((req.url.path, status)))
    client = TestClient(make_app(support.AuditMiddleware))
    assert client.get("/api/v1/vehicles").status_code == 200
    assert client.get("/index.html").status_code == 200
    assert client.get("/api/v1/nothing-here").status_code == 404
    assert calls == [("/api/v1/vehicles", 200), ("/api/v1/nothing-here", 404)]
