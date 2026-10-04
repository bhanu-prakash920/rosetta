"""Security tests, organised by the OWASP API Security Top 10 (2023)."""
from __future__ import annotations

import time

import jwt
import pytest

from tests.support import PASSWORD

PUBLIC = {("post", "/api/v1/auth/token"), ("get", "/api/v1/system/health"), ("get", "/api/v1/system/live")}


# ------------------------------------------- API1: broken object level authorisation
def test_other_tenants_vehicle_is_invisible(api, manager, other_manager):
    mine = api.get("/api/v1/vehicles?limit=5", headers=manager).json()["items"]
    theirs = api.get("/api/v1/vehicles?limit=5", headers=other_manager).json()["items"]
    assert {v["tenant_id"] for v in mine} == {1} and {v["tenant_id"] for v in theirs} == {2}
    vin = theirs[0]["vin"]
    for path in (f"/api/v1/vehicles/{vin}", f"/api/v1/vehicles/{vin}/history", f"/api/v1/vehicles/{vin}/trips"):
        r = api.get(path, headers=manager)
        assert r.status_code == 404, path           # 404, not 403: existence is not revealed
        assert vin not in r.text


def test_other_tenants_driver_cannot_be_read_or_erased(api, manager, other_manager):
    theirs = api.get("/api/v1/drivers?limit=3", headers=other_manager).json()["items"]
    mine = {d["id"] for d in api.get("/api/v1/drivers?limit=100", headers=manager).json()["items"]}
    assert theirs and not mine & {d["id"] for d in theirs}
    r = api.post("/api/v1/compliance/erasure", json={"driver_id": theirs[0]["id"]}, headers=manager)
    assert r.status_code == 404
    still = api.get("/api/v1/drivers?limit=3", headers=other_manager).json()["items"][0]
    assert still["full_name"] == theirs[0]["full_name"]


def test_alerts_and_map_are_tenant_scoped(api, manager, engineer):
    own = {v["vin"] for v in _all_vehicles(api, manager)}
    pts = api.get("/api/v1/map/points?limit=20000", headers=manager).json()
    assert set(pts["vins"]) <= own
    for a in api.get("/api/v1/alerts?limit=200", headers=manager).json()["items"]:
        assert a["vin"] in own


def _all_vehicles(api, h):
    out, cur = [], None
    while True:
        j = api.get("/api/v1/vehicles?limit=200" + (f"&cursor={cur}" if cur else ""), headers=h).json()
        out += j["items"]
        cur = j["next_cursor"]
        if not cur:
            return out


# ------------------------------------------------------- API2: broken authentication
def _token(claims, key=None, alg="HS256"):
    from rosetta.api import security

    base = {"iss": "rosetta", "sub": "1", "email": "x@rosetta.example", "roles": ["admin"],
            "iat": int(time.time()), "nbf": int(time.time()), "exp": int(time.time()) + 600}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, key or security.secret(), algorithm=alg)


@pytest.mark.parametrize("name,make", [
    ("no token", lambda: None),
    ("garbage", lambda: "not.a.token"),
    ("alg none", lambda: "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
                         "eyJzdWIiOiIxIiwicm9sZXMiOlsiYWRtaW4iXSwiZXhwIjo5OTk5OTk5OTk5fQ."),
    ("wrong key", lambda: _token({}, key="k" * 40)),
    ("expired", lambda: _token({"exp": int(time.time()) - 5})),
    ("wrong issuer", lambda: _token({"iss": "someone-else"})),
    ("no expiry", lambda: _token({"exp": None})),
    ("no subject", lambda: _token({"sub": None})),
    ("other algorithm", lambda: _token({}, alg="HS512")),
])
def test_bad_tokens_are_rejected(api, name, make):
    tok = make()
    h = {"Authorization": f"Bearer {tok}"} if tok else {}
    r = api.get("/api/v1/overview", headers=h)
    assert r.status_code == 401, name
    assert r.headers.get("www-authenticate") == "Bearer"


