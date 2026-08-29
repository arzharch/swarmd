"""Guards on what may reach a deployed environment.

These are cheap greps dressed as tests, and that is the point: each one encodes
a mistake that is easy to make, invisible in review, and expensive in
production. A comment saying "don't do X" is not a control; a failing build is.
"""

from __future__ import annotations

import ipaddress
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
TRUTHY = {"1", "true", "yes", "on", "True", "TRUE"}


def _all_deploy_docs():
    for path in sorted(DEPLOY.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                yield path, doc


def _walk(node, key):
    """Yield every value stored under `key`, at any depth."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key)


# --- simulated data must never be deployed ---------------------------------


def test_no_manifest_enables_the_simulated_provider():
    """Synthetic output in a deployed environment is ADR-006's whole concern.

    The taint on ledger rows means simulated results cannot be *reported* as
    real, but a deployed environment silently serving synthetic responses is
    still a failure -- it looks alive and does nothing.
    """
    offenders = []
    for path, doc in _all_deploy_docs():
        for value in _walk(doc, "SWARMD_SIMULATED_PROVIDER"):
            if str(value).strip() in TRUTHY:
                offenders.append(f"{path.relative_to(REPO)}: {value!r}")
        # kustomize literals arrive as "KEY=value" strings, not mappings.
        for literals in _walk(doc, "literals"):
            for literal in literals or []:
                key, _, value = str(literal).partition("=")
                if key == "SWARMD_SIMULATED_PROVIDER" and value.strip() in TRUTHY:
                    offenders.append(f"{path.relative_to(REPO)}: {literal!r}")
    assert offenders == [], f"simulated provider enabled in deploy manifests: {offenders}"


def test_no_manifest_hardcodes_a_provider_key():
    """A key committed to git is in the history forever; rotation cannot reach it."""
    key_names = (
        "GROQ_API_KEY", "GOOGLE_API_KEY", "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
    )
    offenders = []
    for path, doc in _all_deploy_docs():
        for name in key_names:
            for value in _walk(doc, name):
                if str(value).strip():
                    offenders.append(f"{path.relative_to(REPO)}: {name}")
    assert offenders == [], f"non-empty provider keys in manifests: {offenders}"


# --- production hardening --------------------------------------------------


def _prod_docs():
    path = DEPLOY / "k8s" / "overlays" / "prod" / "kustomization.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_prod_pins_images_by_digest_not_tag():
    """A tag is a mutable pointer: it can be repointed after it was tested."""
    images = _prod_docs().get("images", [])
    assert images, "prod overlay must pin images"
    for image in images:
        assert "digest" in image, f"{image.get('name')} pinned by tag, not digest"
        assert image["digest"].startswith("sha256:")


def test_prod_replaces_the_placeholder_secret():
    """An empty mounted Secret presents as a provider outage, not a config error."""
    patches = _prod_docs().get("patches", [])
    deletes_secret = any(
        p.get("target", {}).get("kind") == "Secret"
        and "$patch: delete" in str(p.get("patch", ""))
        for p in patches
    )
    assert deletes_secret, "prod must delete the base placeholder Secret"


def test_prod_does_not_enable_data_training_by_default():
    """That tier's quota is paid for in data. It should never be a default."""
    for literals in _walk(_prod_docs(), "literals"):
        for literal in literals or []:
            key, _, value = str(literal).partition("=")
            if key == "SWARMD_ALLOW_DATA_TRAINING":
                assert value.strip() not in TRUTHY


def test_dev_never_enables_paid_providers():
    """A runaway loop against a paid provider in an unwatched env is a surprise bill."""
    path = DEPLOY / "k8s" / "overlays" / "dev" / "kustomization.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    found = False
    for literals in _walk(doc, "literals"):
        for literal in literals or []:
            key, _, value = str(literal).partition("=")
            if key == "SWARMD_ALLOW_PAID":
                found = True
                assert value.strip() not in TRUTHY
    assert found, "dev overlay must state SWARMD_ALLOW_PAID explicitly"


# --- pod security ----------------------------------------------------------


def _workload_specs():
    for path, doc in _all_deploy_docs():
        kind = doc.get("kind")
        if kind in {"Deployment", "Job", "StatefulSet", "DaemonSet"}:
            yield path, doc["metadata"]["name"], doc["spec"]["template"]["spec"]


def test_every_workload_runs_as_non_root():
    for path, name, spec in _workload_specs():
        ctx = spec.get("securityContext", {})
        assert ctx.get("runAsNonRoot") is True, f"{name} in {path.name} may run as root"


def test_every_container_drops_all_capabilities():
    for path, name, spec in _workload_specs():
        for container in spec.get("containers", []):
            caps = container.get("securityContext", {}).get("capabilities", {})
            assert caps.get("drop") == ["ALL"], (
                f"{name}/{container['name']} does not drop ALL capabilities"
            )


def test_every_container_forbids_privilege_escalation():
    for path, name, spec in _workload_specs():
        for container in spec.get("containers", []):
            ctx = container.get("securityContext", {})
            assert ctx.get("allowPrivilegeEscalation") is False, (
                f"{name}/{container['name']} permits privilege escalation"
            )


def test_every_container_has_a_read_only_root_filesystem():
    for path, name, spec in _workload_specs():
        for container in spec.get("containers", []):
            ctx = container.get("securityContext", {})
            assert ctx.get("readOnlyRootFilesystem") is True, (
                f"{name}/{container['name']} has a writable root filesystem"
            )


def test_every_container_requests_resources():
    """No request means best-effort, which is evicted first under node pressure.

    For a long run that is work lost for no reason.
    """
    for path, name, spec in _workload_specs():
        for container in spec.get("containers", []):
            requests = container.get("resources", {}).get("requests", {})
            assert requests.get("memory"), f"{name}/{container['name']} has no memory request"
            assert requests.get("cpu"), f"{name}/{container['name']} has no cpu request"


def test_no_egress_rule_can_reach_the_cloud_metadata_endpoint():
    """169.254.169.254 is the standard path from code execution to cloud creds.

    Asserted by checking every permitted CIDR rather than looking for an
    `except` entry: the policy now ALLOWLISTS provider ranges instead of
    excepting the metadata address out of 0.0.0.0/0, which is strictly
    stronger, and a test that only looked for the exception would have gone
    green while checking nothing.
    """
    metadata = ipaddress.ip_address("169.254.169.254")
    offenders = []
    for path, doc in _all_deploy_docs():
        if doc.get("kind") != "NetworkPolicy":
            continue
        for rule in doc["spec"].get("egress", []):
            for target in rule.get("to", []):
                block = target.get("ipBlock")
                if not block:
                    continue
                network = ipaddress.ip_network(block["cidr"])
                if metadata not in network:
                    continue
                excepted = any(
                    metadata in ipaddress.ip_network(e)
                    for e in block.get("except", [])
                )
                if not excepted:
                    offenders.append(
                        f"{path.relative_to(REPO)}: {block['cidr']} reaches metadata"
                    )
    assert offenders == [], offenders


# --- the image is the deployment artifact ----------------------------------
#
# Every check below corresponds to a defect that was live in `master` and was
# found by building the image and running the manifests against a real cluster.
# None of them could be found by reading: the image had never been built
# successfully, so nothing downstream of the build had ever executed.


DOCKERFILE = REPO / "deploy" / "Dockerfile"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_the_dockerfile_copies_every_file_the_build_backend_reads():
    """`uv sync` fails on a missing readme, and the error names a path, not a cause.

    pyproject.toml declares `readme = "README.md"`, so the build backend opens
    it while packaging. The Dockerfile copied src/ and examples/ and not the
    readme, so the build died with "failed to open file /app/README.md" -- and
    the image had therefore never been built at all.
    """
    import tomllib

    project = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    readme = project.get("readme")
    if not readme:
        pytest.skip("no readme declared")

    dockerfile = _dockerfile()
    assert f"COPY {readme}" in dockerfile, (
        f"pyproject declares readme={readme!r}; the Dockerfile must COPY it or "
        f"the build backend cannot package the project"
    )


def test_the_image_installs_the_extras_the_manifest_depends_on():
    """The container started, printed 'serve needs the serve extra', and exited.

    The manifest runs `swarmd serve` against Postgres, so `serve` and
    `postgres` are part of the deployment contract however optional their name
    makes them sound.
    """
    dockerfile = _dockerfile()
    for extra in ("serve", "postgres"):
        assert f"--extra {extra}" in dockerfile, (
            f"the deployment needs the {extra!r} extra; without it the "
            f"container exits at startup"
        )


def test_the_image_default_command_binds_a_reachable_address():
    """Loopback inside a container is reachable from nothing outside it.

    `docker run -p 8000:8000` against the old default connected to a server
    that was up and listening where the published port could not reach it. The
    CLI's 127.0.0.1 default is right for a laptop and wrong here.
    """
    dockerfile = _dockerfile()
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD")]
    assert cmd, "no CMD in the Dockerfile"
    assert "0.0.0.0" in cmd[-1], (
        f"the image default command must bind 0.0.0.0, not loopback: {cmd[-1]}"
    )


def test_anything_bound_off_host_is_given_an_operator_token():
    """The deployment CrashLoopBackOffed on every apply until this held.

    The app refuses to bind 0.0.0.0 with no SWARMD_API_TOKEN (ADR-013) -- which
    is correct, since the token is the only thing between the run API and
    anyone who can reach the port. The base Secret ships an empty placeholder,
    so the guard fired on every dev deploy and the pods never started.

    Checked per overlay: prod gets a real token from an ExternalSecret, dev
    patches in an obviously-fake local one, and neither may regress to empty.
    """
    import subprocess

    for overlay in ("dev", "prod"):
        rendered = subprocess.run(
            ["kubectl", "kustomize", f"deploy/k8s/overlays/{overlay}"],
            capture_output=True, text=True, cwd=REPO, check=False,
        )
        if rendered.returncode != 0:
            pytest.skip(f"kubectl unavailable: {rendered.stderr[:80]}")

        docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
        binds_off_host = any(
            "0.0.0.0" in str(value)
            for doc in docs
            for value in _walk(doc, "args")
        )
        if not binds_off_host:
            continue

        # Either a literal token with a value, or an external source for one.
        literal = [
            v
            for doc in docs
            for sd in _walk(doc, "stringData")
            for k, v in (sd or {}).items()
            if k == "SWARMD_API_TOKEN"
        ]
        external = [
            k
            for doc in docs
            for k in _walk(doc, "secretKey")
            if k == "SWARMD_API_TOKEN"
        ]
        assert any(str(v).strip() for v in literal) or external, (
            f"{overlay} binds 0.0.0.0 but supplies no SWARMD_API_TOKEN; the "
            f"control plane will refuse to start and CrashLoopBackOff"
        )


# --- alert/runbook coupling ------------------------------------------------


def test_every_alert_has_a_runbook_entry():
    """An alert nobody knows how to act on trains people to ignore pages."""
    alerts = yaml.safe_load(
        (REPO / "observability" / "alerts.yml").read_text(encoding="utf-8")
    )
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text(encoding="utf-8").lower()

    # Match against the runbook's actual HEADINGS, not against its whole text.
    # Substring-matching the document was too weak to be useful: two alerts
    # linked #costceilingapproaching and #ceilingabort while the runbook had a
    # single combined "## CostCeilingApproaching / CeilingAbort" heading, so
    # both links resolved to nothing and this test passed anyway. A guard that
    # accepts a dead link is worse than no guard, because it certifies it.
    headings = {
        re.sub(r"[^a-z0-9]", "", line)
        for line in re.findall(r"^##\s+(.+)$", runbook, re.MULTILINE)
    }

    missing = []
    for group in alerts["groups"]:
        for rule in group["rules"]:
            anchor = rule["labels"].get("runbook", "")
            assert anchor, f"{rule['alert']} declares no runbook link"
            if anchor.split("#", 1)[-1] not in headings:
                missing.append(f"{rule['alert']} -> {anchor}")
    assert missing == [], f"alerts whose runbook link resolves nowhere: {missing}"


@pytest.mark.parametrize("path", ["observability/alerts.yml", "observability/prometheus.yml"])
def test_observability_config_is_valid_yaml(path):
    yaml.safe_load((REPO / path).read_text(encoding="utf-8"))


# --- the frontend has no fixture path --------------------------------------


FRONTEND = REPO / "frontend"
FIXTURE_HINTS = (
    "mockdata", "mock_data", "fakedata", "fake_data", "sampledata",
    "sample_data", "seeddata", "seed_data", "demodata", "demo_data",
    "placeholderdata", "dummydata", "dummy_data", "fixtures",
)


def _frontend_sources():
    if not FRONTEND.exists():
        return []
    return [
        path
        for pattern in ("*.ts", "*.tsx")
        for path in FRONTEND.rglob(pattern)
        if "node_modules" not in path.parts
        and ".next" not in path.parts
        # Generated by `next build`; not authored, not reviewed, and it
        # contains a docs URL that trips the hardcoded-host check.
        and path.name != "next-env.d.ts"
    ]


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend not present")
def test_the_frontend_imports_no_sample_data():
    """ADR-006, enforced rather than promised.

    A dashboard fed by fixtures is pixel-identical to one fed by a real run.
    The page renders the websocket stream or it renders an empty state, and a
    build fails rather than shipping a third option.
    """
    offenders = []
    for path in _frontend_sources():
        text = path.read_text(encoding="utf-8").lower()
        for line in text.splitlines():
            if not (line.startswith("import ") or " from \"" in line):
                continue
            if any(hint in line.replace("-", "").replace(" ", "") for hint in FIXTURE_HINTS):
                offenders.append(f"{path.relative_to(REPO)}: {line.strip()[:80]}")
    assert offenders == [], f"frontend imports sample data: {offenders}"


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend not present")
def test_the_frontend_has_no_hardcoded_backend_host():
    """An absolute backend URL baked into the client breaks every environment
    that is not the one it was built for. The image is built once and promoted."""
    offenders = []
    allowed = {"127.0.0.1", "localhost"}  # dev proxy config only
    for path in _frontend_sources():
        if path.name == "next.config.mjs":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            has_url = "http://" in line or "https://" in line
            if has_url and not any(host in line for host in allowed):
                offenders.append(f"{path.relative_to(REPO)}: {line.strip()[:80]}")
    assert offenders == [], f"hardcoded backend hosts: {offenders}"


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend not present")
def test_the_simulated_banner_is_driven_by_ledger_data():
    """The banner must read the taint flag, not a UI setting someone can forget."""
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "cost?.simulated" in page, (
        "the SIMULATED banner must be derived from the cost report's taint flag"
    )


# --- the no-user-auth posture (ADR-013) ------------------------------------


def test_the_control_plane_has_no_publicly_routable_service():
    """The run API must not be reachable except through the allowlisted path.

    A LoadBalancer or NodePort pointing at the control plane would give it a
    route of its own, bypassing both the Ingress allowlist and the reasoning
    in ADR-013.
    """
    offenders = []
    for path, doc in _all_deploy_docs():
        if doc.get("kind") != "Service":
            continue
        if doc["spec"].get("type") in {"LoadBalancer", "NodePort"}:
            offenders.append(f"{path.relative_to(REPO)}: {doc['metadata']['name']}")
    assert offenders == [], f"publicly routable services: {offenders}"


def test_the_ingress_allowlists_source_addresses():
    """The dashboard is an operations console, not a public site."""
    found = False
    for _, doc in _all_deploy_docs():
        if doc.get("kind") != "Ingress":
            continue
        annotations = doc["metadata"].get("annotations", {})
        allowlist = annotations.get(
            "nginx.ingress.kubernetes.io/whitelist-source-range"
        )
        assert allowlist, "ingress must allowlist source addresses"
        found = True
    assert found, "no Ingress found to check"


def test_the_base_allowlist_fails_closed():
    """A forgotten overlay override must lock everyone out, not let everyone in."""
    path = DEPLOY / "k8s" / "base" / "frontend.yaml"
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "Ingress":
            allowlist = doc["metadata"]["annotations"][
                "nginx.ingress.kubernetes.io/whitelist-source-range"
            ]
            assert allowlist.startswith("127.0.0.1"), (
                "the base allowlist must be loopback so a missing override "
                f"fails closed, got {allowlist!r}"
            )


def test_the_operator_token_is_declared_but_empty_in_the_base_secret():
    path = DEPLOY / "k8s" / "base" / "rbac-and-config.yaml"
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "Secret":
            assert "SWARMD_API_TOKEN" in doc["stringData"]
            assert doc["stringData"]["SWARMD_API_TOKEN"] == ""


def test_prod_sources_the_operator_token_from_external_secrets():
    path = DEPLOY / "k8s" / "overlays" / "prod" / "external-secrets.yaml"
    text = path.read_text(encoding="utf-8")
    assert "SWARMD_API_TOKEN" in text


def test_interactive_api_docs_are_disabled_in_cluster():
    """Docs are a live client for every endpoint, served without the token."""
    path = DEPLOY / "k8s" / "base" / "rbac-and-config.yaml"
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ConfigMap":
            assert doc["data"].get("SWARMD_ENV") == "prod"


def test_structured_logging_is_configured_in_cluster():
    path = DEPLOY / "k8s" / "base" / "rbac-and-config.yaml"
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ConfigMap":
            assert doc["data"].get("SWARMD_LOG_FORMAT") == "json"


def test_egress_is_not_open_to_the_whole_internet():
    """443-to-anywhere was the loosest control shipped and the PRR's blocker.

    It is the path generated code in the sandbox would take to reach the
    internet if it escaped its subprocess.
    """
    offenders = []
    for path, doc in _all_deploy_docs():
        if doc.get("kind") != "NetworkPolicy":
            continue
        for rule in doc["spec"].get("egress", []):
            for target in rule.get("to", []):
                cidr = target.get("ipBlock", {}).get("cidr")
                if cidr == "0.0.0.0/0":
                    offenders.append(
                        f"{path.relative_to(REPO)}: {doc['metadata']['name']}"
                    )
    assert offenders == [], f"egress open to the internet: {offenders}"


def test_egress_still_permits_the_provider_ranges():
    """A policy so tight nothing can call a provider is an outage, not security."""
    https_targets = 0
    for _, doc in _all_deploy_docs():
        if doc.get("kind") != "NetworkPolicy":
            continue
        for rule in doc["spec"].get("egress", []):
            ports = {p.get("port") for p in rule.get("ports", [])}
            if 443 in ports:
                https_targets += len(rule.get("to", []))
    assert https_targets >= 3, "no egress ranges permitted for provider APIs"


# --- running without auth is only safe if something else restricts access ----


def _open_mode_declared():
    for path, doc in _all_deploy_docs():
        if doc.get("kind") != "ConfigMap":
            continue
        value = str((doc.get("data") or {}).get("SWARMD_ALLOW_OPEN", ""))
        if value in TRUTHY:
            return path
    return None


def test_open_mode_is_backed_by_a_network_policy():
    """The compensating control, made enforceable.

    User auth is out of MVP scope, so the deployed control plane binds 0.0.0.0
    with no token. What makes that defensible is NOT the missing token -- it is
    that only the dashboard pod and the ingress controller can reach port 8000.
    Delete the NetworkPolicy and the same manifests become an open run API that
    spends real provider quota, with nothing in the diff to say so.
    """
    declared = _open_mode_declared()
    if declared is None:
        pytest.skip("open mode is not declared; the token is doing the work")

    policies = [
        doc for _, doc in _all_deploy_docs() if doc.get("kind") == "NetworkPolicy"
    ]
    assert policies, (
        f"{declared} sets SWARMD_ALLOW_OPEN with no NetworkPolicy anywhere in "
        f"deploy/: the control plane would accept run submissions from any pod "
        f"in the cluster"
    )

    # A default-deny, so a new workload is restricted rather than exposed until
    # somebody remembers to restrict it.
    assert any(
        p.get("spec", {}).get("podSelector") == {}
        and set(p.get("spec", {}).get("policyTypes", [])) >= {"Ingress"}
        for p in policies
    ), "no default-deny ingress policy; allow-listing on top of nothing"

    # And something has to actually name the control plane's port.
    guarded = [
        p for p in policies
        if any(
            str(port.get("port")) == "8000"
            for rule in p.get("spec", {}).get("ingress", []) or []
            for port in rule.get("ports", []) or []
        )
    ]
    assert guarded, "no NetworkPolicy admits port 8000 from a restricted source"


def test_open_mode_is_declared_rather_than_implied():
    """`SWARMD_ALLOW_OPEN` must be set explicitly where a reviewer sees it.

    The control plane refuses to bind a public interface without it, so its
    absence is a crash rather than a silent exposure -- but its PRESENCE should
    be a line someone consciously added, not a default buried in code.
    """
    declared = _open_mode_declared()
    if declared is None:
        pytest.skip("open mode is not declared")
    text = declared.read_text(encoding="utf-8")
    assert "NetworkPolicy" in text or "NO USER AUTH" in text, (
        f"{declared} enables open mode without recording why it is safe"
    )
