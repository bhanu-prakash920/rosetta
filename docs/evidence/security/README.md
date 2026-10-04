# Security scan reports

Scans run locally on 30 September 2026 against the Rosetta source tree, the
container image and the running API. Rule sets and options match the
`security` and `dast` jobs in `.github/workflows/ci.yml` unless this page says otherwise.

"Before" is the state we found. "After" is the state once the fixes below were in.
Reports marked `-before` keep the original results.

## Summary

| Scanner | Kind | Before | After | Gate (CI) |
|---|---|---|---|---|
| bandit | SAST, Python | 0 | 0 | pass |
| Semgrep | SAST, Python, Docker, Terraform, secrets | 5 (2 error, 3 warning) | 0 | pass |
| Semgrep (web rules) | SAST, TypeScript/React | 0 | 0 | pass (added to CI) |
| pip-audit | Python dependencies | 8 (1 package) in a clean install; 329 (34 packages) in the old local venv | 0 | pass |
| npm audit | web dependencies | 10 (1 critical, 4 high, 5 moderate) | 0 | pass |
| Trivy fs | dependencies, secrets, IaC | 2 high | 0 high/critical | pass |
| Trivy image | OS and Python packages in the image | 4 high with a fix; 302 in total | 0 with a fix; 271 in total, none fixable | pass with `--ignore-unfixed` |
| ZAP baseline | DAST, passive | 1 medium, 3 low | 0 medium, 2 low | report only |
| ZAP API scan | DAST, active, authenticated | 1 high (false positive), 3 low | 1 high (false positive), 1 low | report only |

## What was fixed

| Finding | Fix | File |
|---|---|---|
| 10 npm advisories (vite, vitest, esbuild, puppeteer-core, react-router) | Upgraded to react-router-dom 7.18, vite 7.3, vitest 4.1, puppeteer-core 25.12. Typecheck and build pass | `web/package.json`, `web/package-lock.json`, `web/scripts/screenshots.mjs` (`headless: true`, the old `"new"` value is gone in puppeteer 25) |
| Old, vulnerable Python packages could still be installed (starlette 0.52, python-multipart 0.0.26, cryptography 46, anyio 4.12, requests 2.32, urllib3 2.6, python-dotenv 1.2.1, pytest 9.0.2) | Raised the lower bounds to the first fixed versions. Tests pass on the new versions | `pyproject.toml` |
| pip and setuptools with known CVEs in the image (their vendored `wheel`, `jaraco.context`, `msgpack`) | `python -m venv --upgrade-deps`, then pip removed from the runtime venv; base image pip and setuptools uninstalled | `Dockerfile` |
| Debian packages with published fixes (openssl, libssl3 and others) | `apt-get upgrade` in the runtime stage | `Dockerfile` |
| Trivy DS-0013: `RUN cd /` | Replaced by `WORKDIR /` | `Dockerfile` |
| ZAP 10055: CSP allowed `style-src 'unsafe-inline'` | Removed `'unsafe-inline'` and the unused Google Fonts hosts. Checked in Chrome on all ten console pages, the map included: no CSP violation | `rosetta/api/support.py`, `tests/unit/test_support.py` |
| ZAP 90004: no `Cross-Origin-Resource-Policy` | Added `Cross-Origin-Resource-Policy: same-origin` to every response | `rosetta/api/support.py`, `tests/unit/test_support.py` |
| Health check opened any URL taken from the environment (Semgrep) | Accepts only `http://` and `https://` | `infra/docker/healthcheck.py` |
| A public EKS endpoint with an empty allow-list falls back to 0.0.0.0/0 (Semgrep) | Validation rejects `0.0.0.0/0`; a precondition requires a CIDR list when the endpoint is public | `infra/terraform/variables.tf`, `infra/terraform/modules/eks/main.tf` |
| Trivy KSV-0011: the `waitForSeed` init container had no CPU limit | `limits.cpu: 500m` | `infra/helm/rosetta/values.yaml` |

