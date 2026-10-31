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
        assert "[Install]\nWantedBy=default.target llm-routing-pod.service" in text, container.name


def test_quadlet_container_healthcmds_and_aligned_versions():
    minio = (QUADLETS / "llm-routing-minio.container").read_text()
    assert 'HealthCmd=["curl", "-sf", "http://127.0.0.1:MINIO_S3_PORT_PLACEHOLDER/minio/health/live"]' in minio

    clickhouse = (QUADLETS / "llm-routing-clickhouse.container").read_text()
    assert (
        'HealthCmd=["clickhouse-client", "--host", "127.0.0.1", "--port", "CLICKHOUSE_TCP_PORT_PLACEHOLDER", "--user", "clickhouse", "--password", "CLICKHOUSE_PASSWORD_PLACEHOLDER", "--query", "SELECT 1"]'
        in clickhouse
    )

    worker = (QUADLETS / "llm-routing-langfuse-worker.container").read_text()
    assert "HealthCmd=wget -q -O /dev/null http://127.0.0.1:LANGFUSE_WORKER_PORT_PLACEHOLDER/api/health" in worker

    litellm = (QUADLETS / "llm-routing-litellm.container").read_text()
    assert (
        "HealthCmd=python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:LITELLM_PORT_PLACEHOLDER/health/liveness', timeout=3)\""
        in litellm
    )
    assert "HealthTimeout=10s" in litellm
    assert "Image=LITELLM_IMAGE_PLACEHOLDER" in litellm
    assert "Label=wud.tag.include=^v[0-9]+[.][0-9]+[.][0-9]+$" in litellm
    assert "Label=wud.tag.exclude=.*(dev|nightly|rc|beta).*" in litellm

    postgres = (QUADLETS / "llm-routing-postgres.container").read_text()
    assert "Image=POSTGRES_IMAGE_PLACEHOLDER" in postgres
    assert "Label=wud.tag.exclude=.*(trixie|bookworm|bullseye).*" in postgres
    assert "Exec=postgres -c log_checkpoints=off -c log_min_messages=warning" in postgres

    assert "Label=wud.tag.include=^RELEASE[.][0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z$" in minio
    assert "Label=wud.tag.exclude=.*-cpu.*" in minio
    assert (
        'Label="wud.tag.transform=^RELEASE[.]([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})-([0-9]{2})-([0-9]{2})Z$ => $1.$2$3.$4$5$6"'
        in minio
    )

    assert "Label=wud.tag.include=^[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+-distroless$" in clickhouse
    assert (
        'Label="wud.tag.transform=^([0-9]+)[.]([0-9]+)[.]([0-9]+)[.]([0-9]+)-distroless$ => $1.$2.$3-$4"' in clickhouse
    )

    router = (QUADLETS / "llm-routing-router.container").read_text()
    assert (
        "HealthCmd=python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:ROUTER_PORT_PLACEHOLDER/metrics', timeout=3)\""
        in router
    )
    assert "HealthTimeout=10s" in router
    assert "Label=wud.tag.include=^v[0-9]+[.][0-9]+[.][0-9]+$" in router
    assert "Label=wud.tag.exclude=.*(dev|nightly|rc|beta).*" in router


def test_router_graceful_shutdown_bounds_sigterm_stop():
    # Issue #724: long-lived SSE streams kept uvicorn (PID 1) alive past podman's
    # 10s default StopTimeout, so every planned stop ended in SIGKILL and the
    # systemd unit reported exit-code failure. uvicorn must bound its graceful
    # shutdown and the Quadlet must raise StopTimeout as belt-and-braces.
    router = (QUADLETS / "llm-routing-router.container").read_text()
    assert "--timeout-graceful-shutdown 10" in router
    exec_line = next(line for line in router.splitlines() if line.startswith("Exec="))
    assert (
        exec_line.index("exec uvicorn") < exec_line.index("--timeout-graceful-shutdown 10") < exec_line.rindex('"')
    ), "graceful-shutdown timeout must be inside the quoted exec uvicorn command"
    assert "StopTimeout=30" in router

    # pod.yaml (legacy play-kube manifest kept in lockstep) gets the same flag.
    pod_yaml = (ROOT / "pod.yaml").read_text()
    assert "--timeout-graceful-shutdown 10" in pod_yaml

    # Langfuse web/worker drain longer than the 10s default and expose no
    # shutdown-timeout knob; StopTimeout= is the template-side mitigation.
    for name in ("llm-routing-langfuse-web.container", "llm-routing-langfuse-worker.container"):
        text = (QUADLETS / name).read_text()
        assert "StopTimeout=30" in text, name


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
    assert "for f in pod.yaml docker-compose.yml start-stack.sh quadlets/" in script


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
    assert 'tempfile.mkdtemp(prefix=".llm-routing-render-"' in script
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
    assert '"LLAMA_CLASSIFIER_URL_PLACEHOLDER": llama_classifier_url' in script
    assert '"LLAMA_SERVER_URL_PLACEHOLDER": llama_server_url' in script


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
    assert "def namespace_identifier(match):" in script
    assert "identifier_suffixes = (" in script
    assert 'identifier_prefix.sub(namespace + "-", value)' in script
    assert 'rendered_name = os.path.basename(tpl).replace("llm-routing", namespace)' in script


