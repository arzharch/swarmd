# Multi-stage build.
#
# Stage 1 resolves dependencies with uv against the committed lockfile, so the
# image contains exactly what CI tested -- no floating resolution at build time,
# which is how "works in CI, breaks in prod" happens.
#
# Stage 2 carries only the virtualenv and the source. No uv, no build tools, no
# package manager: anything not needed to run is attack surface that has to be
# patched forever.

# --- builder ---------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer. Source changes far more often than
# the lockfile, so this layer stays cached across almost every build.
# EXTRAS ARE THE DEPLOYMENT CONTRACT. The manifest runs `swarmd serve` against
# Postgres and Redis, so `serve` and `postgres` are not optional here whatever
# their name says: without them the container starts, prints "serve needs the
# 'serve' extra", and exits. Verified by running this image on a cluster.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra serve --extra postgres --extra metrics --extra otel --extra redis

COPY src/ ./src/
COPY examples/ ./examples/
# README.md is a BUILD INPUT here, not documentation: pyproject.toml declares
# `readme = "README.md"`, so the build backend reads it while packaging. Without
# this line `uv sync --no-editable` fails with "failed to open file
# /app/README.md" -- which is how this image came to have never once built. The
# CI job that would have caught it existed and had never run, because the
# workflow watched a branch this repo does not use.
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --extra serve --extra postgres --extra metrics --extra otel --extra redis

# --- runtime ---------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Numeric uid so the Kubernetes runAsNonRoot check can verify it without
# resolving a name -- with a username, the kubelet cannot tell it is non-root
# and admission fails at runtime rather than at deploy.
RUN groupadd --gid 10001 swarmd \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin swarmd

# tini reaps zombies. The sandbox harness spawns subprocesses, and PID 1 in a
# container does not reap by default, so without this a long run accumulates
# defunct processes until it exhausts the pid limit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src
COPY --from=builder --chown=10001:10001 /app/examples /app/examples

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SWARMD_METRICS_PORT=9464

USER 10001:10001
EXPOSE 8000 9464

# Build metadata, injected by CI. Ends up on the /healthz payload so a running
# pod can be traced back to the exact commit without guessing from image tags.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV SWARMD_GIT_SHA=${GIT_SHA} \
    SWARMD_BUILD_TIME=${BUILD_TIME}
LABEL org.opencontainers.image.revision=${GIT_SHA} \
      org.opencontainers.image.source="https://github.com/arzharch/swarmd" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/usr/bin/tini", "--"]
# Binds 0.0.0.0, not the CLI's 127.0.0.1 default. Inside a container loopback
# is unreachable from outside it, so `docker run -p 8000:8000` against the old
# default connected to nothing -- the server was up and the published port went
# nowhere. The Kubernetes manifest passes --host explicitly and so never hit
# this; anyone running the image directly did.
#
# Loopback is the right default for the CLI on a laptop and the wrong one here,
# which is why the two differ rather than one being "the" default. The app
# refuses to bind 0.0.0.0 without SWARMD_API_TOKEN (ADR-013), so this is not a
# quiet widening of exposure: with no token the container exits and says why.
CMD ["swarmd", "serve", "--host", "0.0.0.0", "--port", "8000"]
