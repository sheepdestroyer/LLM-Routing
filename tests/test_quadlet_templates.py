"""Static deployment-contract tests for the systemd Quadlet templates."""
from pathlib import Path
import os
import re
import subprocess
import tempfile


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


def test_containers_source_the_merged_effective_environment():
    script = (ROOT / "start-stack.sh").read_text()
    for template in (QUADLETS / "llm-routing-router.container", QUADLETS / "llm-routing-litellm.container"):
        assert "EFFECTIVE_ENV_FILE_PLACEHOLDER:/config/.env" in template.read_text()
    assert 'EFFECTIVE_ENV_FILE="${DATA_ROOT}/effective.env"' in script
    assert '"EFFECTIVE_ENV_FILE_PLACEHOLDER": os.environ["EFFECTIVE_ENV_FILE"]' in script
    assert "shlex.quote(os.environ[key])" in script
    assert "APPLICATION_ENV = {" in script
    assert "for key in sorted(APPLICATION_ENV):" in script


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
    assert 'identifier_suffixes = (' in script
    assert 'identifier_prefix.sub(namespace + "-", value)' in script
    assert 'rendered_name = os.path.basename(tpl).replace("llm-routing", namespace)' in script


def test_namespace_rendering_preserves_non_identifiers():
    script = (ROOT / "start-stack.sh").read_text()
    embedded = script.split("python3 - \"$src_dir\" \"$QUADLET_DIR\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    env = os.environ.copy()
    values = {
        "POSTGRES_PASSWORD": "pg", "WORKDIR": str(ROOT), "HOME": str(ROOT),
        "LITELLM_MASTER_KEY": "master", "NEXTAUTH_SECRET": "next", "SALT": "salt",
        "ENCRYPTION_KEY": "encrypt", "OLLAMA_API_KEY": "ollama", "OPENROUTER_API_KEY": "openrouter",
        "LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": "secret", "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": "minio-pass", "LANGFUSE_INIT_USER_PASSWORD": "lf-pass",
        "REDIS_AUTH": "redis", "CLICKHOUSE_PASSWORD": "click", "PROXY_BASE_URL_DERIVED": "https://proxy",
        "NEXTAUTH_URL_DERIVED": "https://next", "PUBLIC_BASE_URL": "https://host/llm-routing",
        "ROUTING_DOMAIN": "vendeuvre.lan", "LLAMA_CLASSIFIER_URL": "http://127.0.0.1:8083/v1",
        "LLAMA_SERVER_URL": "http://127.0.0.1:8083", "POD_NAME": "dev-router-pod",
        "DATA_ROOT": str(ROOT / "data"), "EFFECTIVE_ENV_FILE": str(ROOT / "data" / "effective.env"),
        "ROUTER_IMAGE": "registry/llm-routing-router:latest",
        "ROUTER_PORT": "5010", "LITELLM_PORT": "4010", "LANGFUSE_WEB_PORT": "3011",
        "LANGFUSE_WORKER_PORT": "3030", "POSTGRES_PORT": "5442", "VALKEY_CACHE_PORT": "6389",
        "VALKEY_LF_PORT": "6390", "CLICKHOUSE_HTTP_PORT": "8123", "CLICKHOUSE_TCP_PORT": "9003",
        "MINIO_S3_PORT": "9002", "MINIO_CONSOLE_PORT": "9001", "QUADLET_NAMESPACE": "llm-routing-dev",
    }
    env.update(values)
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "src", Path(tmp) / "out"
        src.mkdir()
        out.mkdir()
        (src / "llm-routing.pod").write_text("[Pod]\nPodName=llm-routing.pod\n")
        (src / "llm-routing-router.container").write_text(
            "[Unit]\nAfter=llm-routing-litellm.service\n[Container]\n"
            "Image=registry/llm-routing-router:latest\n"
            "Environment=PUBLIC_BASE_URL=https://host/llm-routing-router\nPod=llm-routing.pod\n"
        )
        subprocess.run(["python3", "-c", embedded, str(src), str(out)], env=env, check=True, capture_output=True, text=True)
        rendered = (out / "llm-routing-dev-router.container").read_text()
        assert "After=llm-routing-dev-litellm.service" in rendered
        assert "Image=registry/llm-routing-router:latest" in rendered
        assert "PUBLIC_BASE_URL=https://host/llm-routing-router" in rendered
        assert "Pod=llm-routing-dev.pod" in rendered


def test_namespace_is_validated_and_ownership_preserves_exact_unit():
    script = (ROOT / "start-stack.sh").read_text()
    assert '[[ ! "$QUADLET_NAMESPACE" =~ ^[a-z0-9][a-z0-9-]*$ ]]' in script
    assert "printf 'quadlet:%s\\n' \"$infra_unit\"" in script
    assert 'owner_unit="${STACK_OWNERSHIP#quadlet:}"' in script
    assert 'failed to restart ${owner_unit}' in script
    assert 'status ${owner_unit} --no-pager' in script
    assert 'legacy_unit_owns_pod()' in script
    assert "grep -E 'podman[[:space:]]+pod[[:space:]]+create'" in script
    assert 'grep -Eq -- "--name[=[:space:]]${pod_name_pattern}' in script
    assert '&& legacy_unit_owns_pod "$LEGACY_LLM_ROUTING_POD_UNIT"' in script
    assert 'elif [[ "$infra_unit" == "$LEGACY_LLM_ROUTING_POD_UNIT" ]]; then' in script
    assert 'printf \'conflict:%s\\n\' "$infra_unit"' in script
    assert 'STACK_OWNERSHIP" == conflict:*' in script


def test_documentation_uses_environment_specific_units():
    readme = (ROOT / "README.md").read_text()
    scripts_readme = (ROOT / "scripts" / "README.md").read_text()
    assert "systemctl --user status llm-routing-prod-pod.service" in readme
    assert "llm-routing-dev-pod.service" in readme
    assert "llm-routing-pod.service" not in scripts_readme
    assert "llm-routing-{dev,prod}" not in readme


def test_data_root_is_worktree_scoped_by_default():
    script = (ROOT / "start-stack.sh").read_text()
    assert 'DATA_ROOT="${DATA_ROOT:-${WORKDIR}/data}"' in script
    dev_root = ROOT.resolve()
    prod_root = (ROOT.parent.parent / "prod" / "LLM-Routing").resolve()
    assert dev_root != prod_root
    assert dev_root / "data" != prod_root / "data"
