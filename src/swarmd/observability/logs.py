"""Structured logging.

A log line is only useful if something can query it. In a cluster, logs go to a
collector that parses JSON and drops anything else into an unindexed blob, so
plain-text logs are searchable by grep on a single pod and by nothing at all
across a fleet.

The ConfigMap has set `SWARMD_LOG_FORMAT=json` since the deployment was written
and nothing read it — a gap the production readiness review caught and listed as
PARTIAL. This is that gap closed.

Two behaviours worth naming:

**Extras are promoted to top-level fields.** `logger.info("request",
extra={"status": 500})` produces `{"message": "request", "status": 500}` rather
than burying the status inside the message string. That is the difference
between `status:500` as a query and a substring search that also matches a
request which happened to take 500ms.

**Secrets are redacted on the way out.** Not because anything deliberately logs
a key, but because exception messages and request echoes are where they escape,
and a log collector is the last place you want one — logs are replicated,
retained, and readable by more people than the secret store is.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

# Fields already present on every LogRecord. Anything else in a record's
# __dict__ came from `extra=` and is promoted.
_STANDARD = frozenset(
    ["name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread", "threadName", "processName", "process", "taskName", "message", "asctime"]
)

# Redaction patterns. Deliberately broad: a false redaction costs a slightly
# less useful log line, while a missed one puts a live credential in a system
# that is replicated and widely readable.
_REDACTIONS = (
    (re.compile(r"\b(sk|gsk|xai|key|api)[-_][A-Za-z0-9_-]{12,}", re.IGNORECASE), "<redacted-key>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{8,}", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(postgres(?:ql)?://[^:]+:)[^@]+(@)", re.IGNORECASE), r"\1<redacted>\2"),
    (re.compile(r"(redis://[^:]+:)[^@]+(@)", re.IGNORECASE), r"\1<redacted>\2"),
)

SENSITIVE_KEYS = frozenset(
    {"api_key", "token", "password", "secret", "authorization", "database_url"}
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with `extra` fields promoted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            if key.lower() in SENSITIVE_KEYS:
                payload[key] = "<redacted>"
            elif isinstance(value, (str, int, float, bool, type(None))):
                payload[key] = redact(value) if isinstance(value, str) else value
            else:
                payload[key] = redact(str(value))

        if record.exc_info:
            # Tracebacks routinely contain the arguments a call was made with,
            # which is exactly where a key ends up.
            payload["exception"] = redact(self.formatException(record.exc_info))

        # default=str so an unexpected object never turns a log line into a
        # logging failure -- losing the line you needed to debug the thing that
        # produced it is a particularly unhelpful failure mode.
        return json.dumps(payload, default=str, separators=(",", ":"))


class PlainFormatter(logging.Formatter):
    """Human-readable, still redacted. Redaction is not a production-only concern."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure(
    *, level: str | None = None, fmt: str | None = None, stream: Any = None
) -> None:
    """Install the root handler. Idempotent.

    Reads SWARMD_LOG_FORMAT and SWARMD_LOG_LEVEL so the deployment ConfigMap
    controls it, rather than the call site.
    """
    resolved_format = (fmt or os.environ.get("SWARMD_LOG_FORMAT", "plain")).lower()
    resolved_level = (level or os.environ.get("SWARMD_LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        JsonFormatter() if resolved_format == "json" else PlainFormatter()
    )

    root = logging.getLogger()
    # Replace rather than add: configure() running twice would otherwise
    # duplicate every line, which reads as the system doing everything twice.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # uvicorn installs its own handlers and would double-log through the root.
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True
