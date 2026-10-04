"""The HTTP API. `uvicorn rosetta.api.main:app`"""
from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config import get_settings
from ..observability.logs import configure as configure_logs
from . import state as st
from .routers import agent, auth, batch, compliance, dlq, fleet, ingest, ops, registry
from .support import TITLES, AuditMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware, problem, swagger_page

log = logging.getLogger("rosetta.api")

DESCRIPTION = """
Rosetta translates vehicle telemetry from many car makers into one canonical event.

* **Authentication** OAuth2 bearer tokens (JWT). Get one from `POST /api/v1/auth/token`.
* **Errors** RFC 9457 problem details (`application/problem+json`).
* **Pagination** keyset cursors: pass `next_cursor` back as `cursor`. No offsets.
* **Rate limits** per caller. A 429 carries `Retry-After`.
* **Versioning** the path carries the major version. Additive changes do not bump it.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logs()
    st.startup()
    log.info("api started", extra={"vehicles": len(st.state.vins)})
    yield
    st.shutdown()


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="Rosetta API", version=__version__, description=DESCRIPTION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")

    # Swagger UI with its own Content-Security-Policy. The API's policy is
    # default-src 'none', which would block this page; loosening it for every
    # response is not needed. The page's one inline script is fixed, so it is
    # allowed by its hash, and scripts and styles come only from the pinned CDN path.
    docs_html, docs_csp = swagger_page(app.title)

    @app.get("/api/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        return HTMLResponse(docs_html, headers={"Content-Security-Policy": docs_csp, "Cache-Control": "no-store"})

    # Order matters: the last one added runs first.
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    origins = [o.strip() for o in cfg.cors_origins.split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "X-Device-Id"],
                       max_age=600)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
            resp = problem(500, "Internal error", "the request could not be completed", request_id=rid)
        ms = (time.perf_counter() - t0) * 1000.0
        resp.headers["X-Request-Id"] = rid
        resp.headers["Server-Timing"] = f"app;dur={ms:.1f}"
        if request.url.path.startswith("/api/") and not request.url.path.endswith(("/overview", "/series", "/health")):
            log.info("request", extra={"request_id": rid, "method": request.method, "path": request.url.path,
                                       "status": resp.status_code, "ms": round(ms, 1)})
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, e: StarletteHTTPException):
        r = problem(e.status_code, TITLES.get(e.status_code, "Error"), str(e.detail))
        for k, v in (e.headers or {}).items():
            r.headers[k] = v
        return r

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError):
        errs = [{"field": ".".join(str(p) for p in x["loc"][1:]) or str(x["loc"][0]), "problem": x["msg"]}
                for x in e.errors()[:20]]
        return problem(422, "Unprocessable content", "the request did not pass validation", errors=errs)

    for r in (auth.router, ops.router, registry.router, dlq.router, agent.router, fleet.router,
              compliance.router, ingest.router, batch.router):
        app.include_router(r, prefix="/api/v1")

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request):
        # With ROSETTA_METRICS_TOKEN set, only a scraper holding the token may read.
        want = os.environ.get("ROSETTA_METRICS_TOKEN", "")
        if want and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {want}"):
            return PlainTextResponse("unauthorised\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return ops.prometheus()

    dist = Path(cfg.web_dist).resolve()
    if (dist / "index.html").exists():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        for sub in ("img", "fonts"):
            if (dist / sub).is_dir():
                app.mount(f"/{sub}", StaticFiles(directory=dist / sub), name=sub)

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "metrics")):
                raise HTTPException(404, "not found")
            f = (dist / path).resolve()
            if path and f.is_file() and dist in f.parents:      # no escaping the web root
                return FileResponse(f)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return JSONResponse({"service": "rosetta", "version": __version__, "docs": "/api/docs",
                                 "note": "web UI not built: run `npm run build` in web/"})

    return app


app = create_app()
