"""HTTP intake for OEM clouds that push over HTTPS instead of MQTT."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ...engine.decoders import MAX_PAYLOAD_BYTES
from ...pipeline.gateway import Backpressure, Gateway
from ...services import registry
from ..security import ADMIN, SERVICE, Principal, require
from ..state import read_db, state

router = APIRouter(tags=["ingest"])
MAX_BODY = 4 << 20
_gateway: Gateway | None = None
_known: dict[str, float] = {}


def gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        _gateway = Gateway(state.broker, lag_fn=lambda: state.agg.overview(3)["lag"]["normalizer"])
    return _gateway


@router.post("/ingest/{oem}", status_code=202,
             summary="Send raw payloads of one source, newline separated for text formats")
async def ingest(oem: str, request: Request, x_device_id: str = Header(..., max_length=64),
                 content_type: str = Header("application/octet-stream", max_length=80),
                 p: Principal = Depends(require(SERVICE, ADMIN))):
    now = time.monotonic()
    if now - _known.get(oem, 0.0) > 30.0:
        with read_db() as s:
            try:
                registry.get_oem(s, oem)
            except registry.RegistryError:
                raise HTTPException(404, "unknown source") from None
        _known[oem] = now
    cl = request.headers.get("content-length")
    if cl and int(cl) > MAX_BODY:
        raise HTTPException(413, f"body larger than {MAX_BODY} bytes")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, f"body larger than {MAX_BODY} bytes")
    text_like = content_type.startswith(("application/json", "application/x-ndjson", "text/"))
    parts = [b for b in body.split(b"\n") if b.strip()] if text_like else [body]
    items = [(x_device_id, b) for b in parts if len(b) <= MAX_PAYLOAD_BYTES * 4]
    try:
        n = gateway().submit(oem, items, content_type.split(";")[0])
    except Backpressure as bp:
        # Tell the producer to slow down. Its data is safe on its side until it retries.
        return JSONResponse({"title": "Too many requests", "status": 429,
                             "detail": "the pipeline is catching up, retry shortly"},
                            status_code=429, headers={"Retry-After": str(max(1, int(bp.retry_after_s + 0.999)))},
                            media_type="application/problem+json")
    return {"accepted": n, "rejected": len(parts) - n}
