#!/usr/bin/env python3
"""Container health check for every Rosetta role. Standard library only.

    rosetta-healthcheck            role detected from the command line of PID 1
    rosetta-healthcheck api        HTTP: GET /api/v1/system/health must answer status "ok"
    rosetta-healthcheck api-live   HTTP: the same endpoint must answer at all (liveness)
    rosetta-healthcheck normalizer the checkpoint file was written in the last MAX_AGE seconds
    rosetta-healthcheck worker     PID 1 is a Rosetta process

Why three kinds. The health endpoint answers 200 with status "degraded" when the
database or the broker is unreachable, so a plain HTTP probe cannot tell ready
from not ready: the body has to be read. The normaliser writes its checkpoint
every two seconds whether or not there is traffic, which makes the age of that
file a real signal that the loop is turning. The gateway, processor and
dead-letter worker expose nothing, so for them the check can only say that the
process exists. That is weaker than it should be and is listed as a gap in
infra/VERIFICATION.md.

Exit code 0 healthy, 1 unhealthy.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("ROSETTA_HEALTH_URL",
                     f"http://127.0.0.1:{os.environ.get('ROSETTA_PORT', '8000')}/api/v1/system/health")
LIVE_URL = URL.replace("/system/health", "/system/live")
MAX_AGE_S = float(os.environ.get("ROSETTA_HEALTH_MAX_AGE_S", "60"))


def pid1() -> str:
    try:
        return Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def detect() -> str:
    # ROSETTA_SERVICE is set per service by Compose and by the Helm chart. It wins,
    # because PID 1 is not the service when the container runs under an init process.
    svc = os.environ.get("ROSETTA_SERVICE", "")
    if svc in ("api", "normalizer"):
        return svc
    if svc:
        return "worker"
    cmd = pid1()
    if "uvicorn" in cmd:
        return "api"
    if "normalizer_worker" in cmd:
        return "normalizer"
    return "worker"


def check_api(require_ok: bool) -> tuple[bool, str]:
    """Readiness uses /system/health, which answers 503 when a dependency is down.
    Liveness uses /system/live, which answers while the process can serve at all:
    a database outage must take a replica out of rotation, not restart it."""
    url = URL if require_ok else LIVE_URL
    if not url.startswith(("http://", "https://")):
        return False, f"ROSETTA_HEALTH_URL must be an http(s) URL, not {url!r}"
    try:
        # The URL comes from the container's own environment and its scheme is checked above.
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            return False, f"HTTP {e.code} from {url}"
        return False, f"HTTP {e.code}: status {body.get('status')}: {body.get('checks')}"
    except Exception as e:
        return False, f"no answer from {url}: {type(e).__name__}"
    status = body.get("status")
    if require_ok and status != "ok":
        return False, f"status {status}: {body.get('checks')}"
    return True, f"status {status}"


def check_normalizer() -> tuple[bool, str]:
    folder = Path(os.environ.get("ROSETTA_DATA_DIR", "./data")) / "checkpoints"
    files = list(folder.glob("normalizer-*.json"))
    if not files:
        return False, f"no checkpoint in {folder}"
    age = time.time() - max(f.stat().st_mtime for f in files)
    return age <= MAX_AGE_S, f"newest checkpoint is {age:.0f}s old (limit {MAX_AGE_S:.0f}s)"


def check_worker() -> tuple[bool, str]:
    cmd = pid1()
    if not cmd:
        return False, "cannot read /proc/1/cmdline"
    return "rosetta" in cmd, f"pid 1: {cmd.strip()[:120]}"   # also true for `docker-init -- rosetta-entrypoint`


def main(argv: list[str]) -> int:
    role = argv[1] if len(argv) > 1 else detect()
    if role == "api":
        ok, why = check_api(require_ok=True)
    elif role == "api-live":
        ok, why = check_api(require_ok=False)
    elif role == "normalizer":
        ok, why = check_normalizer()
    elif role == "worker":
        ok, why = check_worker()
    else:
        print(f"unknown role {role!r}", file=sys.stderr)
        return 1
    print(f"{role}: {'healthy' if ok else 'unhealthy'}: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
