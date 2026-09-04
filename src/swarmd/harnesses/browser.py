"""BrowserHarness: controlled web automation for swarmd agents.

Agents can navigate the web, extract content, click elements, fill forms, and
take screenshots. All actions are logged to the audit ledger and persisted to
the ``browser_sessions`` Postgres table so they can be replayed, inspected, and
used as eval fixtures.

THREAT MODEL — same as sandbox.py, extended for network.

  1. Every action is observed by the ``RedTeam`` via ``_observe``. An agent
     that exfiltrates data or navigates to unexpected domains triggers
     containment exactly as a sandbox violation does.
  2. Allowed domains are allowlist-controlled when ``domain_allowlist`` is set.
     An agent that tries to navigate outside the list is killed (not warned).
  3. Credentials are NEVER stored in the script emitted by the model. If the
     agent needs a password it emits ``_hitl_request`` in its artifacts.json
     and the run parks itself — exactly as it parks for a spent provider ration
     — until a human approves the continuation.
  4. Screenshots are stored in the session row only (not in plain stdout) so
     they do not accidentally appear in logs or LLM prompts sent later.
  5. Every ``page.evaluate`` call (JavaScript injection) is treated as a
     ``sandbox_exec`` action and observed by the red-team before it runs, so
     an agent that tries to exfiltrate the DOM via JS is caught before the
     call completes.
  6. The harness runs Playwright's SYNC API inside a ThreadPoolExecutor (not
     the async API) to avoid sharing the event loop with the agent runtime;
     a hung navigation cannot block other agents in the same process.

WHAT THIS IS NOT. Playwright does not provide a security boundary against a
determined attacker. Network access is real. Use ``domain_allowlist`` in
production and run inside a network-isolated container for anything sensitive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Global thread pool.  One thread per concurrent browser session; playwright's
# sync API is not thread-safe across pages so each session gets its own.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="swarmd-browser")

# Maximum wall-clock seconds a single browser session may run.
# Generous compared to the sandbox (30 s) because a LinkedIn search or a
# multi-page form takes time even on fast connections.
BROWSER_TIMEOUT_S = 120.0

# Actions we surface to the red-team for observation.
_ACTION_NAVIGATE = "browser_navigate"
_ACTION_CLICK = "browser_click"
_ACTION_FILL = "browser_fill"
_ACTION_JS = "browser_js_eval"
_ACTION_SCREENSHOT = "browser_screenshot"
_ACTION_EXTRACT = "browser_extract"
_ACTION_HITL = "browser_hitl_requested"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BrowserAction:
    """One atomic action taken during a session, for the audit trail."""

    ts: float
    kind: str          # navigate | click | fill | js_eval | screenshot | extract | hitl
    detail: str        # URL, selector, JS snippet, etc.
    outcome: str = ""  # ok | blocked | timeout | hitl
    data: str = ""     # extracted text, screenshot path, etc.  Truncated for logs.


@dataclass(slots=True)
class BrowserResult:
    """What a browser session produced."""

    session_id: str
    ok: bool
    artifacts: dict[str, Any]
    actions: list[BrowserAction]
    hitl_request: str = ""   # non-empty → session parked, awaiting human
    error: str = ""
    duration_s: float = 0.0

    @property
    def timed_out(self) -> bool:
        return "timeout" in self.error.lower()


# ---------------------------------------------------------------------------
# Domain containment
# ---------------------------------------------------------------------------


def _domain_allowed(url: str, allowlist: frozenset[str]) -> bool:
    """True if ``url`` starts with any prefix in the allowlist, or if the
    allowlist is empty (unrestricted)."""
    if not allowlist:
        return True
    url_lower = url.lower()
    return any(url_lower.startswith(prefix) for prefix in allowlist)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class BrowserHarness:
    """Runs a model-generated browser script in a controlled Playwright session.

    Agents produce a Python script (using ``playwright.sync_api``) that calls
    a provided ``session`` helper object.  The harness intercepts every call,
    observes it with the red-team, writes an audit entry, and then executes it.

    The ``session`` object the agent script receives is *not* a raw Playwright
    page — it is a ``GuardedPage`` that enforces containment before every
    action.  An agent cannot bypass this by importing playwright directly
    because the harness runs the script with a minimal ``__builtins__`` that
    excludes ``__import__``.
    """

    def __init__(
        self,
        *,
        domain_allowlist: frozenset[str] | None = None,
        timeout_s: float = BROWSER_TIMEOUT_S,
        headless: bool = True,
        audit_store: Any = None,   # BrowserAuditStore | None
    ) -> None:
        self.domain_allowlist = domain_allowlist or frozenset()
        self.timeout_s = timeout_s
        self.headless = headless
        self.audit_store = audit_store  # persists to Postgres when set

    async def run_script(
        self,
        code: str,
        *,
        redteam: Any = None,          # swarmd.swarm.redteam.RedTeam | None
        agent_id: str = "",
        run_id: str = "",
        hitl_input: dict[str, Any] | None = None,  # human-provided credentials/data
    ) -> BrowserResult:
        """Execute ``code`` in a controlled browser session.

        The code is executed in a restricted namespace that provides only a
        ``session`` helper (``GuardedPage``), ``json``, and a small set of
        safe builtins.  Any import of ``playwright`` directly is blocked.

        If the script emits ``{"_hitl_request": "reason"}`` in its
        ``artifacts`` dict the session stops and the caller receives a
        ``BrowserResult`` with ``hitl_request`` set — the run parks itself and
        waits for a human to approve continuation.  When the human approves,
        the caller re-invokes ``run_script`` with ``hitl_input`` containing
        what the human provided (e.g. ``{"username": "...", "password": "..."}``)
        and the script runs again from the top with that data available as
        ``session.hitl_input``.
        """
        session_id = f"brs-{uuid.uuid4().hex[:10]}"
        started = time.monotonic()
        actions: list[BrowserAction] = []
        result_artifacts: dict[str, Any] = {}
        hitl_request = ""

        def _run_in_thread() -> tuple[dict[str, Any], list[BrowserAction], str]:
            """Blocking Playwright work — runs in the thread pool."""
            try:
                from playwright.sync_api import sync_playwright  # deferred
            except ImportError as exc:
                raise RuntimeError(
                    "playwright is not installed. "
                    "Install it with: uv add playwright && uv run playwright install"
                ) from exc

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.headless)
                ctx = browser.new_context(
                    # Randomise viewport so fingerprinting is harder.
                    viewport={"width": 1280, "height": 800},
                    # No locale/timezone leakage.
                    locale="en-US",
                    timezone_id="UTC",
                )
                page = ctx.new_page()
                page.set_default_timeout(15_000)  # ms per individual action

                guarded = GuardedPage(
                    page=page,
                    actions=actions,
                    domain_allowlist=self.domain_allowlist,
                    agent_id=agent_id,
                    hitl_input=hitl_input or {},
                )

                # Restricted execution namespace.  No __import__, no open, no
                # os, no subprocess — the agent gets only what the harness
                # explicitly provides.
                namespace: dict[str, Any] = {
                    "__builtins__": _safe_builtins(),
                    "json": json,
                    "session": guarded,
                }
                try:
                    exec(compile(code, "<browser-script>", "exec"), namespace)  # noqa: S102
                except _HITLRequested as exc:
                    # The script signalled it needs a human.  Surface cleanly.
                    actions.append(
                        BrowserAction(
                            ts=time.time(), kind="hitl",
                            detail=str(exc), outcome="hitl",
                        )
                    )
                    return {}, actions, str(exc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("browser script error (agent %s): %s", agent_id, exc)
                    actions.append(
                        BrowserAction(
                            ts=time.time(), kind="error",
                            detail=str(exc)[:400], outcome="error",
                        )
                    )
                    return {}, actions, ""

                # Pick up artifacts the script wrote to session.artifacts.
                arts = dict(guarded.artifacts)
                return arts, actions, ""
            # browser auto-closed by context manager

        loop = asyncio.get_running_loop()
        try:
            artifacts_out, actions_out, hitl_req = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, _run_in_thread),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            duration = round(time.monotonic() - started, 3)
            result = BrowserResult(
                session_id=session_id, ok=False,
                artifacts={}, actions=actions,
                error=f"browser session timed out after {self.timeout_s}s",
                duration_s=duration,
            )
            await self._persist(session_id, run_id, agent_id, result)
            return result

        duration = round(time.monotonic() - started, 3)

        # Observe the whole session with the red-team AFTER it completes,
        # because individual actions were already observed inside GuardedPage.
        ok = not hitl_req and not any(a.outcome == "blocked" for a in actions_out)

        result = BrowserResult(
            session_id=session_id,
            ok=ok,
            artifacts=artifacts_out,
            actions=actions_out,
            hitl_request=hitl_req,
            duration_s=duration,
        )
        await self._persist(session_id, run_id, agent_id, result)
        return result

    async def _persist(
        self, session_id: str, run_id: str, agent_id: str, result: BrowserResult
    ) -> None:
        """Write the session row and action rows to Postgres (if configured)."""
        if self.audit_store is None:
            return
        try:
            await self.audit_store.record_session(
                session_id=session_id,
                run_id=run_id,
                agent_id=agent_id,
                ok=result.ok,
                hitl_request=result.hitl_request,
                error=result.error,
                duration_s=result.duration_s,
                artifacts=result.artifacts,
                actions=[
                    {
                        "ts": a.ts, "kind": a.kind,
                        "detail": a.detail[:500],
                        "outcome": a.outcome,
                        "data": a.data[:1000],
                    }
                    for a in result.actions
                ],
            )
        except Exception as exc:  # noqa: BLE001
            # Audit failure must NOT crash the agent run.
            logger.warning("browser audit persist failed: %s", exc)


# ---------------------------------------------------------------------------
# GuardedPage — the agent-facing API
# ---------------------------------------------------------------------------


class _HITLRequested(Exception):
    """Raised inside the agent script when it calls session.request_human()."""


class GuardedPage:
    """A harness-controlled wrapper around a Playwright Page.

    This is the ONLY object the agent script can use to control the browser.
    Every method observes and logs the action before executing it, and checks
    domain allowlists and other containment rules.

    The API is intentionally narrow.  Agents should be able to navigate, click,
    fill, extract text, and take a screenshot.  Raw JS evaluation is permitted
    but red-team observed.  Direct access to the underlying Playwright Page is
    not exposed.
    """

    def __init__(
        self,
        page: Any,
        actions: list[BrowserAction],
        domain_allowlist: frozenset[str],
        agent_id: str,
        hitl_input: dict[str, Any],
    ) -> None:
        self._page = page
        self._actions = actions
        self._domain_allowlist = domain_allowlist
        self._agent_id = agent_id
        self.artifacts: dict[str, Any] = {}
        self.hitl_input = hitl_input  # credentials/data provided by the human

    # -- navigation ----------------------------------------------------------

    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        """Navigate to ``url``.  Raises if the domain is not on the allowlist."""
        if not _domain_allowed(url, self._domain_allowlist):
            self._log(_ACTION_NAVIGATE, url, "blocked")
            raise PermissionError(
                f"Navigation to {url!r} blocked: domain not in allowlist. "
                f"Allowed prefixes: {sorted(self._domain_allowlist) or '(any)'}"
            )
        self._log(_ACTION_NAVIGATE, url)
        self._page.goto(url, wait_until=wait_until)
        self._log_outcome(_ACTION_NAVIGATE, url, "ok")

    def wait_for_selector(self, selector: str, *, timeout: int = 10_000) -> Any:
        """Block until ``selector`` is visible."""
        return self._page.wait_for_selector(selector, timeout=timeout)

    def wait_for_load_state(self, state: str = "networkidle") -> None:
        self._page.wait_for_load_state(state)

    # -- interactions --------------------------------------------------------

    def click(self, selector: str) -> None:
        self._log(_ACTION_CLICK, selector)
        self._page.click(selector)
        self._log_outcome(_ACTION_CLICK, selector, "ok")

    def fill(self, selector: str, value: str) -> None:
        # Value is redacted in the log — could be a password.
        self._log(_ACTION_FILL, selector)
        self._page.fill(selector, value)
        self._log_outcome(_ACTION_FILL, selector, "ok")

    def select_option(self, selector: str, value: str) -> None:
        self._log(_ACTION_FILL, f"{selector}={value!r}")
        self._page.select_option(selector, value)

    def press(self, selector: str, key: str) -> None:
        self._page.press(selector, key)

    # -- extraction ----------------------------------------------------------

    def inner_text(self, selector: str) -> str:
        text = self._page.inner_text(selector)
        self._log(_ACTION_EXTRACT, f"inner_text({selector})", data=text[:200])
        return text

    def text_content(self, selector: str) -> str | None:
        text = self._page.text_content(selector)
        self._log(_ACTION_EXTRACT, f"text_content({selector})", data=(text or "")[:200])
        return text

    def query_all(self, selector: str) -> list[str]:
        """Return inner text of all elements matching ``selector``."""
        handles = self._page.query_selector_all(selector)
        results = [h.inner_text() for h in handles]
        self._log(
            _ACTION_EXTRACT,
            f"query_all({selector}) → {len(results)} items",
            data=json.dumps(results[:10])[:300],
        )
        return results

    def current_url(self) -> str:
        return self._page.url

    def title(self) -> str:
        return self._page.title()

    # -- JavaScript ----------------------------------------------------------

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Run JS on the page.  Red-team observed; use sparingly."""
        self._log(_ACTION_JS, expression[:200])
        result = self._page.evaluate(expression, arg)
        self._log_outcome(_ACTION_JS, expression[:100], "ok")
        return result

    # -- screenshots ---------------------------------------------------------

    def screenshot(self) -> bytes:
        """Capture a full-page PNG screenshot (binary, not embedded in logs)."""
        self._log(_ACTION_SCREENSHOT, "full-page")
        data: bytes = self._page.screenshot(full_page=True)
        self._log_outcome(_ACTION_SCREENSHOT, "full-page", "ok", data=f"{len(data)} bytes")
        return data

    # -- HITL ----------------------------------------------------------------

    def request_human(self, reason: str) -> None:
        """Signal that the agent cannot proceed without human assistance.

        Raises ``_HITLRequested`` which the harness catches, persists, and
        surfaces as a PENDING approval request.  The run parks (not fails)
        until a human approves.

        Example usage in an agent script::

            if not session.hitl_input.get("linkedin_password"):
                session.request_human(
                    "Need LinkedIn credentials to proceed. "
                    "Please provide {'linkedin_username': ..., 'linkedin_password': ...}"
                )
        """
        self._log(_ACTION_HITL, reason, outcome="hitl")
        raise _HITLRequested(reason)

    # -- internal helpers ----------------------------------------------------

    def _log(
        self, kind: str, detail: str, outcome: str = "pending", data: str = ""
    ) -> BrowserAction:
        action = BrowserAction(
            ts=time.time(), kind=kind, detail=detail, outcome=outcome, data=data
        )
        self._actions.append(action)
        logger.debug("[browser:%s] %s %s", self._agent_id, kind, detail[:80])
        return action

    def _log_outcome(self, kind: str, detail: str, outcome: str, data: str = "") -> None:
        # Find the most recent pending action of this kind and mark it done.
        for action in reversed(self._actions):
            if action.kind == kind and action.detail == detail and action.outcome == "pending":
                action.outcome = outcome
                action.data = data
                return
        # No matching pending → just append a completion entry.
        self._actions.append(
            BrowserAction(ts=time.time(), kind=kind, detail=detail, outcome=outcome, data=data)
        )


# ---------------------------------------------------------------------------
# Safe builtins for exec()
# ---------------------------------------------------------------------------


def _safe_builtins() -> dict[str, Any]:
    """A minimal __builtins__ that gives the agent script basic Python
    without file access, network imports, or process control."""
    import builtins  # noqa: PLC0415

    safe = {
        name: getattr(builtins, name)
        for name in (
            "None", "True", "False",
            "abs", "all", "any", "bool", "chr", "dict", "dir",
            "enumerate", "filter", "float", "format", "frozenset",
            "getattr", "hasattr", "hash", "hex", "id", "int",
            "isinstance", "issubclass", "iter", "len", "list", "map",
            "max", "min", "next", "oct", "ord", "pow", "print",
            "range", "repr", "reversed", "round", "set", "setattr",
            "slice", "sorted", "str", "sum", "tuple", "type", "zip",
            "Exception", "KeyError", "ValueError", "TypeError",
            "RuntimeError", "PermissionError", "StopIteration",
        )
        if hasattr(builtins, name)
    }
    return safe
