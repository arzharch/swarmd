"""Edge hardening for the control plane.

THE POSTURE, stated plainly because it drives everything here: swarmd is a
single-tenant, operator-run service. There are no user accounts, no roles, and
no per-user data, because there are no users — one operator turns it on, runs
work, and turns it off. Adding an identity system would be building a login for
a population of one.

That is a decision, not an omission (ADR-013), and a decision only holds if the
compensating controls are real:

  1. **A shared operator token.** Not user auth — one bearer token that gates
     every mutating endpoint. Optional locally so `swarmd serve` on a laptop
     stays frictionless; REQUIRED whenever the service is reachable off-host,
     and the app refuses to start in that configuration without one.
  2. **Read/write split.** Probes and metrics stay open so Kubernetes and
     Prometheus can scrape without holding a secret. Anything that starts work,
     spends money, or records a human decision requires the token.
  3. **Rate limiting.** Not a DoS defence — a quota defence. The scarce resource
     is provider requests, and a loop in a script can burn a day's free tier in
     a minute.
  4. **Body size cap.** Enforced before parsing. A task is at most 4000
     characters; accepting a 50MB body to then reject it is a memory
     amplification with no upside.
  5. **Request identity.** Every request gets an id, logged with method, path,
     status and duration, so an incident has something to reconstruct from.

What this deliberately is NOT: a defence against an authenticated insider, or a
substitute for keeping the service off the public internet. The NetworkPolicy
and the Ingress allowlist are the primary control; this is the layer beneath.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ENV_TOKEN = "SWARMD_API_TOKEN"
ENV_BIND = "SWARMD_BIND_HOST"

# A task is capped at 4000 characters by the request model, so 64KB is generous
# headroom for JSON overhead while still rejecting anything absurd before it is
# parsed into memory.
MAX_BODY_BYTES = 64 * 1024

# Endpoints reachable without the operator token. Kubernetes probes and the
# Prometheus scraper must not need a secret: giving every scraper the token
# that can also start runs is a wider grant than the job requires.
PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

# Methods that change something. GETs on run history are readable without the
# token because the dashboard is already behind the Ingress allowlist and
# nothing there is a secret; anything that spends money is not.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class InsecureConfiguration(RuntimeError):
    """Refuses to serve off-host without a token.

    Fatal at startup rather than at first request. A service that binds
    0.0.0.0 with no token and only complains when someone finds it has already
    failed; the whole value of this check is that it happens before exposure.
    """


ENV_ALLOW_OPEN = "SWARMD_ALLOW_OPEN"

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def require_safe_configuration(
    host: str, token: str | None, *, allow_open: bool | None = None
) -> None:
    """Refuse to start wide open on a non-loopback interface BY ACCIDENT.

    User authentication is deliberately out of MVP scope: this is an operator
    tool, and the run API is not a multi-tenant surface. That is a defensible
    decision and it is not the same as an unguarded one, so running open has to
    be something you SAY, not something you get by forgetting a variable.

    Three ways to start:

      bind loopback             the default, and what local development uses
      set SWARMD_API_TOKEN      auth on, for anything reachable off-host
      set SWARMD_ALLOW_OPEN=1   auth off on a public bind, deliberately

    The third is what a deployment uses while auth is out of scope, and it is
    only honest because something else is doing the work: in Kubernetes a
    default-deny NetworkPolicy admits port 8000 from the dashboard pod and the
    ingress controller and nothing else (deploy/k8s/base/rbac-and-config.yaml).
    Set it anywhere without an equivalent control and the run API -- which
    spends real provider quota -- is reachable by anything that can route to
    the port.
    """
    if allow_open is None:
        allow_open = _truthy(os.environ.get(ENV_ALLOW_OPEN, ""))
    if host in LOOPBACK or token or allow_open:
        return
    raise InsecureConfiguration(
        f"refusing to bind {host} with no {ENV_TOKEN} set. swarmd has no user "
        f"auth by design (ADR-013); nothing here distinguishes one caller from "
        f"another, and the run API spends real provider quota. Either bind "
        f"127.0.0.1, set {ENV_TOKEN}, or set {ENV_ALLOW_OPEN}=1 to say you "
        f"have restricted access some other way."
    )


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def exposure_warning(host: str, token: str | None) -> str:
    """One line naming exactly what is exposed, or "" when nothing is.

    Returned rather than logged so the CLI can print it where an operator will
    actually see it. A warning that only reaches the log is a warning that
    scrolls past during startup.
    """
    if token or host in LOOPBACK:
        return ""
    return (
        f"OPEN: bound to {host} with no {ENV_TOKEN}. Every endpoint, including "
        f"run submission, is reachable by anything that can route here. This is "
        f"only safe behind a network control that restricts who can."
    )


@dataclass
class RateLimit:
    """Per-client sliding window over mutating requests.

    ANATOMY: max_requests / window_s
      Why 30 per minute: a human operator submitting runs from a dashboard never
      approaches it, while a runaway script is stopped in seconds. This is a
      QUOTA defence rather than a DoS defence -- the scarce resource is the
      ~45 provider requests per minute the whole system shares, and a loop
      submitting runs can burn a day's free tier before anyone notices.

    Sliding window rather than a fixed one: a fixed window lets a caller spend
    the entire minute's allowance in its last second and the next minute's in
    the following second, which is twice the intended rate at the boundary.
    """

    max_requests: int = 30
    window_s: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def check(self, client: str, now: float | None = None) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        window = self._hits.setdefault(client, deque())
        cutoff = now - self.window_s
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self.max_requests:
            return False, round(window[0] + self.window_s - now, 2)
        window.append(now)
        return True, 0.0

    def prune(self, now: float | None = None) -> None:
        """Drop idle clients so the table does not grow without bound."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_s
        for client in [c for c, w in self._hits.items() if not w or w[-1] < cutoff]:
            del self._hits[client]


