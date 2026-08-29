"""The pause: one run-level state, visible, resumable, and never a failure.

WHAT CHANGED AND WHY. The pool used to raise `NoCapacity` after thirty seconds
of finding nothing available. For a per-minute bucket that is right -- thirty
seconds is longer than the wait. For a spent daily ration it is wrong in a way
that costs the whole run: the capacity is coming back in four hours, the run
has a criterion, a plan and half a level of finished work, and it throws all of
it away to report an error about a limit that is behaving exactly as documented.

The operator's decision is that a run always completes. So a wait longer than
the pool can absorb becomes a PAUSE: every in-flight agent parks on one Event,
the run's state is flushed to disk, and a ticker wakes them when the ration
frees. Nothing is lost because agents park INSIDE `pool.complete`, before any
step is committed -- a parked agent holds a checkpoint that is already
consistent, and resuming is just its next call going through.

ONE PAUSE, NOT N SLEEPS. Sixty-four agents each sleeping four hours is the same
wall-clock outcome and a completely different operational one: nothing says the
run is waiting, nothing says why, and nothing says when it comes back. The
Pacer exists so that a four-hour silence has a first-class name, one event
carrying the binding provider and dimension, and a countdown that keeps
arriving while it lasts.

--no-wait IS THE ESCAPE HATCH. CI and smoke runs want the old behaviour: fail
fast, do not sit for four hours in a pipeline. That is `Paced`, raised instead
of parking, and it is opt-in rather than the default because the default has to
be the thing that finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# How often a paused run says it is still waiting. Sixty seconds because a
# dashboard connecting mid-pause otherwise shows a blank screen for hours, and
# because it is the interval at which the wake time is re-derived: another
# replica may have freed or spent capacity since the pause began.
HEARTBEAT_S = 60.0

# A pause that ends and immediately re-forms is the SAME pause. Within this
# window a new park is reported as an extension rather than as a fresh pause, so
# an operator sees "still waiting, new ETA" instead of a stutter of pause events
# that looks like a crash loop.
EXTENSION_WINDOW_S = 60.0

# Extensions before the pause is called stalled. Three, because one extension is
# a boundary that arrived a little early and two is bad luck; three means the
# ETA is systematically wrong and someone should look. Still not a failure --
# the run keeps waiting, by decision.
STALL_AFTER_EXTENSIONS = 3

# Slack added to every computed wake. Provider clocks are not ours, and waking
# one second early costs a 429 and another pause.
WAKE_GRACE_S = 5.0

# Jitter as a fraction of the sleep, added before the final wake. N replicas
# that all park on the same reset instant would otherwise resume in lockstep and
# hand the provider the thundering herd the pause was avoiding.
WAKE_JITTER = 0.05


class Paced(RuntimeError):
    """No capacity now, and the wait is longer than this caller will accept.

    Raised instead of parking when the request asked for `no_wait`. Distinct
    from `NoCapacity` because it means the opposite thing: capacity exists and
    is coming back at a stated time, and the caller chose not to wait for it.
    """

    def __init__(self, cause: PauseCause) -> None:
        super().__init__(
            f"paced: {cause.reason} on {cause.provider} ({cause.dimension}); "
            f"capacity returns in {max(0.0, cause.resumes_in()):.0f}s"
        )
        self.cause = cause


@dataclass(frozen=True, slots=True)
class PauseCause:
    """Why the run is waiting, in the words the event stream will use.

    Carries the numbers rather than a sentence so every surface -- CLI tail,
    dashboard, API -- can render the same fact its own way without re-deriving
    anything. A pause reported as "waiting for capacity" is a pause nobody can
    act on.
    """

    reason: str
    provider: str = ""
    credential: str = ""
    dimension: str = ""
    used: int = 0
    envelope: int = 0
    resumes_at: float = 0.0
    detail: str = ""

    def resumes_in(self, now: float | None = None) -> float:
        return self.resumes_at - (now if now is not None else time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "provider": self.provider,
            "credential": self.credential,
            "dimension": self.dimension,
            "used": self.used,
            "envelope": self.envelope,
            "resumes_at": round(self.resumes_at, 3),
            "resumes_in_s": round(max(0.0, self.resumes_in()), 1),
            "detail": self.detail,
        }

    def human(self) -> str:
        """The one line every surface prints, written once here.

        Three surfaces phrasing the same pause three ways is how an operator
        ends up unsure whether they are looking at one incident or three.
        """
        span = max(0.0, self.resumes_in())
        where = f"{self.provider} {self.dimension}" if self.provider else self.reason
        figures = (
            f" {self.used:,}/{self.envelope:,}" if self.envelope else ""
        )
        return (
            f"PAUSED {self.reason}: {where}{figures}; "
            f"resumes in {span / 60:.0f}m"
        )


@dataclass
class Pacer:
    """One pause per run, shared by every agent the run has in the air.

    ANATOMY: emit
      The run's event sink. Optional so the pool can hold a pacer before a run
      exists, and so a test can construct one with no plumbing.

    ANATOMY: on_pause / on_resume
      Called once per pause, not once per agent. This is where a run releases
      resources it must not hold for hours -- a sandbox handle whose idle
      timeout is shorter than the pause -- and reacquires them on the way back.

    ANATOMY: no_wait
      Set from the run's `--no-wait`. Turns every park into a `Paced` raise, so
      the fail-fast path CI depends on is one flag rather than a second code
      path that can rot.
    """

    run_id: str = ""
    emit: Any = None
    on_pause: Any = None
    on_resume: Any = None
    no_wait: bool = False
    heartbeat_s: float = HEARTBEAT_S
    # Overshoot past the predicted opening before retrying. A field rather than
    # the bare constant for the same reason `heartbeat_s` is one: waking exactly
    # on the estimate races the provider's own clock, and how much slack that
    # needs is a deployment's decision -- and a test cannot afford five real
    # seconds per pause.
    grace_s: float = WAKE_GRACE_S
    checkpoint_path: str = ""

    paused: bool = False
    cause: PauseCause | None = None
    resumes_at: float = 0.0
    paused_since: float = 0.0
    extensions: int = 0
    pauses: int = 0
    paused_total_s: float = 0.0
    waiting: set[str] = field(default_factory=set)

    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _ticker: asyncio.Task[None] | None = None
    _last_resumed_at: float = 0.0

    # -- the pause ------------------------------------------------------

    async def park(self, cause: PauseCause, *, agent_id: str = "") -> None:
        """Hold this agent until capacity returns. Returns; never raises on wait.

        The caller re-enters the pool loop afterwards and re-evaluates from
        scratch. It must NOT assume capacity exists: another replica may have
        taken the slice between the wake and the retry, which is a second pause
        rather than a bug.
        """
        if self.no_wait:
            self._publish("run_pace_refused", cause, agent_id=agent_id)
            raise Paced(cause)

        async with self._lock:
            self.waiting.add(agent_id or f"anon-{len(self.waiting)}")
            if not self.paused:
                self._begin(cause)
            elif cause.resumes_at and cause.resumes_at < self.resumes_at:
                # An earlier opening than the one we are waiting for. Shorten
                # rather than ignore: the pause exists to end as soon as it can.
                self.resumes_at = cause.resumes_at
                self.cause = cause
                self._publish("run_pause_updated", cause)
            else:
                self._publish("run_agent_parked", cause, agent_id=agent_id)
            event = self._event

        await event.wait()

        async with self._lock:
            self.waiting.discard(agent_id or "")

    def _begin(self, cause: PauseCause) -> None:
        now = time.time()
        extended = (
            self._last_resumed_at
            and now - self._last_resumed_at <= EXTENSION_WINDOW_S
        )
        self.paused = True
        self.cause = cause
        self.resumes_at = cause.resumes_at or (now + self.heartbeat_s)
        self.paused_since = now
        self.pauses += 1
        self._event = asyncio.Event()
        if extended:
            self.extensions += 1
            self._publish(
                "run_pause_extended", cause,
                extensions=self.extensions,
                previous_eta=round(self._last_resumed_at, 3),
            )
            if self.extensions >= STALL_AFTER_EXTENSIONS:
                # Loud, and still not fatal. The run must complete; what an
                # operator needs is to know the ETA has stopped meaning anything.
                logger.error(
                    "pace stalled: %d extensions on %s (%s)",
                    self.extensions, cause.provider, cause.reason,
                )
                self._publish("pace_stalled", cause, extensions=self.extensions)
        else:
            self.extensions = 0
        if self.on_pause is not None:
            self._safely(self.on_pause, cause)
        logger.warning(
            "%s; %d agent(s) parked; state at %s",
            cause.human(), len(self.waiting), self.checkpoint_path or "(memory)",
        )
        self._publish(
            "run_paused", cause,
            waiting_agents=len(self.waiting),
            checkpoint_path=self.checkpoint_path,
        )
        self._ticker = asyncio.create_task(self._wait_out())

    async def _wait_out(self) -> None:
        """Sleep to the wake in heartbeat-sized slices.

        Sliced rather than one long sleep for two reasons: the wake time can be
        shortened while we wait (another agent found an earlier opening), and a
        pause with no heartbeat is indistinguishable from a hang to anything
        watching.
        """
        try:
            while True:
                now = time.time()
                remaining = (self.resumes_at + self.grace_s) - now
                if remaining <= 0:
                    break
                if remaining <= self.heartbeat_s:
                    await asyncio.sleep(
                        remaining + random.uniform(0, WAKE_JITTER * remaining)
                    )
                    break
                self._publish(
                    "pace_waiting",
                    self.cause,
                    resumes_in_s=round(remaining, 1),
                    waiting_agents=len(self.waiting),
                )
                await asyncio.sleep(self.heartbeat_s)
        finally:
            # Runs on cancellation too, which is the case that matters: a
            # shutdown mid-pause must still wake the parked agents, or they
            # wait on a heartbeat that will never tick again.
            await self.wake("timer")

    async def wake(self, by: str = "timer") -> None:
        """Release every parked agent. Idempotent; several paths can call it."""
        async with self._lock:
            if not self.paused:
                return
            now = time.time()
            waited = now - self.paused_since
            self.paused_total_s += waited
            self.paused = False
            self._last_resumed_at = now
            cause = self.cause
            self.cause = None
            self._event.set()
        if self.on_resume is not None:
            self._safely(self.on_resume, cause)
        logger.info("resumed after %.0fs (%s)", waited, by)
        self._publish(
            "run_resumed", cause, paused_for_s=round(waited, 1), resumed_by=by
        )

    async def aclose(self) -> None:
        """Stop the ticker without waking the run. For shutdown only."""
        ticker = self._ticker
        self._ticker = None
        if ticker is not None and not ticker.done():
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    # -- corrections ----------------------------------------------------

    def limit_corrected(
        self, *, provider: str, credential: str, declared: Any, until: float, source: str
    ) -> None:
        """Announce that the provider contradicted the declared limit.

        Emitted where the correction is APPLIED rather than logged later, so the
        event stream shows the moment a table entry stopped being believed --
        which is the only way anyone remembers to go and fix the table.
        """
        self._emit(
            "pace_limit_corrected",
            provider=provider,
            credential=credential,
            declared=declared,
            observed_reset_at=round(until, 3),
            source=source,
            human=(
                f"{provider} says its day is spent until "
                f"{time.strftime('%H:%M:%SZ', time.gmtime(until))}; the "
                f"declared limit is no longer trusted until then"
            ),
        )

    # -- plumbing -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "resumes_at": round(self.resumes_at, 3) if self.paused else None,
            "resumes_in_s": (
                round(max(0.0, self.resumes_at - time.time()), 1) if self.paused
                else None
            ),
            "waiting_agents": len(self.waiting),
            "pauses": self.pauses,
            "extensions": self.extensions,
            "paused_total_s": round(self.paused_total_s, 1),
            "cause": self.cause.to_dict() if self.cause else None,
        }

    def _publish(self, kind: str, cause: PauseCause | None, **extra: Any) -> None:
        payload = cause.to_dict() if cause else {}
        payload["human"] = cause.human() if cause else kind
        # `extra` last, and merged rather than splatted. `**payload, **extra`
        # raises TypeError on any key both carry -- `resumes_in_s` is in both,
        # so every heartbeat raised inside the ticker, the ticker's `finally`
        # woke the run, and a pause that was supposed to last hours ended
        # immediately and silently. The run then spun through the refusal it
        # had just been parked on.
        payload.update(extra)
        self._emit(kind, **payload)

    def _emit(self, kind: str, **payload: Any) -> None:
        """Hand one event to the run's sink.

        ONE positional dict with `kind` inside it, matching what the run and
        the hub already consume. The sink was previously called as
        `emit(kind, **payload)`, which no sink in the codebase accepted; every
        call raised and was swallowed by the guard below, so pause events
        reached nothing -- the dashboard, the logs and the run's own stream
        were all silent for exactly the hours an operator most needs them.
        """
        if self.emit is None:
            return
        try:
            self.emit({"kind": kind, **payload})
        except Exception:
            logger.debug("pacer event sink raised; continuing", exc_info=True)

    def _safely(self, hook: Any, cause: PauseCause | None) -> None:
        """Run a pause hook without letting it end the run.

        A sandbox that refuses to release is a resource leak; a sandbox that
        refuses to release AND takes the run down with it is an outage. The leak
        is the better failure.
        """
        try:
            hook(cause)
        except Exception:
            logger.warning("pause hook raised; continuing", exc_info=True)
