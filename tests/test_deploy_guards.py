"""Guards on what may reach a deployed environment.

These are cheap greps dressed as tests, and that is the point: each one encodes
a mistake that is easy to make, invisible in review, and expensive in
production. A comment saying "don't do X" is not a control; a failing build is.
"""

from __future__ import annotations

import pathlib

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


def test_egress_policy_blocks_the_cloud_metadata_endpoint():
    """169.254.169.254 is the standard path from code execution to cloud creds."""
    found = False
    for _, doc in _all_deploy_docs():
        if doc.get("kind") != "NetworkPolicy":
            continue
        for block in _walk(doc, "ipBlock"):
            for excluded in block.get("except", []):
                if excluded.startswith("169.254.169.254"):
                    found = True
    assert found, "no NetworkPolicy excludes the cloud metadata endpoint"


# --- alert/runbook coupling ------------------------------------------------


def test_every_alert_has_a_runbook_entry():
    """An alert nobody knows how to act on trains people to ignore pages."""
    alerts = yaml.safe_load(
        (REPO / "observability" / "alerts.yml").read_text(encoding="utf-8")
    )
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text(encoding="utf-8").lower()

    missing = []
    for group in alerts["groups"]:
        for rule in group["rules"]:
            anchor = rule["labels"].get("runbook", "")
            assert anchor, f"{rule['alert']} declares no runbook link"
            heading = anchor.split("#", 1)[-1]
            if heading not in runbook.replace(" ", "").replace("/", ""):
                missing.append(rule["alert"])
    assert missing == [], f"alerts with no runbook section: {missing}"


@pytest.mark.parametrize("path", ["observability/alerts.yml", "observability/prometheus.yml"])
def test_observability_config_is_valid_yaml(path):
    yaml.safe_load((REPO / path).read_text(encoding="utf-8"))
