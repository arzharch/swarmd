"""Service endpoints for evals, sessions, providers, and harness control.

Everything here previously existed only as a CLI subcommand, which made the
CLI the product and the service an afterthought. That is backwards: the service
is what runs, and the dashboard is how it is operated. The CLI is now a thin
client over the same paths, useful when there is no browser.

TWO THINGS ARE DELIBERATELY NOT SETTABLE OVER HTTP.

**The cost ceiling cannot be raised past a configured maximum.** A control that
the thing it controls can widen is not a control. The per-run ceiling is a
request parameter, but `SWARMD_COST_CEILING_MAX` bounds it and only the
deployment can change that.

**Provider credentials are never returned or set.** They arrive through the
environment from a secret store. An endpoint that echoed them back would put
them in a browser, a proxy log, and anyone's devtools.
"""

from __future__ import annotations

import logging
import os
from typing import Any

# Imported at module scope for the same reason as in app.py: with
# `from __future__ import annotations` FastAPI resolves annotations against
# the module namespace, and a name imported inside the function is invisible
# there -- body models silently become query parameters.
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from swarmd.server.jobs import JobKind, JobRegistry
from swarmd.swarm.run import PROFILES

logger = logging.getLogger(__name__)

# Hard bound on what a request may ask for. The per-run ceiling is a parameter
# so an operator can lower it; this is what stops anything raising it.
CEILING_MAX = float(os.environ.get("SWARMD_COST_CEILING_MAX", "0.50"))

_NOTE_THRESHOLDS = (
    "Criterion, red-team and economy thresholds are not adjustable at runtime: "
    "changing them mid-flight makes runs on either side incomparable, and "
    "comparability is the point of the ledger."
)
_NOTE_SECRETS = (
    "Provider credentials are never readable or settable here. They arrive "
    "from the secret store through the environment."
)
_NOTE_SCOPE = (
    "Changes apply to this process only. A restart returns to the deployed "
    "configuration."
)
_CONFIG_NOTES = (_NOTE_THRESHOLDS, _NOTE_SECRETS, _NOTE_SCOPE)


class EvalRequest(BaseModel):
    arms: str = Field(default="both", pattern="^(both|public|custom)$")
    repeats: int = Field(default=5, ge=1, le=20)
    profile: str = "smoke"
    holdout: bool = False
    ceiling_usd: float = Field(default=0.05, gt=0.0)


class SessionRequest(BaseModel):
    tasks: int = Field(default=10, ge=1, le=200)
    profile: str = "smoke"
    consolidate_every: int = Field(default=5, ge=1, le=50)
    use_skills: bool = True
    auto_approve: bool = False
    ceiling_usd: float = Field(default=0.05, gt=0.0)
    # Fleet self-correction: the supervisor rewrites the worker prompt when
    # criterion failures cluster, and reverts a rewrite that did not help.
    # Off by default because a patched prompt is a confound -- an arm must be
    # able to run with the stock prompt and know that is what it ran with.
    supervisor: bool = False


class ConfigPatch(BaseModel):
    """Runtime-adjustable harness knobs.

    Only values that are safe to change between runs and that an operator has a
    real reason to touch. Anything structural -- criterion thresholds, red-team
    detector thresholds, the economy's reward ratio -- is deliberately absent:
    changing those mid-flight would make runs on either side of the change
    incomparable, and comparability is the point of the ledger.
    """

    default_profile: str | None = None
    default_ceiling_usd: float | None = Field(default=None, gt=0.0)
    chaos_kill_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    sandbox_timeout_s: float | None = Field(default=None, gt=0.0, le=300.0)
    sandbox_memory_mb: int | None = Field(default=None, ge=64, le=4096)
    allow_paid: bool | None = None