Checks after the fixes: `pytest tests/security tests/unit` passes on the project venv and
on a clean install with the new versions. `npm run build` passes. `terraform validate` passes.
The rebuilt image (`rosetta:secscan`) starts, answers `/api/v1/system/live` and passes its health check.

## Per scanner

### bandit (SAST, Python)

| | |
|---|---|
| Version | bandit 1.9.4 |
| Scanned | `rosetta/` (9,961 lines) |
| Command | `.venv/bin/python -m bandit -c pyproject.toml -r rosetta -f json -o docs/evidence/security/bandit.json` |
| Before / after | 0 / 0 |
| Reports | `bandit.json`, `bandit.txt` |

Three lines carry a reviewed `# nosec` (the `exec` of generated adapters, a constant SQL column list,
the OAuth2 `token_type`). `pyproject.toml` skips B101, B110, B112, B311, B404 and B603. Without those skips
bandit reports 18 low findings, all of those kinds.

### Semgrep (SAST)

| | |
|---|---|
| Version | Semgrep 1.178.0 (throwaway venv, same pin as CI) |
| Scanned | 490 files: Python, Dockerfile, Terraform, YAML, JSON, TypeScript. `tests`, `design`, `web/node_modules`, `web/dist` excluded as in CI |
| Command | `semgrep scan --config p/python --config p/security-audit --config p/secrets --config p/dockerfile --exclude web/node_modules --exclude web/dist --exclude tests --exclude design --metrics off --error --timeout 120 --sarif-output docs/evidence/security/semgrep.sarif --json-output docs/evidence/security/semgrep.json` |
| Before | 5: 2 error (subprocess in `scripts/run_api_load.py`), 3 warning (`exec` in codegen, `urlopen` in the health check, public EKS endpoint) |
| After | 0 |
| Reports | `semgrep.sarif`, `semgrep.json`, `semgrep.txt`; before: `semgrep-before.*` |

The 11 Helm templates cannot be parsed by Semgrep (Go template syntax). This is expected; Trivy covers them.
`--timeout 120` was added because the default 5 s per file timed out on a few large files while other scans ran.

Extra scan of the web code with TypeScript rules (not in CI yet):
`semgrep scan --config p/typescript --config p/react --config p/javascript --config p/xss --metrics off web/src web/index.html web/vite.config.ts`
gave 0 findings on 18 files. Reports: `semgrep-web.json`, `semgrep-web.txt`.

### pip-audit (Python dependencies)

| | |
|---|---|
| Version | pip-audit 2.10.1 |
| Scanned | A clean venv with `pip install -e ".[dev]"`, as CI does (139 packages) |
| Command | `pip-audit --format json --output docs/evidence/security/pip-audit.json` |
| Before | 8 records, all `setuptools 65.5.0` (4 advisories, each listed twice) |
| After | 0 (setuptools upgraded to 84.0.0 in the venv) |
| Reports | `pip-audit.json`, `pip-audit.txt`; before: `pip-audit-before.json`, `pip-audit-local-venv-before.json` |

`pip-audit-local-venv-before.json` is the project's own `.venv` (325 packages, OSV database).
It had 329 records in 34 packages. Most are notebook and LangChain tools that are not Rosetta dependencies.
The ones that are (starlette, python-multipart, cryptography, anyio, requests, urllib3, python-dotenv, pytest)
now have raised lower bounds in `pyproject.toml`. The local `.venv` itself was not upgraded, because the
demo server on port 8765 runs from it. Upgrade it with `pip install -U -e ".[dev]"` after the demo.

### npm audit (web dependencies)

| | |
|---|---|
| Version | npm 11.6.2, Node 25.2.1 |
| Scanned | `web/package-lock.json` (132 packages) |
| Command | `cd web && npm audit --json > ../docs/evidence/security/npm-audit.json` |
| Before | 10: 1 critical (vitest), 4 high (vite, puppeteer-core, @puppeteer/browsers, extract-zip), 5 moderate |
| After | 0 |
| Reports | `npm-audit.json`, `npm-audit.txt`; before: `npm-audit-before.*` |

