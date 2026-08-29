"""Control plane: submit runs, stream events, serve health and metrics.

Stateless by design. Every piece of state a run depends on -- checkpoints,
ledger, approvals, the skill library, frozen criteria -- lives outside the
process, which is what makes a rolling deploy safe and why in-flight runs
survive a control-plane restart (docs/DEPLOYMENT.md section 6).

The two probes answer DIFFERENT questions, and conflating them would be a real
outage:

    /healthz  is the process alive?          -> liveness
    /readyz   can it take NEW work?          -> readiness

A saturated pool makes /readyz fail so no new runs arrive, while /healthz stays
green so the pod is not killed and does not lose the runs it already holds. If
liveness used the same signal, a capacity event would become a correctness
event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import asdict
from typing import Any

# Imported at MODULE scope, not inside create_app(). With
# `from __future__ import annotations` every annotation is a string that
# FastAPI resolves against the module namespace, so a name imported inside
# the factory is invisible: FastAPI silently reinterprets `websocket:
# WebSocket` as a query parameter and the handshake fails with a 1008.
# This module is only imported by `swarmd serve`, so requiring the optional
# serve extra here costs nothing elsewhere.
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from swarmd.observability import logs, metrics
from swarmd.router.cache import SemanticCache
from swarmd.server import control, middleware
from swarmd.server.hub import EventHub
from swarmd.server.jobs import JobRegistry
from swarmd.swarm.run import PROFILES, RunResult, SwarmRun
from swarmd.swarm.runstore import IncompatibleRunState, RunStore

logger = logging.getLogger(__name__)

START_TS = time.time()

# A run that reached one of these is finished. Resuming it would re-run
# distillation over work already banked and report a second result for the same
# run id. "interrupted" is deliberately NOT here: that is exactly the state a
# resume is for.
TERMINAL = {"completed", "failed_criterion", "aborted", "error", "cancelled"}

# Ledger rows retained per run in the in-process index. Why 5000: enough to
# reconstruct a standard run end to end, bounded so a deep run does not pin
# tens of thousands of rows in a long-lived control plane.
LEDGER_ROWS_KEPT = 5000


class RunRequest(BaseModel):
    """Defined at MODULE scope, not inside create_app().

    With `from __future__ import annotations` every annotation is a string, and
    FastAPI resolves them against the module namespace. A model defined inside
    the factory is invisible there, so FastAPI silently reinterprets the body
    parameter as a query parameter and every POST fails validation -- a 422
    that looks like a client bug and is not.
    """

    task: str = Field(min_length=1, max_length=4000)
    profile: str = "standard"
    chaos: bool = True
    kill_rate: float = Field(default=0.2, ge=0.0, le=1.0)
    use_skills: bool = True
    ceiling_usd: float = Field(default=0.05, gt=0.0, le=10.0)
    # How many agents to run. None means the profile decides. Bounded at 2000
    # here rather than left open because this arrives over HTTP from a client
    # that may have a typo in it, and the validation error is a better outcome
    # than a run that spends its whole ceiling discovering the same thing.
    agents: int | None = Field(default=None, ge=1, le=2000)
    # Deliberate misbehaviour to inject: "all", or a comma-separated subset of
    # the five patterns. Empty means a clean run.
    seed_rogues: str = ""
    # What to do when the session's ration is spent. The default waits, because
    # a run that resumes hours later still delivers what was asked for and a
    # run that dies has thrown away everything it already paid for. A caller on
    # a deadline -- CI, an eval sweep -- sets this and gets a prompt failure
    # naming the wait instead.
    no_wait: bool = False


class RunRegistry:
    """In-process record of runs this pod started.

    Deliberately not the source of truth -- that is the ledger and Postgres.
    This is a convenience index so the dashboard can list what is happening
    without a database round trip, and losing it on restart costs nothing.
    """

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}

    def record(self, run_id: str, payload: dict[str, Any]) -> None:
        self.runs[run_id] = {**self.runs.get(run_id, {}), **payload}

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            self.runs.values(), key=lambda r: r.get("started", 0), reverse=True
        )


@contextlib.asynccontextmanager
async def _lifespan(app: Any) -> Any:
    """Startup and shutdown for the control plane.

    Nothing to do on the way in. On the way out, say what happened to the runs
    this pod was carrying -- see `_mark_in_flight_interrupted`.
    """
    yield
    await _mark_in_flight_interrupted(app)


async def _mark_in_flight_interrupted(app: Any) -> None:
    """Record that in-flight runs stopped, so the listing stays honest.

    Runs are background tasks, not request handlers, so uvicorn considers
    itself idle and exits while they are still going -- the 120s grace period
    in the Deployment was being spent waiting for nothing. The work itself is
    safe: the run store persists at every boundary that costs money, so a
    resume picks up where it stopped.

    What was NOT safe was the record. A killed run's document still said
    "running", so `swarmd runs list` and /api/runs/resumable reported work in
    progress that no process was doing, indefinitely. Marking it is the
    difference between a resumable run and a lie.
    """
    store = app.state.run_store
    for run_id, task in list(app.state.registry.tasks.items()):
        if task.done():
            continue
        task.cancel()
        app.state.registry.record(run_id, {"status": "interrupted"})
        try:
            state = store.load(run_id)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("could not mark run %s interrupted: %s", run_id, exc)
            continue
        if state is None or state.status in TERMINAL:
            continue
        # Not "paused": nothing is waiting on a clock, and a resume should not
        # tell an operator to expect it to come back on its own.
        state.status = "interrupted"
        state.paused_reason = "control plane shut down"
        state.resumes_at = 0.0
        store.save(state)
        logger.warning("run %s interrupted by shutdown; resumable", run_id)


def create_app(
    *,
    hub: EventHub | None = None,
    provider_factory: Any = None,
    skills_path: str | None = None,
    api_token: str | None = None,
) -> Any:
    """Build the FastAPI app.

    `provider_factory` is injected so tests construct the app without a network
    and without provider keys, and so the simulated provider can be swapped in
    by configuration rather than by editing a call site.
    """
    logs.configure()
    app = FastAPI(
        title="swarmd",
        version="0.1.0",
        # No interactive docs in a deployed service. They are a live client for
        # every endpoint, served without the operator token, to anyone who
        # reaches the port. Useful locally; not a thing to expose.
        docs_url="/docs" if os.environ.get("SWARMD_ENV", "dev") == "dev" else None,
        redoc_url=None,
        lifespan=_lifespan,
        openapi_url="/openapi.json"
        if os.environ.get("SWARMD_ENV", "dev") == "dev"
        else None,
    )
    middleware.install(app, token=api_token)
    app.state.hub = hub or EventHub()
    app.state.registry = RunRegistry()
    app.state.provider_factory = provider_factory or _default_provider_factory
    app.state.skills_path = skills_path or os.environ.get("SWARMD_SKILLS_PATH")
    # Evals and sessions are long-running work of the same shape as runs, so
    # they share one registry: one status endpoint, one cancel semantic, one
    # progress event, rather than three of each.
    # ONE cache for the process, shared by every run this pod serves. A
    # per-run cache would never hit: within a single run each node's prompt
    # differs and each repair prompt carries its own candidate's failures. The
    # repetition worth paying for is ACROSS runs -- a session working through a
    # curriculum, or two operators asking similar things.
    app.state.cache = SemanticCache(ttl_s=3600.0, capacity=2048, exact_only=True)
    # Durable working sets. Survives the pod, not just the request: a run parked
    # on a spent ration outlives any deployment, and the registry above is an
    # in-process index that a restart empties.
    app.state.run_store = RunStore()
    # Finished run documents age out; paused ones never do. Done at startup
    # rather than on a timer because the store grows per run, not per second,
    # and a pod that restarts weekly is exactly when the sweep should happen.
    app.state.run_store.prune()
    app.state.jobs = JobRegistry(hub=app.state.hub)
    app.state.config = control.HarnessConfig()
    control.register(app, registry=app.state.jobs, config=app.state.config)

    # -- probes -------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness. Green whenever the process can serve, so a saturated pod
        is not killed and does not lose the runs it is already holding."""
        return JSONResponse(
            {
                "status": "ok",
                "uptime_s": round(time.time() - START_TS, 1),
                # Injected at build time so a running pod traces to a commit
                # without guessing from an image tag.
                "git_sha": os.environ.get("SWARMD_GIT_SHA", "unknown"),
                "build_time": os.environ.get("SWARMD_BUILD_TIME", "unknown"),
            }
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness. Red when no provider capacity exists, so new runs go
        elsewhere while this pod keeps working on what it has."""
        try:
            provider = app.state.provider_factory()
        except RuntimeError as exc:
            return JSONResponse(
                {"status": "no_provider_capacity", "detail": str(exc)},
                status_code=503,
            )
        status = provider.status() if hasattr(provider, "status") else []
        available = [row for row in status if row.get("available", True)]
        if status and not available:
            return JSONResponse(
                {"status": "all_providers_backed_off", "providers": status},
                status_code=503,
            )
        return JSONResponse({"status": "ready", "providers": status})

    @app.get("/metrics")
    async def prometheus_metrics() -> PlainTextResponse:
        """Served in-process rather than on a second listener, so one port and
        one NetworkPolicy rule cover it."""
        return PlainTextResponse(
            metrics.render().decode("utf-8"),
            media_type="text/plain; version=0.0.4",
        )

    # -- runs ---------------------------------------------------------------

    @app.post("/api/runs")
    async def submit_run(request: RunRequest) -> JSONResponse:
        if request.profile not in PROFILES:
            raise HTTPException(400, f"unknown profile; known: {sorted(PROFILES)}")

        from swarmd.swarm.rogues import UnknownRogue, parse_patterns

        try:
            parse_patterns(request.seed_rogues)
        except UnknownRogue as exc:
            # 400 rather than seeding nothing: a typo that produces a clean run
            # reads exactly like a red-team gate that passed.
            raise HTTPException(400, str(exc)) from exc

        from swarmd.chaos import ChaosHook
        from swarmd.harnesses.sandbox import SandboxHarness
        from swarmd.swarm.skills import SkillLibrary

        try:
            provider = app.state.provider_factory()
        except RuntimeError as exc:
            raise HTTPException(503, f"no provider capacity: {exc}") from exc

        run = SwarmRun(
            provider,
            profile=request.profile,
            agents=request.agents,
            seed_rogues=request.seed_rogues,
            ceiling_usd=request.ceiling_usd,
            use_skills=request.use_skills,
            skills=(
                SkillLibrary(app.state.skills_path) if app.state.skills_path else None
            ),
            sandbox=SandboxHarness(),
            cache=app.state.cache,
            chaos=(
                ChaosHook(kill_rate=request.kill_rate) if request.chaos else None
            ),
            on_event=app.state.hub.publish,
            no_wait=request.no_wait,
            store=app.state.run_store,
        )
        app.state.registry.record(
            run.run_id,
            {
                "run_id": run.run_id,
                "task": request.task,
                "profile": request.profile,
                "agents": run.agents,
                "seed_rogues": request.seed_rogues,
                "status": "running",
                "started": time.time(),
            },
        )
        return _accept(run, request.task)

    def _accept(run: SwarmRun, task: str) -> JSONResponse:
        """Start a run in the background and answer immediately.

        Shared by submit and resume so a resumed run reports through exactly
        the same registry fields -- a second copy of this drifted into
        reporting a resumed run as permanently "running", because only one of
        them recorded the terminal status.
        """

        async def execute() -> RunResult:
            try:
                result = await run.run(task)
            finally:
                app.state.registry.record(run.run_id, {"finished": time.time()})
            app.state.registry.record(
                run.run_id,
                {
                    "status": result.status,
                    "report": run.report(result),
                    # Ledger rows are kept so the traceability view can show the
                    # actual audit record rather than a prettier log. Capped:
                    # a deep run writes tens of thousands and the registry is a
                    # convenience index, not the durable store -- the JSONL file
                    # and Postgres hold the complete record.
                    "ledger": [asdict(row) for row in run.account.ledger.rows()][
                        -LEDGER_ROWS_KEPT:
                    ],
                    "verify": run.account.verify(),
                },
            )
            return result

        # Fire and forget: an HTTP client must not hold a connection open for
        # the 18 minutes a demo profile takes -- let alone the hours a rationed
        # run waits. Progress arrives over the websocket, which is what the
        # websocket is for.
        app.state.registry.tasks[run.run_id] = asyncio.create_task(execute())
        return JSONResponse(
            {"run_id": run.run_id, "status": "accepted", "stream": "/api/stream"},
            status_code=202,
        )

    @app.get("/api/pace")
    async def pace() -> JSONResponse:
        """Whether the pool is waiting on a ration, and when it comes back.

        A paused run looks identical to a hung one from outside: no events, no
        errors, nothing finishing. This is what distinguishes them, and it is
        why the dashboard can say "back at 18:40" instead of going quiet.
        """
        try:
            provider = app.state.provider_factory()
        except RuntimeError as exc:
            return JSONResponse({"status": "no_provider_capacity", "detail": str(exc)})
        status = provider.pace_status() if hasattr(provider, "pace_status") else {}
        return JSONResponse(status or {"paused": False})

    @app.get("/api/runs/resumable")
    async def list_resumable(all: bool = False) -> JSONResponse:
        """Runs on disk that can be picked back up.

        Deliberately NOT the in-process registry: that is emptied by a restart,
        and a run parked on a spent ration is precisely the run most likely to
        outlive the pod that started it. Registered before the
        `/api/runs/{run_id}` route so "resumable" is not read as a run id.
        """
        return JSONResponse(
            {
                "runs": [
                    {
                        "run_id": s.run_id,
                        "task": s.task,
                        "profile": s.profile,
                        "agents": s.agents,
                        "status": s.status,
                        "paused_reason": s.paused_reason,
                        "resumes_at": s.resumes_at,
                        "nodes_done": len(s.finished_nodes),
                        "has_criterion": bool(s.criterion),
                        "has_plan": bool(s.plan),
                    }
                    for s in app.state.run_store.list_runs()
                    if all or s.status not in TERMINAL
                ]
            }
        )

    @app.get("/api/runs")
    async def list_runs() -> JSONResponse:
        return JSONResponse({"runs": app.state.registry.list()})

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        record = app.state.registry.runs.get(run_id)
        if record is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        # The ledger is served separately: it is by far the largest field, and
        # the summary is polled while the traceability view is not open.
        return JSONResponse({k: v for k, v in record.items() if k != "ledger"})

    @app.get("/api/runs/{run_id}/ledger")
    async def get_ledger(run_id: str, kind: str = "", limit: int = 500) -> JSONResponse:
        """The append-only record a result is traceable to (ADR-007).

        Every number in every report is an aggregate over these rows, so this
        endpoint is what makes a reported figure checkable rather than trusted.
        """
        record = app.state.registry.runs.get(run_id)
        if record is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        rows = record.get("ledger", [])
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return JSONResponse(
            {
                "run_id": run_id,
                "rows": rows[-limit:],
                "total": len(record.get("ledger", [])),
                "kinds": sorted({r.get("kind", "") for r in record.get("ledger", [])}),
                # memory-vs-disk reconciliation; a mismatch means a torn write
                "verify": record.get("verify", {}),
            }
        )

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> JSONResponse:
        """Pick a stored run back up, buying nothing it already paid for.

        The criterion, plan and batch drafts come off disk. Re-deriving the
        criterion would be worse than wasteful: the second half of the run
        would be graded against a target the first half never saw, while the
        report still quoted one hash for both.
        """
        try:
            stored = app.state.run_store.load(run_id)
        except IncompatibleRunState as exc:
            # 409, not 500: the document is intact and the request is well
            # formed. This build simply cannot read that shape, and no retry
            # will change it.
            raise HTTPException(409, str(exc)) from exc
        if stored is not None and stored.status in TERMINAL:
            raise HTTPException(
                409,
                f"run {run_id} already finished ({stored.status}); resuming it "
                f"would re-run distillation over work already banked",
            )

        existing = app.state.registry.tasks.get(run_id)
        if existing is not None and not existing.done():
            # Two live runs sharing a run id would interleave their writes into
            # one document and one ledger, and the resulting report would
            # describe neither.
            raise HTTPException(409, f"run {run_id} is already running")

        try:
            provider = app.state.provider_factory()
        except RuntimeError as exc:
            raise HTTPException(503, f"no provider capacity: {exc}") from exc

        from swarmd.harnesses.sandbox import SandboxHarness
        from swarmd.swarm.skills import SkillLibrary

        try:
            run = SwarmRun.resume(
                run_id,
                provider,
                store=app.state.run_store,
                skills=(
                    SkillLibrary(app.state.skills_path)
                    if app.state.skills_path
                    else None
                ),
                sandbox=SandboxHarness(),
                cache=app.state.cache,
                on_event=app.state.hub.publish,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except IncompatibleRunState as exc:
            # 409, not 500: the document is intact and the request is
            # well-formed. What is wrong is that this build cannot read that
            # shape, and no retry will change it.
            raise HTTPException(409, str(exc)) from exc

        task = run.state.task
        if not task:
            raise HTTPException(
                422,
                f"stored run {run_id} has no task recorded; it was interrupted "
                f"before the task was persisted and cannot be resumed",
            )

        app.state.registry.record(
            run.run_id,
            {
                "run_id": run.run_id,
                "task": task,
                "profile": run.state.profile,
                "agents": run.agents,
                "status": "running",
                "resumed": time.time(),
                "resumed_from_nodes": len(run.state.finished_nodes),
            },
        )
        return _accept(run, task)

    @app.delete("/api/runs/{run_id}")
    async def cancel_run(run_id: str) -> JSONResponse:
        task = app.state.registry.tasks.get(run_id)
        if task is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        task.cancel()
        app.state.registry.record(run_id, {"status": "cancelled"})
        return JSONResponse({"run_id": run_id, "status": "cancelled"})

    # -- approvals ----------------------------------------------------------

    @app.get("/api/approvals")
    async def list_approvals() -> JSONResponse:
        """The human queue. Outreach and skill entry both terminate here."""
        from swarmd.hitl.approvals import ApprovalManager
        from swarmd.hitl.stores import build_approval_store

        pending = await ApprovalManager(build_approval_store()).pending()
        metrics.set_approvals_pending(len(pending))
        return JSONResponse(
            {
                "pending": [
                    {
                        "request_id": r.request_id,
                        "stage": r.stage,
                        "item": r.item,
                        "waited_s": round(time.time() - r.created_ts, 1),
                    }
                    for r in pending
                ]
            }
        )

    @app.post("/api/approvals/{request_id}/{action}")
    async def decide_approval(
        request_id: str, action: str, actor: str = "dashboard"
    ) -> JSONResponse:
        from swarmd.hitl.approvals import ApprovalManager
        from swarmd.hitl.stores import build_approval_store

        if action not in {"approve", "reject"}:
            raise HTTPException(400, "action must be approve or reject")
        manager = ApprovalManager(build_approval_store())

        # A skill decision must reach the library too, or the audit trail says
        # approved while the skill stays unusable.
        from swarmd.hitl.skill_gate import STAGE as SKILL_STAGE
        from swarmd.hitl.skill_gate import SkillGate
        from swarmd.swarm.skills import SkillLibrary

        existing = await manager.store.get_request(request_id)
        if existing is not None and existing.stage == SKILL_STAGE:
            gate = SkillGate(manager, SkillLibrary(app.state.skills_path))
            try:
                decision = await gate.decide(request_id, action, actor=actor)
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return JSONResponse(
                {
                    "request_id": decision.request.request_id,
                    "state": decision.request.state.value,
                    "applied": decision.applied,
                    "detail": decision.detail,
                }
            )

        try:
            decided = await manager.decide(request_id, action, actor=actor)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            # Already decided. Immutable by design, so this is a 409 rather
            # than an error -- the caller's view was simply stale.
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse(
            {"request_id": decided.request_id, "state": decided.state.value}
        )

    # -- skills -------------------------------------------------------------

    @app.get("/api/skills")
    async def list_skills() -> JSONResponse:
        from swarmd.swarm.skills import SkillLibrary

        library = SkillLibrary(app.state.skills_path)
        return JSONResponse(
            {
                "stats": library.stats(),
                "pending": [s.to_dict() for s in library.pending()],
                "approved": [s.to_dict() for s in library.approved()],
            }
        )

    @app.post("/api/skills/{skill_id}/{action}")
    async def decide_skill(
        skill_id: str, action: str, actor: str = "dashboard"
    ) -> JSONResponse:
        """The gate on what the system is allowed to learn."""
        from swarmd.swarm.skills import SkillLibrary, SkillLibraryError

        if action not in {"approve", "reject"}:
            raise HTTPException(400, "action must be approve or reject")
        library = SkillLibrary(app.state.skills_path)
        try:
            skill = (
                library.approve(skill_id, actor=actor) if action == "approve"
                else library.reject(skill_id, actor=actor)
            )
        except SkillLibraryError as exc:
            raise HTTPException(404, str(exc)) from exc
        return JSONResponse(skill.to_dict())

    # -- streaming ----------------------------------------------------------

    @app.get("/api/events")
    async def recent_events(limit: int = 200) -> JSONResponse:
        """Polling fallback for clients that cannot hold a websocket."""
        return JSONResponse({"events": app.state.hub.history(limit)})

    @app.get("/api/stream/stats")
    async def stream_stats() -> JSONResponse:
        return JSONResponse(app.state.hub.stats())

    @app.websocket("/api/stream")
    async def stream(websocket: WebSocket) -> None:
        """Live event stream. The dashboard's only data source.

        Replays recent history on connect so a viewer arriving at minute ten of
        an eighteen-minute run sees the run so far rather than a blank page.
        """
        # The stream carries every prompt, thought and artifact a run
        # produces, so it is gated exactly like the mutating endpoints. A
        # browser cannot set Authorization on a handshake, hence the query
        # parameter and the X-Swarmd-Token header both being accepted.
        expected = app.state.api_token
        if expected:
            supplied = (
                websocket.query_params.get("token")
                or middleware.extract_token(websocket.headers)
            )
            if not middleware.token_matches(supplied, expected):
                await websocket.close(code=1008, reason="operator token required")
                return

        await websocket.accept()
        subscriber = app.state.hub.subscribe(replay=True)
        try:
            while True:
                event = await subscriber.queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("stream closed", exc_info=True)
        finally:
            app.state.hub.unsubscribe(subscriber)
            with contextlib.suppress(Exception):
                await websocket.close()

    return app


def _default_provider_factory() -> Any:
    """Build the pool from the environment.

    Raises rather than falling back to anything synthetic: a control plane with
    no providers should report 503 and be taken out of rotation, not quietly
    serve fabricated results (ADR-006, ADR-012).
    """
    from swarmd.router.pool import ProviderPool

    return ProviderPool.from_env(
        allow_data_training=os.environ.get(
            "SWARMD_ALLOW_DATA_TRAINING", ""
        ).lower() in {"1", "true", "yes"},
        allow_paid=os.environ.get("SWARMD_ALLOW_PAID", "").lower()
        in {"1", "true", "yes"},
    )
