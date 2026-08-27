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
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra metrics --extra otel --extra redis

COPY src/ ./src/
COPY examples/ ./examples/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --extra metrics --extra otel --extra redis

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
CMD ["swarmd", "serve"]