Only react-router-dom ships to the browser. The rest are build and test tools. All were upgraded anyway.

### Trivy file system scan

| | |
|---|---|
| Version | Trivy 0.74.0 (container `aquasec/trivy`) |
| Scanned | the repository: lock files, secrets, Dockerfile, Helm chart, Terraform, compose |
| Command (CI gate) | `trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --ignore-unfixed --skip-dirs web/node_modules --skip-dirs design --skip-dirs .venv --exit-code 1 --format json --output docs/evidence/security/trivy-fs.json .` |
| Before (gate) | 2 high: KSV-0109 (Helm ConfigMap "with secrets"), AWS-0073 (MSK plain text) |
| After (gate) | 0 |
| All severities before / after | 18 (2 high, 11 medium, 5 low) / 10 (9 medium, 1 low) |
| Reports | `trivy-fs.json`, `trivy-fs.txt`, `trivy-fs-all-severities.json`; before: `trivy-fs-before.json`, `trivy-fs-all-severities-before.json` |

No vulnerable dependency and no secret was found.

### Trivy image scan

| | |
|---|---|
| Version | Trivy 0.74.0 |
| Scanned | Before: `rosetta:dev` (built from the old Dockerfile). After: `rosetta:secscan`, built from the fixed Dockerfile |
| Command | `trivy image --scanners vuln,secret,misconfig --format json --output docs/evidence/security/trivy-image-rosetta-secscan.json rosetta:secscan` |
| Gate command | `trivy image --scanners vuln,secret --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 rosetta:secscan` |
| Before | 302: 5 critical, 61 high, 125 medium, 108 low, 3 unknown. 31 had a fix, 4 of them high (setuptools' vendored `wheel` and `jaraco.context`). Gate: fail |
| After | 271: 5 critical, 57 high, 109 medium, 98 low, 2 unknown. None has a fix. Gate: pass |
| Reports | `trivy-image-rosetta-secscan.json`, `trivy-image-rosetta-secscan.txt`; before: `trivy-image-rosetta-dev-before.*` |

All remaining findings are Debian 12 packages (perl-base, util-linux, libc, sqlite, zlib and others).
Debian marks them "affected", "fix deferred" or "will not fix". The Python packages in the image have no known vulnerability.

### OWASP ZAP

ZAP 2.17.0 (container `zaproxy/zap-stable`).

| Scan | Target | Command | Before | After | Reports |
|---|---|---|---|---|---|
| Baseline (passive, spider and AJAX spider) | Before: demo server `:8765`. After: patched image on `:18099` | `zap-baseline.py -t http://host.docker.internal:8765 -a -j -J zap-baseline.json -r zap-baseline.html -w zap-baseline.md` | 1 medium, 3 low, 8 info | 0 medium, 2 low, 7 info | `zap-baseline.*`, `zap-baseline-before.*` |
| API scan (active, with a bearer token) | Before: demo server `:8765`, safe subset of the API. After: patched image on `:18099`, the full `docs/api/openapi.json` | `zap-api-scan.py -t /zap/wrk/openapi.json -f openapi -O http://host.docker.internal:8765 -J zap-api.json -r zap-api.html -w zap-api.md -z "-config replacer.full_list(0).description=auth -config replacer.full_list(0).enabled=true -config replacer.full_list(0).matchtype=REQ_HEADER -config replacer.full_list(0).matchstr=Authorization -config replacer.full_list(0).regex=false -config replacer.full_list(0).replacement=Bearer\ $TOKEN"` | 1 high, 3 low, 3 info | 1 high, 1 low, 3 info | `zap-api.*`, `zap-api-live-before.*` |

Notes on the ZAP runs:

- The demo server on port 8765 could not be restarted, so it still runs the old code. The "after" scans ran
  against the fixed image (`rosetta:secscan`, seeded with 2,000 vehicles, `ROSETTA_ENV=test`).
- On the demo server the active scan skipped nine state-changing operations (chaos kill, erasure, mapping
  actions, new mapping, simulator settings and scenarios, DLQ replay, ingest, agent runs), so it would not
  change the demo data. The subset is `zap-api-live-openapi-subset.json`. The run on the disposable
  container covered the whole API.
- The rate limits did their job. On the demo server many attack requests got `429 Too Many Requests`.
  For the disposable container the general limit was raised (`ROSETTA_RATE_LIMIT_PER_MIN=60000`) so the
  attacks reached the handlers. The login throttle stayed on and answered 324 requests with 429.
  The container logged no `500` response during the whole active scan.

## Findings that remain

| Scanner | Finding | Decision | Why |
|---|---|---|---|
| ZAP API | 40018 SQL injection (34 instances before, 1 after) | false positive | Every flagged response was a `429`. ZAP compares answers to `AND 1=1` and `OR 1=1`; the rate limiter makes them differ. Repeated by hand: injected values return 422 or no rows. All queries go through SQLAlchemy with bound parameters. After the fix only `POST /auth/token` is flagged, which is the login throttle |
| ZAP API | 100001 unexpected content type | accepted | `/api/v1/stream` is Server-Sent Events (`text/event-stream`). Unknown paths outside `/api/v1/` return the single-page app's HTML by design |
| ZAP baseline | 90004 no `Cross-Origin-Embedder-Policy` | accepted | `require-corp` would block the OpenStreetMap and ArcGIS map tiles. The app does not need cross-origin isolation |
| ZAP baseline | 10096 timestamp disclosure | false positive | A number in the JavaScript bundle; API timestamps are data, not a leak |
| ZAP | Info: Sec-Fetch-* missing, cacheable content, modern web app, auth request, client error codes | informational | Describe ZAP's own requests or normal behaviour |
| Semgrep | `exec` in `rosetta/engine/codegen.py` | accepted, suppressed | The generated source holds only `repr()` of validated literals (ADR 0001). Also `# nosec B102` |
| Semgrep | `urlopen` in `infra/docker/healthcheck.py` | fixed, then suppressed | URL comes from the container's own environment; scheme now checked |
| Semgrep | `subprocess.run` in `scripts/run_api_load.py` | false positive, suppressed | Argument list, no shell, a developer tool |
| Semgrep | public EKS endpoint | accepted, suppressed | Off by default. Now validated: no 0.0.0.0/0, and a CIDR list is required when on |
| Trivy fs | AWS-0073 MSK plain text between clients and brokers | accepted risk, inline `#trivy:ignore` | The Kafka client has no TLS settings yet. Brokers are VPC-only behind security groups. Proper fix: TLS settings in `rosetta/adapters/kafka_broker.py`, then `kafka_client_broker_encryption = "TLS"` |
| Trivy fs | KSV-0109, KSV-01010 ConfigMap "with secrets" | false positive, `.trivyignore` | Matches `PASS` in `ROSETTA_GOLDEN_PASS_RATE` (a number). Secrets are in a Kubernetes Secret |
| Trivy fs | KSV-0125 images not from a trusted registry (9, medium) | accepted | The registry is a Helm value. Enforce with an admission policy in the cluster |
| Trivy fs | AWS-0089 no S3 access logging (low) | accepted | Needs a separate log bucket; CloudTrail covers API access |
| Trivy image | 271 Debian package CVEs, none fixable | accepted until Debian ships fixes | `apt-get upgrade` takes each fix as soon as it exists. Moving to a Debian 13 base would shrink the list |
| bandit | 18 low findings of skipped kinds | accepted | Skips listed and explained in `pyproject.toml` |

## Suppressions added

- `.trivyignore`: KSV-0109, KSV-01010, with the reason.
- `infra/terraform/modules/msk/main.tf`: `#trivy:ignore:AWS-0073` with the reason.
- `# nosemgrep: <rule id>` with a one-line reason on the line above, in `rosetta/engine/codegen.py`,
  `infra/docker/healthcheck.py`, `scripts/run_api_load.py`,
  `infra/terraform/modules/eks/main.tf`.