class HarnessConfig:
    """Mutable-at-runtime settings, with the immutable ones alongside.

    Defaults come from the environment so the deployment ConfigMap remains the
    source of truth; the dashboard adjusts them for the current process only.
    A restart returns to the deployed configuration, which is intentional --
    a knob turned in a browser at 2am should not silently outlive the incident.
    """

    def __init__(self) -> None:
        self.default_profile = "smoke"
        self.default_ceiling_usd = float(
            os.environ.get("SWARMD_COST_CEILING_USD", "0.05")
        )
        self.chaos_kill_rate = 0.2
        self.sandbox_timeout_s = 30.0
        self.sandbox_memory_mb = 512
        self.allow_paid = os.environ.get("SWARMD_ALLOW_PAID", "").lower() in {
            "1", "true", "yes",
        }

    def apply(self, patch: ConfigPatch) -> list[str]:
        """Apply a patch. Returns what changed, for the audit line."""
        changed: list[str] = []
        for name in (
            "default_profile", "default_ceiling_usd", "chaos_kill_rate",
            "sandbox_timeout_s", "sandbox_memory_mb", "allow_paid",
        ):
            value = getattr(patch, name)
            if value is None or value == getattr(self, name):
                continue
            if name == "default_profile" and value not in PROFILES:
                raise ValueError(f"unknown profile {value!r}")
            if name == "default_ceiling_usd" and float(value) > CEILING_MAX:
                raise ValueError(
                    f"ceiling {value} exceeds the deployment maximum "
                    f"{CEILING_MAX}. A control the controlled thing can widen "
                    f"is not a control; raise SWARMD_COST_CEILING_MAX in the "
                    f"deployment if this is intended."
                )
            changed.append(f"{name}: {getattr(self, name)} -> {value}")
            setattr(self, name, value)
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "adjustable": {
                "default_profile": self.default_profile,
                "default_ceiling_usd": self.default_ceiling_usd,
                "chaos_kill_rate": self.chaos_kill_rate,
                "sandbox_timeout_s": self.sandbox_timeout_s,
                "sandbox_memory_mb": self.sandbox_memory_mb,
                "allow_paid": self.allow_paid,
            },
            # Shown so an operator can see what they cannot change and why,
            # rather than discovering it from a rejected request.
            "fixed": {
                "ceiling_max_usd": CEILING_MAX,
                "profiles": {
                    name: {
                        "agents": p.agents,
                        "proposers": p.proposers,
                        "max_repairs": p.max_repairs,
                        "target_calls": p.target_calls,
                        "description": p.description,
                    }
                    for name, p in PROFILES.items()
                },
                "notes": list(_CONFIG_NOTES),
            },
        }


def register(
    app: FastAPI, *, registry: JobRegistry, config: HarnessConfig
) -> None:
    """Attach the control endpoints to an existing app."""
    # -- jobs ---------------------------------------------------------------

    @app.get("/api/jobs")
    async def list_jobs(kind: str = "") -> JSONResponse:
        selected = JobKind(kind) if kind else None
        return JSONResponse(
            {
                "jobs": [
                    j.to_dict(include_report=False) for j in registry.list(selected)
                ]
            }
        )

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(404, f"unknown job: {job_id}")
        return JSONResponse(job.to_dict())

    @app.delete("/api/jobs/{job_id}")
    async def cancel_job(job_id: str) -> JSONResponse:
        try:
            return JSONResponse(registry.cancel(job_id).to_dict(include_report=False))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # -- evals --------------------------------------------------------------

    @app.post("/api/evals")
    async def start_eval(request: EvalRequest) -> JSONResponse:
        """Run both arms over the task suite.

        Always both arms: the harness refuses to emit an improvement figure
        without a paired control (ADR-007), so offering a treatment-only sweep
        would only produce a report that declines to conclude anything.
        """
        if request.profile not in PROFILES:
            raise HTTPException(400, f"unknown profile; known: {sorted(PROFILES)}")
        if request.ceiling_usd > CEILING_MAX:
            raise HTTPException(400, f"ceiling exceeds maximum {CEILING_MAX}")

        job = registry.submit(
            JobKind.EVAL,
            f"eval · {request.arms} · {request.repeats} repeats",
            _eval_runner(app, request, registry),
            params=request.model_dump(),
        )
        return JSONResponse(
            job.to_dict(include_report=False), status_code=202
        )

    # -- sessions -----------------------------------------------------------

    @app.post("/api/sessions")
    async def start_session(request: SessionRequest) -> JSONResponse:
        if request.profile not in PROFILES:
            raise HTTPException(400, f"unknown profile; known: {sorted(PROFILES)}")
        if request.ceiling_usd > CEILING_MAX:
            raise HTTPException(400, f"ceiling exceeds maximum {CEILING_MAX}")
        if request.auto_approve:
            logger.warning(
                "session %s requested auto-approve: skills will enter the "
                "library with no human review", request.tasks,
            )

        job = registry.submit(
            JobKind.SESSION,
            f"session · {request.tasks} tasks · "
            f"{'treatment' if request.use_skills else 'control'}",
            _session_runner(app, request, registry),
            params=request.model_dump(),
            total=request.tasks,
        )
        return JSONResponse(job.to_dict(include_report=False), status_code=202)

    # -- providers ----------------------------------------------------------

    @app.get("/api/providers")
    async def provider_status() -> JSONResponse:
        """Pool state. Never includes credentials."""
        try:
            pool = app.state.provider_factory()
        except RuntimeError as exc:
            return JSONResponse(
                {"available": False, "detail": str(exc), "providers": []},
                status_code=503,
            )
        status = pool.status() if hasattr(pool, "status") else []
        return JSONResponse({"available": True, "providers": status})

    @app.post("/api/providers/probe")
    async def probe_providers() -> JSONResponse:
        """Ask each provider what it will actually serve.

        Published free-tier limits disagree across sources and change without
        notice, so this replaces documentation with observation (ADR-008).
        """
        try:
            pool = app.state.provider_factory()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not hasattr(pool, "probe"):
            # A single provider rather than a pool. Saying so beats a 500.
            raise HTTPException(
                501, "the configured provider does not support probing"
            )
        rows = await pool.probe()
        live = sum(1 for r in rows if r.get("ok"))
        return JSONResponse({"live": live, "total": len(rows), "providers": rows})

    # -- harness config -----------------------------------------------------

    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        return JSONResponse(config.to_dict())

    @app.patch("/api/config")
    async def patch_config(patch: ConfigPatch, actor: str = "dashboard") -> JSONResponse:
        try:
            changed = config.apply(patch)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if changed:
            # Logged rather than silent: a knob turned during an incident is
            # something the postmortem needs.
            logger.warning("harness config changed by %s: %s", actor, "; ".join(changed))
        return JSONResponse({"changed": changed, "config": config.to_dict()})