def test_namespace_rendering_preserves_non_identifiers():
    script = (ROOT / "start-stack.sh").read_text()
    embedded = script.split('python3 - "$src_dir" "$QUADLET_DIR" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
    env = os.environ.copy()
    values = {
        "POSTGRES_PASSWORD": "pg",
        "WORKDIR": str(ROOT),
        "HOME": str(ROOT),
        "LITELLM_MASTER_KEY": "master",
        "NEXTAUTH_SECRET": "next",
        "SALT": "salt",
        "ENCRYPTION_KEY": "encrypt",
        "OLLAMA_API_KEY": "ollama",
        "OPENROUTER_API_KEY": "openrouter",
        "LANGFUSE_PUBLIC_KEY": "public",
        "LANGFUSE_SECRET_KEY": "secret",
        "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": "minio-pass",
        "LANGFUSE_INIT_USER_PASSWORD": "lf-pass",
        "REDIS_AUTH": "redis",
        "CLICKHOUSE_PASSWORD": "click",
        "PROXY_BASE_URL_DERIVED": "https://proxy",
        "NEXTAUTH_URL_DERIVED": "https://next",
        "PUBLIC_BASE_URL": "https://host/llm-routing",
        "ROUTING_DOMAIN": "vendeuvre.lan",
        "LLAMA_CLASSIFIER_URL": "http://127.0.0.1:8083/v1",
        "LLAMA_SERVER_URL": "http://127.0.0.1:8083",
        "POD_NAME": "dev-router-pod",
        "DATA_ROOT": str(ROOT / "data"),
        "EFFECTIVE_ENV_FILE": str(ROOT / "data" / "effective.env"),
        "ROUTER_IMAGE": "registry/llm-routing-router:latest",
        "ROUTER_PORT": "5010",
        "LITELLM_PORT": "4010",
        "LANGFUSE_WEB_PORT": "3011",
        "LANGFUSE_WORKER_PORT": "3030",
        "POSTGRES_PORT": "5442",
        "VALKEY_CACHE_PORT": "6389",
        "VALKEY_LF_PORT": "6390",
        "CLICKHOUSE_HTTP_PORT": "8123",
        "CLICKHOUSE_TCP_PORT": "9003",
        "MINIO_S3_PORT": "9002",
        "MINIO_CONSOLE_PORT": "9001",
        "QUADLET_NAMESPACE": "llm-routing-dev",
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
        subprocess.run(
            ["python3", "-c", embedded, str(src), str(out)], env=env, check=True, capture_output=True, text=True
        )
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
    assert "failed to restart ${owner_unit}" in script
    assert "status ${owner_unit} --no-pager" in script
    assert "legacy_unit_owns_pod()" in script
    assert "grep -E 'podman[[:space:]]+pod[[:space:]]+create'" in script
    assert 'grep -Eq -- "--name[=[:space:]]${pod_name_pattern}' in script
    assert '&& legacy_unit_owns_pod "$LEGACY_LLM_ROUTING_POD_UNIT"' in script
    assert 'elif [[ "$infra_unit" == "$LEGACY_LLM_ROUTING_POD_UNIT" ]]; then' in script
    assert "printf 'conflict:%s\\n' \"$infra_unit\"" in script
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


def test_langfuse_dual_write_mode_configured():
    pod_yaml = (ROOT / "pod.yaml").read_text()
    assert "- name: LANGFUSE_MIGRATION_V4_WRITE_MODE\n      value: dual" in pod_yaml
    assert pod_yaml.count("name: LANGFUSE_MIGRATION_V4_WRITE_MODE") == 2

    web = (QUADLETS / "llm-routing-langfuse-web.container").read_text()
    assert "Environment=LANGFUSE_MIGRATION_V4_WRITE_MODE=dual" in web

    worker = (QUADLETS / "llm-routing-langfuse-worker.container").read_text()
    assert "Environment=LANGFUSE_MIGRATION_V4_WRITE_MODE=dual" in worker


def test_environment_isolation_guards_and_clean_zombie_ports():
    script = (ROOT / "start-stack.sh").read_text()
    assert "Production namespace (llm-routing-prod) cannot use a development data directory" in script
    assert "Development namespace (llm-routing-dev) cannot use a production data directory" in script
    assert "Refusing to start production stack" in script
    assert "automatically applying .env.dev overlay" in script
    assert "8080" not in script.split("cleanup_zombie_ports()")[1].split('echo "🧹')[0]


def test_postgres_checkpoint_logging_suppressed():
    pod_yaml = (ROOT / "pod.yaml").read_text()
    assert "- log_checkpoints=off" in pod_yaml
    assert "- log_min_messages=warning" in pod_yaml

    postgres = (QUADLETS / "llm-routing-postgres.container").read_text()
    assert "Exec=postgres -c log_checkpoints=off -c log_min_messages=warning" in postgres

    script = (ROOT / "start-stack.sh").read_text()
    assert "ALTER SYSTEM SET log_checkpoints = 'off'" in script
    assert "SELECT pg_reload_conf()" in script

    # Regression #723: psql -c "ALTER SYSTEM ...; SELECT pg_reload_conf()" runs both
    # statements in ONE implicit transaction block, and ALTER SYSTEM cannot run inside
    # a transaction — so the tuning silently never applied. Each statement must get its
    # own -c flag (its own transaction).
    alter_arg = "-c \"ALTER SYSTEM SET log_checkpoints = 'off'\""
    reload_arg = '-c "SELECT pg_reload_conf()"'
    assert script.count(alter_arg) == 1
    assert script.count(reload_arg) == 1
    alter_idx = script.index(alter_arg)
    reload_idx = script.index(reload_arg)
    assert alter_idx < reload_idx
    # The two statements must be separate -c arguments, never joined by a semicolon.
    assert alter_arg + ";" not in script
    assert "; SELECT pg_reload_conf()" not in script

    # Regression #723: the failure must not be swallowed by `|| true` + `2>&1`;
    # it must be guarded and surfaced as a warning.
    guard = 'if ! podman exec "${POD_NAME}-postgres-db" psql'
    assert guard in script
    block = script[script.index(guard) : script.index("fi", reload_idx)]
    assert "|| true" not in block
    assert "2>&1" not in block
    assert "Failed to apply log_checkpoints=off" in block
    # All statements are -c arguments of that single psql invocation (line continuations).
    assert block.count("-c ") == 3
    assert block.count("\\\n") >= 3
    # listen_addresses mirrored in postgresql.auto.conf before the reload (loopback only).
    listen_arg = "-c \"ALTER SYSTEM SET listen_addresses = '127.0.0.1,::1'\""
    assert script.count(listen_arg) == 1
    assert alter_idx < script.index(listen_arg) < reload_idx


def test_clickhouse_logging_level_warning():
    script = (ROOT / "start-stack.sh").read_text()
    assert "generate_clickhouse_config()" in script
    gen_block = script[script.index("generate_clickhouse_config()") : script.index("render_litellm_config()")]
    assert "<logger>" in gen_block
    assert "<level>warning</level>" in gen_block
    assert "<text_log>" in gen_block
    assert "SYSTEM RELOAD CONFIG" in script


def test_dev_stack_disabled_by_default_and_stop_command_supported():
    script = (ROOT / "start-stack.sh").read_text()
    assert "./start-stack.sh --stop | --down" in script
    assert "STOP_MODE=true" in script
    assert "if $STOP_MODE; then" in script

    # Test that dev quadlet rendering strips default.target
    embedded = script.split('python3 - "$src_dir" "$QUADLET_DIR" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
    env = os.environ.copy()
    values = {
        "POSTGRES_PASSWORD": "pg",
        "WORKDIR": str(ROOT),
        "HOME": str(ROOT),
        "LITELLM_MASTER_KEY": "master",
        "NEXTAUTH_SECRET": "next",
        "SALT": "salt",
        "ENCRYPTION_KEY": "encrypt",
        "OLLAMA_API_KEY": "ollama",
        "OPENROUTER_API_KEY": "openrouter",
        "LANGFUSE_PUBLIC_KEY": "public",
        "LANGFUSE_SECRET_KEY": "secret",
        "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": "minio-pass",
        "LANGFUSE_INIT_USER_PASSWORD": "lf-pass",
        "REDIS_AUTH": "redis",
        "CLICKHOUSE_PASSWORD": "click",
        "PROXY_BASE_URL_DERIVED": "https://proxy",
        "NEXTAUTH_URL_DERIVED": "https://next",
        "PUBLIC_BASE_URL": "https://host/llm-routing",
        "ROUTING_DOMAIN": "vendeuvre.lan",
        "LLAMA_CLASSIFIER_URL": "http://127.0.0.1:8083/v1",
        "LLAMA_SERVER_URL": "http://127.0.0.1:8083",
        "POD_NAME": "dev-router-pod",
        "DATA_ROOT": str(ROOT / "data-dev"),
        "EFFECTIVE_ENV_FILE": str(ROOT / "data-dev" / "effective.env"),
        "ROUTER_IMAGE": "registry/llm-routing-router:latest",
        "ROUTER_PORT": "5010",
        "LITELLM_PORT": "4010",
        "LANGFUSE_WEB_PORT": "3011",
        "LANGFUSE_WORKER_PORT": "3030",
        "POSTGRES_PORT": "5442",
        "VALKEY_CACHE_PORT": "6389",
        "VALKEY_LF_PORT": "6390",
        "CLICKHOUSE_HTTP_PORT": "8123",
        "CLICKHOUSE_TCP_PORT": "9003",
        "MINIO_S3_PORT": "9002",
        "MINIO_CONSOLE_PORT": "9001",
        "QUADLET_NAMESPACE": "llm-routing-dev",
    }
    env.update(values)
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "src", Path(tmp) / "out"
        src.mkdir()
        out.mkdir()
        (src / "llm-routing.pod").write_text("[Pod]\nPodName=llm-routing.pod\n\n[Install]\nWantedBy=default.target\n")
        (src / "llm-routing-router.container").write_text(
            "[Unit]\nAfter=llm-routing-litellm.service\n[Container]\n"
            "Image=registry/llm-routing-router:latest\n"
            "Environment=PUBLIC_BASE_URL=https://host/llm-routing-router\nPod=llm-routing.pod\n\n"
            "[Install]\nWantedBy=default.target llm-routing-pod.service\n"
        )
        subprocess.run(
            ["python3", "-c", embedded, str(src), str(out)], env=env, check=True, capture_output=True, text=True
        )
        dev_pod = (out / "llm-routing-dev.pod").read_text()
        dev_container = (out / "llm-routing-dev-router.container").read_text()
        # In dev, default.target MUST NOT be present (disabled and down by default)
        assert "default.target" not in dev_pod
        assert "default.target" not in dev_container
        # Container must still be tied to dev pod unit
        assert "WantedBy=llm-routing-dev-pod.service" in dev_container


def test_host_network_services_bind_loopback_only():
    # AGENTS.md E3/E5: the pod is a documented Network=host exception, so every
    # service must bind loopback itself. Regression: ClickHouse's official image
    # (config.d/docker_related_config.xml: listen_host :: + 0.0.0.0), postgres
    # (listen_addresses='*'), minio (":port"), uvicorn/litellm (--host 0.0.0.0)
    # listened on all interfaces, reachable from the firewalld trusted tailnet.
    script = (ROOT / "start-stack.sh").read_text()
    gen_block = script[script.index("generate_clickhouse_config()") : script.index("render_litellm_config()")]
    port_xml = gen_block[gen_block.index('port-override.xml" << EOF') : gen_block.index("settings-override.xml")]
    hosts = re.findall(r"<listen_host>([^<]*)</listen_host>", port_xml)
    # Two entries are required to override both wildcard entries of the image config.
    assert hosts == ["127.0.0.1", "::1"]
    # Unused wire-protocol listeners (9004/9005) are dropped, not left on shared host ports.
    assert '<mysql_port remove="1"/>' in port_xml
    assert '<postgresql_port remove="1"/>' in port_xml

    pod_yaml = (ROOT / "pod.yaml").read_text()
    templates = {p.name: p.read_text() for p in QUADLETS.glob("*.container")}
    combined = pod_yaml + "\n".join(templates.values())
    assert "0.0.0.0" not in combined
    assert not re.search(r"listen_host>\s*(::|0\.0\.0\.0)\s*<", combined)

    assert "listen_addresses=127.0.0.1,::1" in templates["llm-routing-postgres.container"]
    assert "- listen_addresses=127.0.0.1,::1" in pod_yaml

    minio = templates["llm-routing-minio.container"]
    assert "--address 127.0.0.1:MINIO_S3_PORT_PLACEHOLDER" in minio
    assert "--console-address 127.0.0.1:MINIO_CONSOLE_PORT_PLACEHOLDER" in minio
    assert '- "127.0.0.1:MINIO_S3_PORT_PLACEHOLDER"' in pod_yaml
    assert '- "127.0.0.1:MINIO_CONSOLE_PORT_PLACEHOLDER"' in pod_yaml

    assert "uvicorn main:app --host 127.0.0.1" in templates["llm-routing-router.container"]
    assert "uvicorn main:app --host 127.0.0.1" in pod_yaml

    for name in ("llm-routing-langfuse-web.container", "llm-routing-langfuse-worker.container"):
        assert "Environment=HOSTNAME=127.0.0.1" in templates[name], name
    assert pod_yaml.count("- name: HOSTNAME\n      value: 127.0.0.1") == 2

    valkey_lines = [t for n, t in templates.items() if "valkey" in n]
    assert valkey_lines and all("--bind 127.0.0.1" in t for t in valkey_lines)


def _quadlet_keys(text):
    """Return the [Container] key=value lines of a Quadlet template as a dict of lists."""
    keys = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            keys.setdefault(key.strip(), []).append(value)
    return keys


def test_state_engines_run_an_init_to_reap_orphaned_exec_children():
    # Incident 2026-10-31: a timed-out exec healthcheck child was reparented to the
    # postmaster (PID 1), died of SIGPIPE and forced a full crash-restart. catatonit
    # (RunInit=true, podman --init) must be PID 1 in every server-binary container
    # that runs an exec healthcheck. RunInit= is the Quadlet key (not Init=).
    for name in ("postgres", "clickhouse", "valkey-cache", "valkey-lf"):
        text = (QUADLETS / f"llm-routing-{name}.container").read_text()
        keys = _quadlet_keys(text)
        assert keys.get("RunInit") == ["true"], name
        assert "Init" not in keys, name
        assert "--init" not in text, name


def test_postgres_healthcheck_tolerates_host_io_stalls():
    keys = _quadlet_keys((QUADLETS / "llm-routing-postgres.container").read_text())
    assert keys["HealthCmd"] == ["pg_isready -q -U postgres -p POSTGRES_PORT_PLACEHOLDER"]
    assert keys["HealthTimeout"] == ["15s"]
    assert keys["HealthInterval"] == ["20s"]
    assert keys["HealthRetries"] == ["9"]
    assert keys["HealthStartPeriod"] == ["60s"]
    assert keys["HealthOnFailure"] == ["kill"]

    def seconds(value):
        return int(value.rstrip("s"))

    # Checks never overlap, and kill needs >= 3 min of consecutive failures (the
    # 2026-10-31 stall lasted ~90 s).
    assert seconds(keys["HealthTimeout"][0]) < seconds(keys["HealthInterval"][0])
    assert seconds(keys["HealthInterval"][0]) * int(keys["HealthRetries"][0]) >= 180


def test_postgres_probes_are_quiet_everywhere():
    pod_yaml = (ROOT / "pod.yaml").read_text()
    assert pod_yaml.count("- pg_isready\n        - -q\n") == 2
    assert "      timeoutSeconds: 15\n      failureThreshold: 9\n    name: postgres-db" in pod_yaml
    for script in ("scripts/backup.sh", "start-stack.sh"):
        text = (ROOT / script).read_text()
        for line in text.splitlines():
            if "pg_isready" in line and "podman exec" in line:
                assert " -q" in line, (script, line)
