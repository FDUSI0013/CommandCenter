# Fulcrum Ops control plane
#
# Build context is the REPOSITORY ROOT, not this directory: the image carries
# both the API and the console's static files, and those live in sibling
# directories. Build it with:
#
#   docker build -f apps/control-plane/Dockerfile -t fulcrum-ops/control-plane .
#
# (deploy/docker-compose.yml already does this.)

FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY apps/control-plane/pyproject.toml apps/control-plane/README.md ./
COPY apps/control-plane/src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FULCRUM_OPS_STATIC_DIR=/app/web

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin fulcrum

COPY --from=build /opt/venv /opt/venv
COPY --chown=fulcrum:fulcrum apps/control-plane/migrations /app/migrations
COPY --chown=fulcrum:fulcrum apps/control-plane/alembic.ini /app/alembic.ini
# The console ships inside the image so a single container is a working
# product. The edge proxy serves these files directly in a normal deployment;
# this copy is what makes `docker run` alone enough for a smoke test.
COPY --chown=fulcrum:fulcrum apps/web /app/web

# The export spool mounts as a named volume; owning it here is what makes the
# fresh volume writable by the non-root user on first use.
RUN mkdir -p /app/var/exports && chown -R fulcrum:fulcrum /app/var

WORKDIR /app
USER fulcrum
EXPOSE 8080

# 127.0.0.1, not localhost: on a host with IPv6 disabled "localhost" resolves to
# ::1 first and a server listening correctly on IPv4 is reported unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# Migrations run before the server so a rolling restart never serves against
# an older schema.
#
# --limit-max-requests retires each worker after a bounded number of requests
# and the supervisor starts a fresh one in its place. It is a backstop, not a
# fix: whatever a long-lived process slowly leaks — a socket, a pooled
# connection, memory — a worker that is replaced every few days cannot
# accumulate enough of it to fail. The jitter keeps the four from retiring
# together, and the graceful-shutdown ceiling stops an open live-runs stream
# from holding a retiring worker open indefinitely.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn fulcrum_ops_api.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*' --workers ${WEB_CONCURRENCY:-4} --limit-max-requests ${WORKER_MAX_REQUESTS:-20000} --limit-max-requests-jitter ${WORKER_MAX_REQUESTS_JITTER:-4000} --timeout-graceful-shutdown 25"]
