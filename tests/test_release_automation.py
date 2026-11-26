"""Contract tests for Dependabot auto-merge, daily patch release, and docker-publish workflows."""

from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load_workflow(name: str) -> dict:
    # PyYAML parses unquoted `on:` as boolean True unless normalized.
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    if True in data:
        data["on"] = data.pop(True)
    return data


def test_dependabot_automerge_workflow_contract():
    wf = _load_workflow("dependabot-automerge.yml")
    triggers = wf["on"]
    assert "pull_request_target" in triggers
    assert triggers["pull_request_target"]["types"] == ["opened", "reopened", "synchronize"]
    assert triggers["push"]["branches"] == ["master"]
    assert "workflow_dispatch" in triggers

    automerge = wf["jobs"]["automerge"]
    assert "github.actor == 'dependabot[bot]'" in automerge["if"]
    assert automerge["permissions"]["pull-requests"] == "write"
    assert automerge["permissions"]["contents"] == "write"

    step_runs = "\n".join(step.get("run", "") for step in automerge["steps"])
    step_ifs = "\n".join(step.get("if", "") for step in automerge["steps"])
    step_envs = str([step.get("env", {}) for step in automerge["steps"]])
    assert any(step.get("uses", "").startswith("dependabot/fetch-metadata@v3") for step in automerge["steps"])
    assert "version-update:semver-major" in step_ifs
    assert "gh pr merge --auto --squash" in step_runs
    assert "DEPENDABOT_REBASE_TOKEN" in step_envs
    assert "actions/checkout" not in "\n".join(step.get("uses", "") for step in automerge["steps"])

    nudge = wf["jobs"]["nudge-behind-dependabot"]
    nudge_runs = "\n".join(step.get("run", "") for step in nudge["steps"])
    nudge_envs = str([step.get("env", {}) for step in nudge["steps"]])
    assert "@dependabot rebase" in nudge_runs
    assert "mergeStateStatus" in nudge_runs
    assert "DEPENDABOT_REBASE_TOKEN" in nudge_envs


def test_daily_dependabot_release_workflow_contract():
    wf = _load_workflow("daily-dependabot-release.yml")
    triggers = wf["on"]
    assert triggers["schedule"] == [{"cron": "0 4 * * *"}]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"dry_run", "force"}

    plan = wf["jobs"]["plan-release"]
    assert plan["permissions"]["contents"] == "read"
    plan_runs = "\n".join(step.get("run", "") for step in plan["steps"])
    assert "dependabot" in plan_runs
    assert "BASH_REMATCH[3] + 1" in plan_runs

    verify = wf["jobs"]["verify-head"]
    assert verify["needs"] == "plan-release"
    assert verify["uses"] == "./.github/workflows/test.yml"
    assert verify["with"]["ref"] == "${{ needs.plan-release.outputs.head_sha }}"

    create = wf["jobs"]["create-release"]
    assert create["needs"] == ["plan-release", "verify-head"]
    assert create["permissions"]["contents"] == "write"
    create_runs = "\n".join(step.get("run", "") for step in create["steps"])
    assert "gh release create" in create_runs

    pub = wf["jobs"]["publish-release-artifacts"]
    assert pub["needs"] == ["plan-release", "create-release"]
    assert pub["if"] == "needs.create-release.outputs.released == 'true'"
    assert pub["uses"] == "./.github/workflows/docker-publish.yml"
    assert pub["with"]["tag"] == "${{ needs.plan-release.outputs.tag }}"
    assert pub["secrets"] == "inherit"


def test_test_workflow_supports_workflow_call():
    wf = _load_workflow("test.yml")
    triggers = wf["on"]
    assert "workflow_call" in triggers
    assert "ref" in triggers["workflow_call"]["inputs"]
    steps = wf["jobs"]["test"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ inputs.ref }}"


def test_docker_publish_supports_workflow_call():
    wf = _load_workflow("docker-publish.yml")
    triggers = wf["on"]
    assert "release" in triggers
    assert "push" in triggers
    assert "workflow_call" in triggers
    assert triggers["workflow_call"]["inputs"]["tag"]["required"] is True

    steps = wf["jobs"]["build-and-push"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ inputs.tag || github.ref }}"
    pkg = next(s for s in steps if s.get("name", "").startswith("Package deployment bundle"))
    assert "github.event_name == 'workflow_call'" in pkg["if"]
    assert pkg["env"]["REF_NAME"] == "${{ steps.rel.outputs.tag }}"


def test_start_stack_and_compose_manifest_consistency():
    start_stack = (ROOT / "start-stack.sh").read_text()
    assert 'manifests = [os.path.join(workdir, "pod.yaml")]' not in start_stack
    assert 'manifest = os.path.join(workdir, "pod.yaml")' in start_stack
    assert "docker-compose.yml" not in (ROOT / "scripts" / "package_release.sh").read_text()
    assert "DEPRECATED" in (ROOT / "docker-compose.yml").read_text()

    pod_images = [
        m.group(1)
        for m in re.finditer(r"image:\s*([^\s]+)", (ROOT / "pod.yaml").read_text())
        if "ROUTER_IMAGE_PLACEHOLDER" not in m.group(1)
    ]
    for prefix in (
        "valkey/valkey:",
        "ghcr.io/berriai/litellm:",
        "pgvector/pgvector:",
        "clickhouse/clickhouse-server:",
        "langfuse/langfuse:",
        "langfuse/langfuse-worker:",
        "minio/minio:",
    ):
        assert any(img.startswith(prefix) for img in pod_images), f"missing {prefix} in pod.yaml"
