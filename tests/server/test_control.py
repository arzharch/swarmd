"""Service control surface: evals, sessions, providers, harness config.

All of this previously existed only as CLI subcommands, which made the CLI the
product and the service an afterthought. These tests assert the service can do
everything the CLI could, and that two things remain impossible over HTTP:
raising the cost ceiling past the deployment's maximum, and reading provider
credentials.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from swarmd.server.app import create_app
from swarmd.server.control import CEILING_MAX, ConfigPatch, HarnessConfig
from swarmd.server.jobs import JobKind, JobRegistry, JobState
from tests.server.test_app import FakeProvider


@pytest.fixture
def skills_path(tmp_path):
    """A library with something approved in it.

    An eval whose treatment arm has nothing to retrieve compares a
    configuration against itself, so `/api/evals` refuses to start one. A
    fixture with an empty library would therefore be testing the refusal in
    every test that only wants a sweep.
    """
    from swarmd.swarm.skills import SkillLibrary

    path = tmp_path / "skills.json"
    library = SkillLibrary(str(path))
    skill = library.propose(
        name="extract amounts",
        task_pattern="extract every monetary amount",
        instruction="Read the amounts in document order and keep the currency.",
    )
    library.approve(skill.skill_id, actor="test")
    return str(path)


@pytest.fixture
def client(skills_path):
    app = create_app(provider_factory=FakeProvider, skills_path=skills_path)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_without_skills(tmp_path):
    """A control plane started with no library at all, which is the default."""
    app = create_app(provider_factory=FakeProvider)
    with TestClient(app) as c:
        yield c


def _await_job(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["state"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# --- the job registry ------------------------------------------------------


async def test_a_job_reports_progress_as_counts_not_a_percentage():
    """A percentage hides whether 50% means 1 of 2 or 500 of 1000."""
    registry = JobRegistry()

    async def body(job):
        registry.progress(job, 3, 10)
        return {"ok": True}

    job = registry.submit(JobKind.EVAL, "eval", body)
    await job.task
    assert job.done == 3
    assert job.total == 10


async def test_a_failing_job_records_the_error_rather_than_crashing():
    registry = JobRegistry()

    async def body(job):
        raise RuntimeError("provider exhausted")

    job = registry.submit(JobKind.RUN, "run", body)
    await job.task
    assert job.state is JobState.FAILED
    assert "provider exhausted" in job.error


async def test_a_cancelled_job_is_not_reported_as_completed():
    """Swallowing the cancellation would make it look like it finished."""
    import asyncio

    registry = JobRegistry()

    async def body(job):
        await asyncio.sleep(10)
        return {}

    job = registry.submit(JobKind.SESSION, "session", body)
    await asyncio.sleep(0.05)
    registry.cancel(job.job_id)
    with pytest.raises(asyncio.CancelledError):
        await job.task
    assert job.state is JobState.CANCELLED


async def test_finished_jobs_are_pruned_so_the_index_stays_bounded():
    registry = JobRegistry(max_finished=3)

    async def body(job):
        return {}

    jobs = [registry.submit(JobKind.RUN, f"r{i}", body) for i in range(6)]
    for job in jobs:
        await job.task
    assert len(registry.list()) <= 3


async def test_a_broken_hub_never_fails_a_job():
    class ExplodingHub:
        def publish(self, event):
            raise RuntimeError("dashboard on fire")

    registry = JobRegistry(hub=ExplodingHub())

    async def body(job):
        return {"ok": True}

    job = registry.submit(JobKind.RUN, "run", body)
    await job.task
    assert job.state is JobState.COMPLETED


# --- evals over HTTP -------------------------------------------------------


def test_an_eval_can_be_started_from_the_service(client):
    response = client.post(
        "/api/evals",
        json={"arms": "custom", "repeats": 1, "profile": "smoke"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "eval"
    assert body["state"] in {"queued", "running"}


def test_an_eval_reports_both_arms_when_it_finishes(client):
    job_id = client.post(
        "/api/evals", json={"arms": "custom", "repeats": 1, "profile": "smoke"}
    ).json()["job_id"]

    body = _await_job(client, job_id, timeout=60)
    assert body["state"] == "completed", body.get("error")
    arms = body["report"]["arms"]["custom"]
    assert "treatment" in arms and "control" in arms
    assert "comparison" in arms


def test_an_eval_refuses_a_ceiling_above_the_deployment_maximum(client):
    """A control the controlled thing can widen is not a control."""
    response = client.post(
        "/api/evals", json={"ceiling_usd": CEILING_MAX + 1}
    )
    assert response.status_code == 400


def test_an_eval_refuses_an_unknown_profile(client):
    response = client.post("/api/evals", json={"profile": "enormous"})
    assert response.status_code in (400, 422)


def test_eval_repeats_are_bounded(client):
    """An unbounded repeat count is a day of provider quota in one request."""
    assert client.post("/api/evals", json={"repeats": 500}).status_code == 422


# --- sessions over HTTP ----------------------------------------------------


def test_a_session_can_be_started_from_the_service(client):
    response = client.post(
        "/api/sessions", json={"tasks": 2, "profile": "smoke"}
    )
    assert response.status_code == 202
    assert response.json()["total"] == 2


def test_a_session_reports_its_arm(client):
    job_id = client.post(
        "/api/sessions",
        json={"tasks": 2, "profile": "smoke", "use_skills": False},
    ).json()["job_id"]

    body = _await_job(client, job_id, timeout=60)
    assert body["state"] == "completed", body.get("error")
    assert body["report"]["skills_enabled"] is False
    assert "swarmd eval" in body["report"]["claim"]


def test_a_session_task_count_is_bounded(client):
    assert client.post("/api/sessions", json={"tasks": 10_000}).status_code == 422


# --- jobs listing ----------------------------------------------------------


def test_jobs_can_be_listed_and_filtered(client):
    client.post("/api/evals", json={"arms": "custom", "repeats": 1})
    client.post("/api/sessions", json={"tasks": 1})

    assert len(client.get("/api/jobs").json()["jobs"]) >= 2
    evals = client.get("/api/jobs?kind=eval").json()["jobs"]
    assert all(j["kind"] == "eval" for j in evals)


def test_a_job_can_be_cancelled_from_the_service(client):
    job_id = client.post("/api/sessions", json={"tasks": 50}).json()["job_id"]
    assert client.delete(f"/api/jobs/{job_id}").json()["state"] == "cancelled"


def test_an_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


# --- providers -------------------------------------------------------------


def test_provider_status_is_available_from_the_service(client):
    body = client.get("/api/providers").json()
    assert body["available"] is True
    assert body["providers"]


def test_provider_status_never_returns_credentials(client):
    """An endpoint that echoed a key would put it in a browser and a proxy log."""
    text = client.get("/api/providers").text.lower()
    for forbidden in ("api_key", "secret", "bearer", "sk-"):
        assert forbidden not in text


def test_probing_reports_what_providers_actually_serve(client):
    """Published limits disagree across sources, so this replaces docs with
    observation (ADR-008)."""
    body = client.post("/api/providers/probe").json()
    assert "live" in body and "total" in body


def test_provider_endpoints_report_503_when_there_is_no_pool(tmp_path):
    def no_providers():
        raise RuntimeError("no usable providers")

    app = create_app(provider_factory=no_providers)
    with TestClient(app) as c:
        assert c.get("/api/providers").status_code == 503
        assert c.post("/api/providers/probe").status_code == 503


# --- harness configuration -------------------------------------------------


def test_the_config_shows_what_cannot_be_changed_and_why(client):
    """So an operator sees the boundary rather than discovering it from a 400."""
    body = client.get("/api/config").json()
    assert "adjustable" in body and "fixed" in body
    assert body["fixed"]["ceiling_max_usd"] == CEILING_MAX
    assert any("comparability" in note for note in body["fixed"]["notes"])
    assert any("credentials" in note for note in body["fixed"]["notes"])


def test_adjustable_knobs_can_be_changed(client):
    response = client.patch(
        "/api/config", json={"chaos_kill_rate": 0.4, "sandbox_timeout_s": 15}
    )
    assert response.status_code == 200
    assert response.json()["changed"]
    assert client.get("/api/config").json()["adjustable"]["chaos_kill_rate"] == 0.4


def test_the_ceiling_cannot_be_raised_past_the_deployment_maximum(client):
    response = client.patch(
        "/api/config", json={"default_ceiling_usd": CEILING_MAX + 5}
    )
    assert response.status_code == 400
    assert "maximum" in response.json()["detail"]


def test_an_unknown_profile_is_refused(client):
    assert client.patch(
        "/api/config", json={"default_profile": "enormous"}
    ).status_code == 400


def test_structural_thresholds_are_not_exposed_as_knobs():
    """Changing them mid-flight makes runs on either side incomparable."""
    fields = set(ConfigPatch.model_fields)
    for forbidden in (
        "min_agreement", "max_repairs", "success_reward", "clone_threshold",
        "min_distinct_ratio", "proposers",
    ):
        assert forbidden not in fields


def test_config_changes_do_not_outlive_the_process():
    """A knob turned in a browser at 2am should not silently survive a restart."""
    config = HarnessConfig()
    config.apply(ConfigPatch(chaos_kill_rate=0.9))
    assert config.chaos_kill_rate == 0.9
    assert HarnessConfig().chaos_kill_rate == 0.2


def test_applying_an_unchanged_value_reports_no_change():
    config = HarnessConfig()
    assert config.apply(ConfigPatch(chaos_kill_rate=config.chaos_kill_rate)) == []


def test_the_change_list_records_what_moved(client):
    """The postmortem needs to know what was turned during the incident."""
    changed = client.patch(
        "/api/config", json={"sandbox_memory_mb": 256}
    ).json()["changed"]
    assert any("sandbox_memory_mb" in entry for entry in changed)


def test_mutating_config_requires_the_operator_token(tmp_path):
    app = create_app(provider_factory=FakeProvider, api_token="secret-token")
    with TestClient(app) as c:
        assert c.patch("/api/config", json={"chaos_kill_rate": 0.1}).status_code == 401
        assert c.get("/api/config").status_code == 200   # reads stay open


def test_starting_an_eval_requires_the_operator_token(tmp_path):
    """It spends provider quota, so it is gated like every mutating endpoint."""
    app = create_app(provider_factory=FakeProvider, api_token="secret-token")
    with TestClient(app) as c:
        assert c.post("/api/evals", json={"repeats": 1}).status_code == 401


def test_an_eval_refuses_to_run_when_both_arms_would_be_identical(
    client_without_skills,
):
    """The failure this endpoint could not previously express.

    `SwarmRun` sets `self.skills = skills if use_skills else None`, so with no
    library BOTH arms get None and the ablation compares a configuration
    against itself. It reports "no measured improvement" -- the same words the
    real experiment produces -- after spending a sweep's worth of provider
    quota. The CLI has passed a library since this was found; this path never
    did, so every eval started from the dashboard was a null-result generator
    that read as a measurement.
    """
    response = client_without_skills.post(
        "/api/evals", json={"arms": "custom", "repeats": 1, "profile": "smoke"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "identical" in detail
    assert "--skills" in detail


def test_an_eval_refuses_when_the_library_has_nothing_approved(tmp_path):
    """A library file that exists but holds only unapproved proposals is the
    same experiment as no library: nothing is retrievable."""
    from swarmd.swarm.skills import SkillLibrary

    path = tmp_path / "empty.json"
    SkillLibrary(str(path)).propose(
        name="unapproved", task_pattern="anything", instruction="not gated yet"
    )
    app = create_app(provider_factory=FakeProvider, skills_path=str(path))
    with TestClient(app) as client:
        response = client.post("/api/evals", json={"repeats": 1})
    assert response.status_code == 422
    assert "no approved skills" in response.json()["detail"]


def test_the_eval_gives_the_treatment_arm_the_library_it_refuses_to_run_without(
    client, skills_path
):
    """The refusal above is only worth having if the arm it protects actually
    receives the library."""
    job_id = client.post(
        "/api/evals", json={"arms": "custom", "repeats": 1, "profile": "smoke"}
    ).json()["job_id"]
    body = _await_job(client, job_id, timeout=60)
    assert body["state"] == "completed", body.get("error")
