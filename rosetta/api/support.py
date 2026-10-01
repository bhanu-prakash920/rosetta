"""Cross-cutting API pieces: errors, rate limiting, pagination, audit, security headers."""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

import orjson
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import get_settings
from ..db.session import session_scope
from ..services import audit

PROBLEM = "application/problem+json"


def problem(status: int, title: str, detail: str = "", **extra: Any) -> JSONResponse:
    """RFC 9457 problem details: one error shape for every failure."""
    body = {"type": f"https://rosetta.example/problems/{title.lower().replace(' ', '-')}",
            "title": title, "status": status, "detail": detail, **extra}
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


TITLES = {400: "Bad request", 401: "Unauthorized", 403: "Forbidden", 404: "Not found", 409: "Conflict",
          413: "Payload too large", 422: "Unprocessable content", 429: "Too many requests",
          503: "Service unavailable"}


# ------------------------------------------------------------------ pagination
def encode_cursor(*parts: Any) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(list(parts))).decode().rstrip("=")


def decode_cursor(cursor: str | None, n: int = 1) -> list[Any] | None:
    """Keyset cursor. Opaque to clients, validated here: a forged cursor is a 400, not a 500."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parts = orjson.loads(raw)
    except Exception:
        raise HTTPException(400, "invalid cursor") from None
    if not isinstance(parts, list) or len(parts) != n or not all(isinstance(p, (int, float, str)) for p in parts):
        raise HTTPException(400, "invalid cursor")
    return parts


def page(items: list[Any], limit: int, cursor_of) -> dict[str, Any]:
    """`items` holds limit + 1 rows when there is a next page."""
    more = len(items) > limit
    items = items[:limit]
    return {"items": items, "next_cursor": cursor_of(items[-1]) if more and items else None, "limit": limit}


# --------------------------------------------------------------- rate limiting
class TokenBucket:
    """Per-caller token bucket. O(1) per request, memory bounded by the caller count."""

    def __init__(self, per_minute: int, burst: int | None = None, max_keys: int = 50_000) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(burst or max(10, per_minute // 4))
        self.max_keys = max_keys
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def take(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.state.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self.state[key] = (tokens - cost, now)
                ok, wait = True, 0.0
            else:
                self.state[key] = (tokens, now)
                ok, wait = False, (cost - tokens) / self.rate
            if len(self.state) > self.max_keys:
                for k in list(self.state)[: self.max_keys // 10]:
                    del self.state[k]
        return ok, wait


SKIP_LIMIT = ("/api/v1/system/health", "/api/v1/system/live", "/metrics", "/assets/", "/img/")
LOGIN_PATH = "/api/v1/auth/token"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any) -> None:
        super().__init__(app)
        st = get_settings()
        self.api = TokenBucket(st.rate_limit_per_min)
        self.login = TokenBucket(120, burst=40)   # per address; the per-account limit is in the login endpoint

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path.startswith(SKIP_LIMIT):
            return await call_next(request)
        ip = request.client.host if request.client else "?"
        if path == LOGIN_PATH:
            ok, wait = self.login.take(f"login:{ip}")
        else:
            auth = request.headers.get("authorization", "")
            # The token signature identifies the caller without decoding it here.
            key = f"t:{auth[-24:]}" if auth else f"ip:{ip}"
            ok, wait = self.api.take(key)
        if not ok:
            r = problem(429, "Too many requests", "rate limit exceeded", retry_after_s=round(wait, 1))
            r.headers["Retry-After"] = str(max(1, int(wait + 0.999)))
            return r
        return await call_next(request)


# -------------------------------------------------------------------- headers
SWAGGER_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"


def swagger_page(title: str) -> tuple[str, str]:
    """The Swagger UI page and a CSP that allows exactly it: the CDN files and the
    hash of each inline script."""
    import hashlib
    import re

    from fastapi.openapi.docs import get_swagger_ui_html

    html = get_swagger_ui_html(openapi_url="/api/openapi.json", title=f"{title} - API",
                               swagger_js_url=f"{SWAGGER_CDN}/swagger-ui-bundle.js",
                               swagger_css_url=f"{SWAGGER_CDN}/swagger-ui.css",
                               swagger_favicon_url="data:,").body.decode()
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
        for s in re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    csp = (f"default-src 'none'; script-src {SWAGGER_CDN}/ {hashes}; "
           # Swagger UI sets style attributes from its bundle; that needs 'unsafe-inline' for styles only.
           f"style-src {SWAGGER_CDN}/ 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    return html, csp


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        # API responses never need a referrer. Pages send only their origin, which map
        # tile servers require (OpenStreetMap blocks requests without one).
        h.setdefault("Referrer-Policy", "no-referrer" if request.url.path.startswith("/api/")
                     else "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # Nothing here is meant to be embedded by another site.
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
            h.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        else:
            h.setdefault("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data: https://server.arcgisonline.com https://tile.openstreetmap.org; "
                         # No 'unsafe-inline': React and Leaflet set styles through the DOM, which CSP allows.
                         # Fonts are bundled (fontsource), so no font CDN either.
                         "style-src 'self'; font-src 'self'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return resp


# ---------------------------------------------------------------------- audit
# Reads of data about vehicles and people are audited. Operational metrics are not:
# they contain no personal data and are polled every second.
# Repeated identical reads by one caller are recorded once per window. A map that
# refreshes every two seconds is one act of looking, not thirty.
READ_WINDOW_S = 60.0
_recent: dict[tuple[str, str], float] = {}
NOT_AUDITED = ("/api/v1/audit/verify", "/api/v1/compliance/policy")
AUDITED_PREFIXES = ("/api/v1/vehicles", "/api/v1/map", "/api/v1/alerts", "/api/v1/drivers",
                    "/api/v1/compliance", "/api/v1/audit", "/api/v1/dlq/groups/", "/api/v1/batch",
                    "/api/v1/mappings", "/api/v1/agent", "/api/v1/simulator", "/api/v1/system/chaos",
                    "/api/v1/dlq/replay", "/api/v1/ingest")


def audit_request(request: Request, status: int) -> None:
    path = request.url.path
    method = request.method
    if not path.startswith(AUDITED_PREFIXES) or path.startswith(NOT_AUDITED):
        return
    # Mutations write their own, richer entry inside the business transaction.
    if method != "GET" and status < 400 and not path.startswith("/api/v1/ingest"):
        return
    p = getattr(request.state, "principal", None)
    outcome = "ok" if status < 400 else "denied" if status in (401, 403) else "error"
    if method == "GET" and outcome == "ok":
        key = (p.email if p else "anonymous", path)
        now = time.monotonic()
        if now - _recent.get(key, -1e9) < READ_WINDOW_S:
            return
        if len(_recent) > 20_000:
            _recent.clear()
        _recent[key] = now
    AUDIT.submit(dict(actor_kind="user" if p else "anonymous", actor=p.email if p else "anonymous",
                      tenant_id=p.tenant_id if p else None, action=f"api.{method.lower()}", resource=path,
                      outcome=outcome, ip=request.client.host if request.client else "",
                      detail={"status": status, "query": dict(list(request.query_params.items())[:8])}))


class AuditWriter:
    """Writes read-audit entries in the background, many per transaction.

    A read must not wait for a database write lock: under load that wait was the
    API's p95. Entries are queued and flushed every 200 ms in one transaction.
    Mutations are not affected: they write their audit entry inside their own
    transaction. The trade-off, stated: if the API process is killed, up to 200 ms
    of read-audit entries can be lost. Reading the audit log flushes first, so it
    is always complete from the reader's point of view.
    """

    def __init__(self, interval_s: float = 0.2) -> None:
        import queue

        self.q: queue.Queue[dict] = queue.Queue(maxsize=100_000)
        self.interval_s = interval_s
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.dropped = 0

    def submit(self, entry: dict) -> None:
        if self._thread is None:            # no background thread (tests, scripts): write now
            self._write([entry])
            return
        try:
            self.q.put_nowait(entry)
        except Exception:
            self.dropped += 1

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="audit-writer")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.flush()

    def flush(self) -> int:
        with self.lock:
            batch = []
            while len(batch) < 5000:
                try:
                    batch.append(self.q.get_nowait())
                except Exception:
                    break
            if batch:
                self._write(batch)
            return len(batch)

    @staticmethod
    def _write(entries: list[dict]) -> None:
        try:
            with audit.CHAIN_LOCK, session_scope() as s:
                for e in entries:
                    audit.record(s, **e)
        except Exception:
            pass   # an audit failure must not turn a served request into an error


AUDIT = AuditWriter()


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/api/"):
            import anyio

            await anyio.to_thread.run_sync(audit_request, request, resp.status_code)
        return resp