def token_matches(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison.

    `==` on secrets leaks length and prefix through timing. The window is small
    over a network, but the correct comparison costs nothing and removes the
    need to argue about how small.
    """
    if not supplied:
        return False
    return hmac.compare_digest(supplied.strip(), expected.strip())


def extract_token(headers: Any) -> str | None:
    """Accept `Authorization: Bearer <t>` or `X-Swarmd-Token: <t>`.

    The second exists because a browser dashboard cannot set an Authorization
    header on a websocket handshake, and the stream needs the same gate as
    everything else.
    """
    auth = str(headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    supplied = headers.get("x-swarmd-token")
    if supplied:
        return str(supplied)
    protocols = str(headers.get("sec-websocket-protocol") or "")
    if protocols:
        return protocols.split(",")[0].strip()
    return None


def install(app: Any, *, token: str | None = None, rate_limit: RateLimit | None = None) -> Any:
    """Attach the hardening middleware. Returns the app for chaining."""
    from fastapi import Request

    configured_token = token if token is not None else os.environ.get(ENV_TOKEN, "")
    limiter = rate_limit or RateLimit()
    app.state.api_token = configured_token
    app.state.rate_limit = limiter

    if not configured_token:
        logger.warning(
            "no %s configured: every endpoint is open to anything that can "
            "reach this port. Acceptable on loopback, not off-host.", ENV_TOKEN,
        )

    # Starlette's decorator is untyped, so mypy strict cannot see through it.
    # Ignored at the single application point rather than loosening the whole
    # module's strictness.
    @app.middleware("http")  # type: ignore[untyped-decorator]
    async def harden(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        path = request.url.path
        client = request.client.host if request.client else "unknown"

        # 1. Body size, before parsing.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _refuse(
                413, "request body too large", request_id,
                detail=f"limit is {MAX_BODY_BYTES} bytes",
            )

        # 2. Operator token on mutating endpoints.
        needs_token = (
            configured_token
            and path not in PUBLIC_PATHS
            and request.method in MUTATING_METHODS
        )
        if needs_token and not token_matches(
            extract_token(request.headers), configured_token
        ):
            logger.warning(
                "rejected unauthenticated %s %s from %s", request.method, path, client
            )
            return _refuse(401, "operator token required", request_id)

        # 3. Rate limit, on mutating endpoints only. Reads are cheap; runs are
        #    not, and the resource being protected is provider quota.
        if request.method in MUTATING_METHODS and path not in PUBLIC_PATHS:
            allowed, retry_after = limiter.check(client)
            if not allowed:
                return _refuse(
                    429, "rate limited", request_id,
                    detail=f"retry after {retry_after}s",
                    headers={"Retry-After": str(int(retry_after) + 1)},
                )

        response = await call_next(request)

        # 4. Security headers. The dashboard is same-origin behind the Ingress
        #    and loads no third-party resources, so the policy can be strict.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-Id"] = request_id

        duration_ms = (time.monotonic() - started) * 1000
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "client": client,
            },
        )
        return response

    return app


def _refuse(
    status: int, message: str, request_id: str, *,
    detail: str = "", headers: dict[str, str] | None = None,
) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {"error": message, "detail": detail, "request_id": request_id},
        status_code=status,
        headers={**(headers or {}), "X-Request-Id": request_id},
    )