# --- job bodies ------------------------------------------------------------


def _eval_runner(app: FastAPI, request: EvalRequest, registry: JobRegistry) -> Any:
    async def run(job: Any) -> dict[str, Any]:
        from examples.tasks.suite import suite
        from swarmd.harnesses.sandbox import SandboxHarness
        from swarmd.swarm.evaluate import Evaluator
        from swarmd.swarm.run import SwarmRun

        pool = app.state.provider_factory()
        sandbox = SandboxHarness()
        tasks = suite(arms=request.arms, include_holdout=request.holdout)
        registry.progress(job, 0, len(tasks) * request.repeats * 2)
        completed = 0

        async def run_factory(task: Any, use_skills: bool, seed: int) -> Any:
            nonlocal completed
            run = SwarmRun(
                pool,
                profile=request.profile,
                ceiling_usd=request.ceiling_usd,
                use_skills=use_skills,
                sandbox=sandbox,
                run_id=f"{job.job_id}-{task.task_id}-{seed}-"
                f"{'t' if use_skills else 'c'}",
                on_event=app.state.hub.publish,
            )
            result = await run.run(task.prompt)
            completed += 1
            registry.progress(job, completed)
            return result, run.report(result)

        report = await Evaluator(run_factory, repeats=request.repeats).evaluate(tasks)
        return report.to_dict()

    return run


def _session_runner(app: FastAPI, request: SessionRequest, registry: JobRegistry) -> Any:
    async def run(job: Any) -> dict[str, Any]:
        from examples.tasks.suite import suite
        from swarmd.harnesses.sandbox import SandboxHarness
        from swarmd.hitl.approvals import ApprovalManager
        from swarmd.hitl.stores import build_approval_store
        from swarmd.swarm.run import SwarmRun
        from swarmd.swarm.session import SwarmSession
        from swarmd.swarm.skills import SkillLibrary
        from swarmd.swarm.supervisor import Supervisor

        pool = app.state.provider_factory()
        library = SkillLibrary(app.state.skills_path)
        approvals = ApprovalManager(build_approval_store())
        sandbox = SandboxHarness()
        completed = 0

        async def run_factory(
            task: str, index: int, system_prompt: str = ""
        ) -> Any:
            nonlocal completed
            run = SwarmRun(
                pool,
                profile=request.profile,
                ceiling_usd=request.ceiling_usd,
                use_skills=request.use_skills,
                skills=library,
                approvals=approvals,
                sandbox=sandbox,
                # A session works through a curriculum of related tasks, which
                # is where cache repetition actually lives. Eval runs below get
                # no cache: SwarmRun refuses one, because identical cached
                # repeats collapse the bootstrap interval.
                cache=app.state.cache,
                run_id=f"{job.job_id}-{index:03d}",
                system_prompt=system_prompt,
                on_event=app.state.hub.publish,
            )
            result = await run.run(task)
            completed += 1
            registry.progress(job, completed)
            return result, run.report(result)

        prompts = [t.prompt for t in suite(arms="both")]
        # Cycle rather than silently running fewer: a session of 40 that ran 10
        # would report a curve over a quarter of the requested evidence.
        tasks = [prompts[i % len(prompts)] for i in range(request.tasks)]

        session = SwarmSession(
            run_factory,
            library,
            approvals=approvals,
            consolidate_every=request.consolidate_every,
            auto_approve=request.auto_approve,
            skills_enabled=request.use_skills,
            supervisor=Supervisor() if request.supervisor else None,
        )
        return (await session.run(tasks)).to_dict()

    return run