def test_tampered_roles_do_not_verify(api, analyst):
    import base64
    import json

    head, body, sig = analyst["Authorization"].split()[1].split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["roles"] = ["admin"]
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    r = api.get("/api/v1/drivers", headers={"Authorization": f"Bearer {head}.{forged}.{sig}"})
    assert r.status_code == 401


def test_login_failures_look_the_same(api):
    a = api.post("/api/v1/auth/token", data={"username": "engineer@rosetta.example", "password": "wrong"})
    b = api.post("/api/v1/auth/token", data={"username": "nobody@rosetta.example", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"] == "invalid credentials"


def test_passwords_are_hashed_with_argon2(api):
    from sqlalchemy import select

    from rosetta.db.models import AppUser
    from rosetta.db.session import session_scope

    with session_scope() as s:
        hashes = [h for (h,) in s.execute(select(AppUser.password_hash))]
    assert hashes and all(h.startswith("$argon2id$") and PASSWORD not in h for h in hashes)


def test_signing_key_policy(monkeypatch):
    from rosetta import config
    from rosetta.api import security

    monkeypatch.setenv("ROSETTA_JWT_SECRET", "short")
    config.reset_settings()
    with pytest.raises(RuntimeError, match="at least 32"):
        security.secret()
    monkeypatch.delenv("ROSETTA_JWT_SECRET")
    monkeypatch.setenv("ROSETTA_ENV", "production")
    config.reset_settings()
    with pytest.raises(RuntimeError, match="required outside local"):
        security.secret()
    monkeypatch.setenv("ROSETTA_ENV", "test")
    config.reset_settings()


# --------------------------- API3: broken object property level authorisation
def test_analyst_gets_masked_data(api, analyst, engineer):
    from rosetta.algorithms import geohash

    v = api.get("/api/v1/vehicles?limit=50", headers=analyst).json()["items"]
    assert all(x["vin"].startswith("•" * 11) and x["vin_ref"] is None for x in v)
    live = [x["live"] for x in v if x.get("live")]
    assert live and all(pos["location_masked"] for pos in live)
    for pos in live:
        la, lo = geohash.decode(pos["geohash"])
        assert len(pos["geohash"]) == 5 and abs(la - pos["lat"]) < 1e-3 and abs(lo - pos["lon"]) < 1e-3
    pts = api.get("/api/v1/map/points?limit=2000", headers=analyst).json()
    assert pts["masked"] and "vins" not in pts
    precise = api.get("/api/v1/map/points?limit=2000", headers=engineer).json()
    masked, exact = {(p[0], p[1]) for p in pts["points"]}, {(p[0], p[1]) for p in precise["points"]}
    assert len(masked) < len(exact) and not masked & exact
    for la, lo in list(masked)[:50]:                       # every masked point is a cell centre
        c_la, c_lo = geohash.decode(geohash.encode(la, lo, 5))
        assert abs(c_la - la) < 1e-4 and abs(c_lo - lo) < 1e-4
    for a in api.get("/api/v1/alerts?limit=50", headers=analyst).json()["items"]:
        assert a["vin"].startswith("•") and a["detail"].get("location_masked", True)


def test_unknown_fields_are_refused_not_ignored(api, engineer):
    for path, body in (("/api/v1/agent/runs", {"oem": "helix", "approve": True}),
                       ("/api/v1/dlq/replay", {"oem": "helix", "state": "active"}),
                       ("/api/v1/compliance/erasure", {"driver_id": 1, "tenant_id": 2}),
                       ("/api/v1/simulator", {"drift_pct": 1, "is_admin": True})):
        r = api.post(path, json=body, headers=engineer)
        assert r.status_code in (403, 422), path
    r = api.post("/api/v1/mappings/nordvik/1/actions/retire", json={"state": "active", "comment": "x"}, headers=engineer)
    assert r.status_code == 422


# ------------------------------------ API4: unrestricted resource consumption
@pytest.mark.parametrize("path", [
    "/api/v1/vehicles?limit=100000", "/api/v1/alerts?limit=5000", "/api/v1/audit?limit=100000",
    "/api/v1/map/points?limit=10000000", "/api/v1/series?seconds=100000", "/api/v1/map/cells?precision=12",
    "/api/v1/vehicles?limit=0", "/api/v1/vehicles?limit=-1",
])
def test_limits_are_capped(api, engineer, path):
    assert api.get(path, headers=engineer).status_code == 422


def test_large_bodies_are_refused(api, admin, engineer):
    h = {**admin, "X-Device-Id": "d1", "Content-Type": "application/octet-stream"}
    assert api.post("/api/v1/ingest/nordvik", content=b"x" * (5 << 20), headers=h).status_code == 413
    r = api.post("/api/v1/mappings/nordvik/1/preview", json={"payload": "x" * 400_000}, headers=engineer)
    assert r.status_code == 422


def test_rate_limit_returns_429_with_retry_after():
    from rosetta.api.support import TokenBucket

    b = TokenBucket(per_minute=60, burst=5)
    results = [b.take("caller")[0] for _ in range(8)]
    assert results == [True] * 5 + [False] * 3
    ok, wait = b.take("caller")
    assert not ok and 0 < wait <= 1.0
    assert b.take("someone-else")[0]                   # one caller cannot exhaust another's budget


def test_login_is_rate_limited(api):
    codes = [api.post("/api/v1/auth/token", data={"username": "a@b.example", "password": "x"}).status_code
             for _ in range(8)]
    assert codes == [401] * 5 + [429] * 3
    r = api.post("/api/v1/auth/token", data={"username": "a@b.example", "password": "x"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    # another account from the same address is not affected
    assert api.post("/api/v1/auth/token", data={"username": "c@d.example", "password": "x"}).status_code == 401


# ------------------------------- API5: broken function level authorisation
WRITES = [
    ("post", "/api/v1/agent/runs", {"oem": "helix"}),
    ("post", "/api/v1/mappings/nordvik", {"decoder": {"type": "json"}, "fields": {}}),
    ("post", "/api/v1/mappings/nordvik/1/validate", {}),
    ("post", "/api/v1/mappings/nordvik/1/actions/retire", {}),
    ("post", "/api/v1/mappings/nordvik/1/actions/approve", {}),
    ("post", "/api/v1/mappings/nordvik/1/shadow", {"max_records": 100}),
    ("post", "/api/v1/dlq/replay", {"oem": "helix"}),
    ("post", "/api/v1/simulator", {"paused": True}),
    ("post", "/api/v1/simulator/scenario/pause", None),
    ("post", "/api/v1/system/chaos/kill", {"name": "normalizer-0"}),
]


@pytest.mark.parametrize("method,path,body", WRITES)
def test_read_only_roles_cannot_change_anything(api, analyst, manager, method, path, body):
    for who in (analyst, manager):
        r = api.request(method, path, json=body, headers=who)
        assert r.status_code == 403, (path, r.status_code)
    assert api.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/mappings", "/api/v1/mappings/nordvik/1", "/api/v1/dlq/groups",
                                  "/api/v1/audit", "/api/v1/agent/runs", "/api/v1/batch/quality",
                                  "/api/v1/system/workers"])
def test_fleet_manager_cannot_see_platform_internals(api, manager, path):
    assert api.get(path, headers=manager).status_code == 403


def test_personal_data_needs_the_right_role(api, analyst, engineer):
    for who in (analyst, engineer):
        assert api.get("/api/v1/drivers", headers=who).status_code == 403
        assert api.post("/api/v1/compliance/erasure", json={"driver_id": 1}, headers=who).status_code == 403


# --------------------------------------------- API7: server side request forgery
def test_no_endpoint_takes_a_url(api):
    spec = api.get("/api/openapi.json").json()
    risky = {"url", "uri", "callback", "webhook", "redirect", "next", "target", "host"}
    for path, ops in spec["paths"].items():
        for op in ops.values():
            for p in op.get("parameters", []):
                assert p["name"].lower() not in risky, (path, p["name"])
    for name, schema in spec["components"]["schemas"].items():
        assert not risky & {k.lower() for k in schema.get("properties", {})}, name


# ------------------------------------------------ API8: security misconfiguration
def test_security_headers(api, engineer):
    h = api.get("/api/v1/overview", headers=engineer).headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["cache-control"] == "no-store" and h["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in h["content-security-policy"]
    assert "x-request-id" in h and "server" not in {k.lower() for k in h if h[k].lower().startswith("uvicorn")}


def test_api_docs_page_has_its_own_narrow_csp(api):
    """Swagger UI works in a browser, without loosening the API's default-src 'none'."""
    import base64
    import hashlib
    import re

    r = api.get("/api/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'") and "frame-ancestors 'none'" in csp
    script_src = re.search(r"script-src ([^;]+)", csp).group(1).split()
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    for inline in re.findall(r"<script>(.*?)</script>", r.text, flags=re.S):   # every inline script is allowed by hash
        digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in script_src
    for src in re.findall(r'<script src="([^"]+)"', r.text):                  # and external ones only from the CDN path
        assert any(src.startswith(s) for s in script_src if s.startswith("https://"))
    other = api.get("/api/openapi.json").headers["content-security-policy"]
    assert other == "default-src 'none'; frame-ancestors 'none'", "the rest of the API keeps the strict policy"


def test_cors_is_an_allow_list(api):
    evil = api.options("/api/v1/overview", headers={"Origin": "https://evil.example",
                                                    "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers
    good = api.options("/api/v1/overview", headers={"Origin": "http://localhost:5173",
                                                    "Access-Control-Request-Method": "GET"})
    assert good.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert good.headers.get("access-control-allow-credentials") != "true"


def test_errors_do_not_leak_internals(api, engineer):
    for r in (api.get("/api/v1/mappings/helix/abc", headers=engineer),
              api.get("/api/v1/vehicles?cursor=%00%00", headers=engineer),
              api.post("/api/v1/agent/runs", content=b"{not json", headers={**engineer, "Content-Type": "application/json"})):
        assert r.status_code in (400, 404, 422)
        low = r.text.lower()
        assert not any(x in low for x in ("traceback", "sqlalchemy", "sqlite", "/users/", "file \"", "rosetta/"))


# --------------------------------------------- API9: improper inventory management
def test_every_operation_requires_a_token_unless_listed(api):
    spec = api.get("/api/openapi.json").json()
    missing = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if (method, path) in PUBLIC:
                continue
            if not op.get("security"):
                missing.append((method, path))
    assert not missing, f"operations without authentication: {missing}"
    assert all(p.startswith("/api/v1/") for p in spec["paths"])


def test_unauthenticated_calls_get_401_everywhere(api):
    spec = api.get("/api/openapi.json").json()
    for path, ops in spec["paths"].items():
        for method in ops:
            if (method, path) in PUBLIC:
                continue
            url = path.replace("{oem}", "nordvik").replace("{version}", "1").replace("{action}", "retire") \
                .replace("{vin}", "1HGCM82633A004352").replace("{group_id}", "1").replace("{run_id}", "1") \
                .replace("{name}", "pause")
            r = api.request(method, url, json={})
            assert r.status_code == 401, (method, url, r.status_code)


# ------------------------------------------------------------------- injection
@pytest.mark.parametrize("q", ["' OR 1=1 --", "1; DROP TABLE vehicle", "%27%20UNION%20SELECT", "../../etc/passwd",
                               "<script>alert(1)</script>", "${jndi:ldap://x}", "{{7*7}}"])
def test_injection_strings_are_rejected_or_inert(api, engineer, manager, q):
    from sqlalchemy import func, select

    from rosetta.db.models import Vehicle
    from rosetta.db.session import session_scope

    for path in (f"/api/v1/vehicles?q={q}", f"/api/v1/vehicles?oem={q}", f"/api/v1/mappings?oem={q}",
                 f"/api/v1/audit?action={q}", f"/api/v1/dlq/groups?oem={q}"):
        r = api.get(path, headers=engineer)
        assert r.status_code in (200, 422), path
        if r.status_code == 200:
            assert not r.json().get("items")
        assert "<script>" not in r.text
    assert api.get(f"/api/v1/vehicles/{q}", headers=manager).status_code in (404, 405)
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(Vehicle)).scalar() == 3000


def test_static_route_cannot_escape_the_web_root(api):
    for p in ("/..%2f..%2fpyproject.toml", "/%2e%2e/%2e%2e/pyproject.toml", "/assets/../../pyproject.toml",
              "/....//....//etc/passwd"):
        r = api.get(p)
        assert "[project]" not in r.text and "root:" not in r.text


def test_payload_text_is_escaped_by_the_ui_highlighter():
    """The console renders payloads with innerHTML after escaping. Check the escaping rule."""
    import re
    from pathlib import Path

    src = Path("web/src/lib/format.ts").read_text()
    body = src[src.index("export function highlight"):]
    first = body[body.index("const esc"):].split("\n")[0]
    assert all(x in first for x in ('replace(/&/g, "&amp;")', 'replace(/</g, "&lt;")', 'replace(/>/g, "&gt;")'))
    uses = [p for p in Path("web/src").rglob("*.tsx") if "dangerouslySetInnerHTML" in p.read_text()]
    assert [u.name for u in uses] == ["ui.tsx"]
    assert re.search(r"highlight\(pretty\(value\)\)", Path("web/src/components/ui.tsx").read_text())


# ----------------------------------------------------------------- audit trail
def test_reads_and_denials_are_audited(api, manager, analyst, admin):
    vin = api.get("/api/v1/vehicles?limit=1", headers=manager).json()["items"][0]["vin"]
    api.get(f"/api/v1/vehicles/{vin}/history", headers=manager)
    api.get("/api/v1/drivers", headers=analyst)
    rows = api.get("/api/v1/audit?limit=200", headers=admin).json()["items"]
    assert any(r["resource"] == f"/api/v1/vehicles/{vin}/history" and r["actor"] == "manager@northwind.example"
               and r["outcome"] == "ok" for r in rows)
    assert any(r["resource"] == "/api/v1/drivers" and r["outcome"] == "denied" for r in rows)
    assert any(r["action"] == "auth.login" for r in rows)
    assert api.get("/api/v1/audit/verify", headers=admin).json()["valid"] is True


def test_repeated_reads_are_recorded_once_per_window(api, manager, admin):
    for _ in range(5):
        api.get("/api/v1/map/cells?precision=3", headers=manager)
    rows = api.get("/api/v1/audit?limit=200", headers=admin).json()["items"]
    assert len([r for r in rows if r["resource"] == "/api/v1/map/cells"
                and r["actor"] == "manager@northwind.example"]) == 1


def test_secrets_never_reach_the_logs(capsys):
    import logging

    from rosetta.observability import logs

    root = logging.getLogger()
    old = root.handlers[:], getattr(root, "_rosetta", False)
    root._rosetta = False
    logs.configure("INFO")
    logging.getLogger("t").info("login", extra={"password": "hunter2", "api_key": "sk-abc", "user": "a@b.example",
                                                 "Authorization": "Bearer xyz"})
    out = capsys.readouterr().out
    root.handlers[:], root._rosetta = old
    assert "hunter2" not in out and "sk-abc" not in out and "xyz" not in out and "a@b.example" in out
