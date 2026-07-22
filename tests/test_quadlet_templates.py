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
    assert "os.chmod(out_path, 0o600)" in script
    assert "systemctl --user daemon-reload" in script
