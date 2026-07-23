"""Static deployment-contract tests for the systemd Quadlet templates."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
QUADLETS = ROOT / "quadlets"


def test_quadlet_inventory_and_pod_membership():
    containers = sorted(QUADLETS.glob("*.container"))
    assert len(containers) == 9
    assert (QUADLETS / "llm-routing.pod").is_file()
    for container in containers:
        text = container.read_text()
        assert "Pod=llm-routing.pod" in text, container.name
        assert "ContainerName=POD_NAME_PLACEHOLDER-" in text, container.name


def test_liveness_healthchecks_restart_failed_containers():
    for container in sorted(QUADLETS.glob("*.container")):
        text = container.read_text()
        assert "HealthCmd=" in text, container.name
        # Podman kills an unhealthy container; systemd Restart=always then replaces it.
        assert "HealthOnFailure=kill" in text, container.name
        assert "Restart=always" in text, container.name


def test_quadlet_templates_remain_env_rendered_and_secret_free():
    for template in [*QUADLETS.glob("*.container"), QUADLETS / "llm-routing.pod"]:
        text = template.read_text()
        assert "_PLACEHOLDER" in text, template.name
        assert not re.search(r"sk-(?:or|lf|lit)-[A-Za-z0-9_-]{12,}", text), template.name


def test_upgrade_syncs_quadlets_before_quadlet_start_stack():
    script = (ROOT / "scripts" / "upgrade-prod.sh").read_text()
    assert "quadlets/" in script
    assert 'rsync -a --delete "$TEMP_DIR/quadlets/" "$PROD_DIR/quadlets/"' in script
    assert "for f in pod.yaml start-stack.sh quadlets/" in script


def test_rendered_quadlets_are_owner_only():
    script = (ROOT / "start-stack.sh").read_text()
    assert 'chmod 700 "$QUADLET_DIR"' in script
    assert "os.chmod(staged_path, 0o600)" in script
    assert 'LLM_ROUTING_POD_UNIT="${QUADLET_NAMESPACE}-pod.service"' in script
    assert 'QUADLET_DIR="${HOME}/.config/containers/systemd/${QUADLET_NAMESPACE}"' in script
    assert "systemctl --user daemon-reload" in script
    assert "stack_ownership()" in script
    assert "PODMAN_SYSTEMD_UNIT" in script
    assert "require_user_systemd()" in script


def test_quadlet_renderer_quotes_environment_values_for_systemd():
    script = (ROOT / "start-stack.sh").read_text()
    assert 'text = re.sub(r"(?m)^Environment=(.*)$", quote_environment, text)' in script
    assert "def quote_environment(match):" in script
    assert "return f'Environment=\"{value}\"'" in script


def test_quadlet_renderer_stages_before_replacing_live_units():
    script = (ROOT / "start-stack.sh").read_text()
    assert "tempfile.mkdtemp(prefix=\".llm-routing-render-\"" in script
    assert "os.replace(os.path.join(staging_dir, name), os.path.join(out_dir, name))" in script
    assert "shutil.rmtree(staging_dir, ignore_errors=True)" in script


def test_quadlet_systemd_failures_are_reported():
    script = (ROOT / "start-stack.sh").read_text()
    assert 'if ! systemctl --user restart "$LLM_ROUTING_POD_UNIT"; then' in script
    assert 'if ! systemctl --user start "$LLM_ROUTING_POD_UNIT"; then' in script


def test_router_quadlet_reasserts_overlayed_llama_urls_after_env_source():
    template = (QUADLETS / "llm-routing-router.container").read_text()
    assert "LLAMA_CLASSIFIER_URL=LLAMA_CLASSIFIER_URL_PLACEHOLDER" in template
    assert "LLAMA_SERVER_URL=LLAMA_SERVER_URL_PLACEHOLDER" in template
    assert template.index("LLAMA_SERVER_URL=LLAMA_SERVER_URL_PLACEHOLDER") < template.index("exec uvicorn")
    script = (ROOT / "start-stack.sh").read_text()
    assert '"LLAMA_CLASSIFIER_URL_PLACEHOLDER": os.environ["LLAMA_CLASSIFIER_URL"]' in script
    assert '"LLAMA_SERVER_URL_PLACEHOLDER": os.environ["LLAMA_SERVER_URL"]' in script


def test_quadlet_deployment_enforces_prerequisite_and_restart_failures():
    script = (ROOT / "start-stack.sh").read_text()
    assert "render_pod_yaml()" not in script
    assert "require_user_systemd || exit 1" in script
    assert "failed to derive external service URLs for Quadlet rendering" in script
    assert 'if ! podman pod restart "${POD_NAME}"; then' in script


def test_quadlet_namespace_is_environment_specific():
    dev_env = (ROOT / ".env.dev").read_text()
    prod_namespace = "llm-routing-prod"
    assert 'QUADLET_NAMESPACE="llm-routing-dev"' in dev_env
    assert 'QUADLET_NAMESPACE="${QUADLET_NAMESPACE:-llm-routing-prod}"' in (ROOT / "start-stack.sh").read_text()
    assert prod_namespace in (ROOT / "start-stack.sh").read_text()
    script = (ROOT / "start-stack.sh").read_text()
    assert 'def namespace_identifier(match):' in script
    assert 'namespace + "-"' in script
    assert 'rendered_name = os.path.basename(tpl).replace("llm-routing", namespace)' in script


def test_namespace_is_validated_and_ownership_preserves_exact_unit():
    script = (ROOT / "start-stack.sh").read_text()
    assert 'QUADLET_NAMESPACE" =~ ^[a-z0-9][a-z0-9-]*$' in script
    assert "printf 'quadlet:%s\\n' \"$infra_unit\"" in script
    assert 'owner_unit="${STACK_OWNERSHIP#quadlet:}"' in script
