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

from swarmd.observability import metrics
from swarmd.server.hub import EventHub
from swarmd.swarm.run import PROFILES, RunResult, SwarmRun

logger = logging.getLogger(__name__)

START_TS = time.time()


class RunRequest(BaseModel):
    """Defined at MODULE scope, not inside create_app().

    With `from __future__ import annotations` every annotation is a string, and
    FastAPI resolves them against the module namespace. A model defined inside
    the factory is invisible there, so FastAPI silently reinterprets the body
    parameter as a query parameter and every POST fails validation -- a 422
    that looks like a client bug and is not.
    """

    task: str = Field(min_length=1, max_length=4000)
    profile: str = "demo"
    chaos: bool = True
    kill_rate: float = Field(default=0.2, ge=0.0, le=1.0)
    use_skills: bool = True
    ceiling_usd: float = Field(default=0.05, gt=0.0, le=10.0)


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


def create_app(
    *,
    hub: EventHub | None = None,
    provider_factory: Any = None,
    skills_path: str | None = None,
) -> Any:
    """Build the FastAPI app.

    `provider_factory` is injected so tests construct the app without a network
    and without provider keys, and so the simulated provider can be swapped in
    by configuration rather than by editing a call site.
    """
    app = FastAPI(title="swarmd", version="0.1.0")
    app.state.hub = hub or EventHub()
    app.state.registry = RunRegistry()
    app.state.provider_factory = provider_factory or _default_provider_factory
    app.state.skills_path = skills_path or os.environ.get("SWARMD_SKILLS_PATH")

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
            ceiling_usd=request.ceiling_usd,
            use_skills=request.use_skills,
            skills=(
                SkillLibrary(app.state.skills_path) if app.state.skills_path else None
            ),
            sandbox=SandboxHarness(),
            chaos=(
                ChaosHook(kill_rate=request.kill_rate) if request.chaos else None
            ),
            on_event=app.state.hub.publish,
        )
        app.state.registry.record(
            run.run_id,
            {
                "run_id": run.run_id,
                "task": request.task,
                "profile": request.profile,
                "status": "running",
                "started": time.time(),
            },
        )

        async def execute() -> RunResult:
            try:
                result = await run.run(request.task)
            finally:
                app.state.registry.record(
                    run.run_id, {"finished": time.time()}
                )
            app.state.registry.record(
                run.run_id, {"status": result.status, "report": run.report(result)}
            )
            return result

        # Fire and forget: an HTTP client must not hold a connection open for
        # the 18 minutes a demo profile takes. Progress arrives over the
        # websocket, which is what the websocket is for.
        app.state.registry.tasks[run.run_id] = asyncio.create_task(execute())
        return JSONResponse(
            {"run_id": run.run_id, "status": "accepted", "stream": "/api/stream"},
            status_code=202,
        )

    @app.get("/api/runs")
    async def list_runs() -> JSONResponse:
        return JSONResponse({"runs": app.state.registry.list()})

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        record = app.state.registry.runs.get(run_id)
        if record is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        return JSONResponse(record)

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
