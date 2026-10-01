# syntax=docker/dockerfile:1
#
# One image, every Rosetta service. The argument passed to the container selects
# the role (see infra/docker/entrypoint.sh):
#
#   api | gateway | normalizer | processor | dlq | simulator | migrate | seed
#
# Anything else is executed as a command, so
#   docker run rosetta python -m rosetta.pipeline.dlq
# works as well.
#
# No secret is set here. Every credential arrives through the environment at
# run time (rosetta/config.py).

ARG PYTHON_IMAGE=python:3.11.16-slim-bookworm
ARG NODE_IMAGE=node:22.23.3-bookworm-slim

# ---------------------------------------------------------------- web UI build
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ------------------------------------------------------- Python dependencies
FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
# --upgrade-deps: the pip and setuptools bundled with Python have known
# vulnerabilities (Trivy image scan, docs/evidence/security/README.md).
RUN python -m venv --upgrade-deps /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /src
# pyproject.toml names README.md as the long description, so the build needs it.
COPY pyproject.toml README.md ./
# Dependencies first, in their own layer: an empty package with the same
# pyproject.toml pulls them in. A change to the code then reuses this layer
# instead of downloading every dependency again.
RUN mkdir rosetta && touch rosetta/__init__.py \
    && pip install . \
    && pip uninstall --yes rosetta \
    && rm -rf rosetta build *.egg-info
COPY rosetta/ rosetta/
# The check runs outside /src, so that it imports the installed package and
# not the source tree: the trained model must have been packaged with it.
RUN pip install --no-deps .
WORKDIR /
RUN python -c "import importlib.resources as r; \
assert (r.files('rosetta.ml') / 'artifacts' / 'field_mapper.joblib').is_file(), 'model artifact missing from the package'"
# pip is not needed at run time, and it vendors packages with known
# vulnerabilities (msgpack, setuptools). Remove it from the runtime venv.
RUN python -m pip uninstall -y pip

# -------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="rosetta" \
      org.opencontainers.image.description="Universal OEM telemetry normalisation engine" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROSETTA_DATA_DIR=/var/lib/rosetta \
    ROSETTA_WEB_DIST=/app/web/dist

# Take the Debian security fixes published since the base image was built, and
# remove the base image's own pip and setuptools: the application runs from
# /opt/venv and never uses them, and they carry known vulnerabilities.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python -m pip uninstall -y pip setuptools

RUN groupadd --system --gid 10001 rosetta \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin rosetta \
    && mkdir -p /app /var/lib/rosetta \
    && chown -R 10001:10001 /app /var/lib/rosetta

COPY --from=build /opt/venv /opt/venv
COPY --from=web --chown=10001:10001 /web/dist /app/web/dist
COPY --chmod=0755 infra/docker/entrypoint.sh /usr/local/bin/rosetta-entrypoint
COPY --chmod=0755 infra/docker/healthcheck.py /usr/local/bin/rosetta-healthcheck

WORKDIR /app
USER 10001:10001
VOLUME ["/var/lib/rosetta"]
EXPOSE 8000

# The check adapts to the role of the container: HTTP for the API, checkpoint
# freshness for the normaliser, process presence for the other workers.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=4 \
    CMD ["rosetta-healthcheck"]

ENTRYPOINT ["rosetta-entrypoint"]
CMD ["api"]
