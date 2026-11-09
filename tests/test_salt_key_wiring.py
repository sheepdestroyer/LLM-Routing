"""How start-stack.sh and the LiteLLM Quadlet carry LITELLM_SALT_KEY (and the migrate-from key).

Both reach LiteLLM only as Podman secrets written from .env by scripts/litellm_secrets.py: never
through effective.env (the router reads it too) or an Environment= line. The check runs before the
running stack is touched, and a new database gets a salt key from the start.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
QUADLETS = ROOT / "quadlets"
SCRIPT = (ROOT / "start-stack.sh").read_text()
SALT = "sk-salt-0123456789abcdef0123456789abcdef0123456789abcdef"
OLD = "sk-litellm-old0123456789abcdef0123456789"


def _function(name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", SCRIPT)
    assert match, name
    return match.group(0)


def _renderer() -> str:
    return SCRIPT.split('python3 - "$src_dir" "$QUADLET_DIR" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]


def _render(namespace: str, **extra: str) -> str:
    """Render the real LiteLLM template with the embedded renderer of render_quadlets()."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LITELLM_")}
    env.update(
        {
            "POSTGRES_PASSWORD": "pg",
            "WORKDIR": str(ROOT),
            "HOME": str(ROOT),
            "LITELLM_MASTER_KEY": "sk-master-not-the-salt",
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
            "PUBLIC_BASE_URL": "https://llm-routing.vendeuvre.lan",
            "ROUTING_DOMAIN": "vendeuvre.lan",
            "LLAMA_CLASSIFIER_URL": "http://127.0.0.1:8083/v1",
            "LLAMA_SERVER_URL": "http://127.0.0.1:8083",
            "POD_NAME": "prod-router-pod",
            "DATA_ROOT": "/srv/data",
            "EFFECTIVE_ENV_FILE": "/srv/data/effective.env",
            "ROUTER_IMAGE": "ghcr.io/sheepdestroyer/llm-routing:v0",
            "ROUTER_PORT": "5000",
            "LITELLM_PORT": "4000",
            "LANGFUSE_WEB_PORT": "3001",
            "LANGFUSE_WORKER_PORT": "3030",
            "POSTGRES_PORT": "5432",
            "VALKEY_CACHE_PORT": "6379",
            "VALKEY_LF_PORT": "6380",
            "CLICKHOUSE_HTTP_PORT": "8123",
            "CLICKHOUSE_TCP_PORT": "9000",
            "MINIO_S3_PORT": "9002",
            "MINIO_CONSOLE_PORT": "9001",
            "QUADLET_NAMESPACE": namespace,
            **extra,
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "src", Path(tmp) / "out"
        src.mkdir()
        out.mkdir()
        (src / "llm-routing-litellm.container").write_text((QUADLETS / "llm-routing-litellm.container").read_text())
        subprocess.run(
            [sys.executable, "-c", _renderer(), str(src), str(out)], env=env, check=True, capture_output=True, text=True
        )
        return (out / f"{namespace}-litellm.container").read_text()


def test_only_the_litellm_template_carries_the_two_secrets():
    litellm = (QUADLETS / "llm-routing-litellm.container").read_text()
    assert "Secret=LITELLM_SALT_KEY_SECRET_PLACEHOLDER,type=env,target=LITELLM_SALT_KEY" in litellm
    assert "Secret=LITELLM_MIGRATE_FROM_SECRET_PLACEHOLDER,type=env,target=LITELLM_MIGRATE_FROM_MASTER_KEY" in litellm
    assert not re.search(r"(?m)^Environment=.*LITELLM_(SALT_KEY|MIGRATE_FROM)", litellm)
    for template in QUADLETS.glob("*.container"):
        if template.name != "llm-routing-litellm.container":
            assert "LITELLM_SALT_KEY" not in template.read_text(), template.name


def test_salt_key_renders_as_the_namespaced_secret_of_the_2026_11_08_dropin():
    rendered = _render("llm-routing-prod", LITELLM_SALT_KEY=SALT)
    secrets = re.findall(r"(?m)^Secret=.*$", rendered)
    # Same secret name and target as the hand-made drop-in, so both may coexist (duplicate flags).
    assert secrets == ["Secret=llm-routing-prod-litellm-salt-key,type=env,target=LITELLM_SALT_KEY"]
    assert SALT not in rendered
    assert "_PLACEHOLDER" not in rendered


def test_migration_boot_renders_only_the_migrate_from_secret():
    rendered = _render("llm-routing-dev", LITELLM_MIGRATE_FROM_MASTER_KEY=OLD)
    secrets = re.findall(r"(?m)^Secret=.*$", rendered)
    assert secrets == ["Secret=llm-routing-dev-litellm-migrate-from,type=env,target=LITELLM_MIGRATE_FROM_MASTER_KEY"]
    assert OLD not in rendered


def test_no_secret_line_without_the_variables():
    rendered = _render("llm-routing-prod", LITELLM_SALT_KEY="", LITELLM_MIGRATE_FROM_MASTER_KEY="")
    assert not re.search(r"(?m)^Secret=", rendered)
    assert "_PLACEHOLDER" not in rendered
    assert 'Environment="LITELLM_PORT=4000"' in rendered


def test_effective_env_never_holds_the_salt_migrate_from_or_read_keys():
    allowlist = SCRIPT.split("APPLICATION_ENV = {", 1)[1].split("}", 1)[0]
    names = set(re.findall(r'"([A-Z0-9_]+)"', allowlist))
    assert names.isdisjoint(
        {"LITELLM_SALT_KEY", "LITELLM_MIGRATE_FROM_MASTER_KEY", "LANGFUSE_READ_PUBLIC_KEY", "LANGFUSE_READ_SECRET_KEY"}
    )
    assert "LITELLM_MASTER_KEY" in names


def test_secrets_are_checked_and_written_before_the_stack_is_touched():
    apply_call = 'python3 "${WORKDIR}/scripts/litellm_secrets.py" apply --namespace "$QUADLET_NAMESPACE"'
    assert SCRIPT.count(apply_call) == 1
    position = SCRIPT.index(apply_call)
    guard = SCRIPT.rindex("if ! $STOP_MODE; then", 0, position)
    assert position - guard < 200
    assert "LiteLLM salt-key check failed; the running stack was not changed" in SCRIPT
    assert 'QUADLET_DIR="${HOME}/.config/containers/systemd/${QUADLET_NAMESPACE}"' in SCRIPT[:position]
    for later in (
        'python3 - "$EFFECTIVE_ENV_FILE" <<',
        "Taking pre-deploy database backup",
        "\nSTACK_OWNERSHIP=$(stack_ownership)",
    ):
        assert position < SCRIPT.index(later), later
    assert SCRIPT.index("ensure_litellm_salt_key || exit 1") < position


def test_render_prunes_the_migrate_from_secret_after_the_units():
    # The embedded renderer has its own "}" lines, so cut render_quadlets() at the next function.
    render = SCRIPT[SCRIPT.index("render_quadlets() {") : SCRIPT.index("deploy_quadlets() {")]
    prune_call = 'python3 "${WORKDIR}/scripts/litellm_secrets.py" prune --namespace "$QUADLET_NAMESPACE"'
    assert render.index("shutil.rmtree(staging_dir, ignore_errors=True)") < render.index(prune_call)
    assert "export LITELLM_SALT_KEY LITELLM_MIGRATE_FROM_MASTER_KEY" in render


def test_release_bundle_ships_the_secrets_helper():
    bundle = (ROOT / "scripts" / "package_release.sh").read_text()
    assert "    scripts/litellm_secrets.py \\\n" in bundle


# ── ensure_litellm_salt_key: a new database gets a salt key, an existing one never ──

PODMAN_STUB = """#!/bin/sh
# secret exists <name>: 0 when $FAKE_SECRET_EXISTS is set
[ "$1 $2" = "secret exists" ] && [ -n "$FAKE_SECRET_EXISTS" ] && exit 0
exit 1
"""


def _ensure(tmp_path: Path, *, data: str = "empty", secret: bool = False, **env_extra: str):
    env_file = tmp_path / ".env"
    env_file.write_text('LITELLM_MASTER_KEY="sk-master"\n', encoding="utf-8")
    data_root = tmp_path / "data"
    pg = data_root / "postgres-data"
    pg.mkdir(parents=True)
    if data == "cluster":
        (pg / "data").mkdir()
    elif data == "unlistable":
        pg.chmod(0)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "podman"
    stub.write_text(PODMAN_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LITELLM_")}
    env.update(
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        ENV_FILE=str(env_file),
        DATA_ROOT=str(data_root),
        QUADLET_NAMESPACE="llm-routing-prod",
        **({"FAKE_SECRET_EXISTS": "1"} if secret else {}),
        **env_extra,
    )
    program = (
        "set -euo pipefail\n"
        + _function("gen_hex")
        + _function("postgres_data_is_new")
        + _function("ensure_litellm_salt_key")
        + "ensure_litellm_salt_key\n"
        + 'python3 -c \'import os; print("exported" if os.environ.get("LITELLM_SALT_KEY") else "absent")\'\n'
    )
    try:
        proc = subprocess.run(
            ["bash", "-c", program], env=env, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL
        )
    finally:
        pg.chmod(0o755)
    return proc, env_file.read_text(encoding="utf-8")


def test_a_new_database_gets_a_generated_salt_key(tmp_path):
    proc, env_text = _ensure(tmp_path)
    match = re.search(r'(?m)^LITELLM_SALT_KEY="([0-9a-f]{64})"$', env_text)
    assert match
    assert match.group(1) not in proc.stdout + proc.stderr
    assert f"✓ Generated new LITELLM_SALT_KEY (new database) and saved to {tmp_path / '.env'}" in proc.stdout
    assert proc.stdout.rstrip().endswith("exported")


@pytest.mark.parametrize(
    ("data", "secret", "extra"),
    [
        ("cluster", False, {}),  # an existing database: its values may be under the master key
        ("unlistable", False, {}),  # cannot prove it is empty
        ("empty", True, {}),  # the secret already exists (data elsewhere)
        ("empty", False, {"LITELLM_MIGRATE_FROM_MASTER_KEY": OLD}),  # a migration boot
    ],
)
def test_no_salt_key_is_generated_unless_the_database_is_new(tmp_path, data, secret, extra):
    if data == "unlistable" and os.geteuid() == 0:
        pytest.skip("root can list a mode-0 directory")
    proc, env_text = _ensure(tmp_path, data=data, secret=secret, **extra)
    assert "LITELLM_SALT_KEY" not in env_text
    assert "Generated new LITELLM_SALT_KEY" not in proc.stdout
    assert proc.stdout.rstrip().endswith("absent")


def test_a_salt_key_from_env_is_kept_and_exported(tmp_path):
    proc, env_text = _ensure(tmp_path, LITELLM_SALT_KEY=SALT)
    assert "LITELLM_SALT_KEY" not in env_text
    assert proc.stdout.rstrip().endswith("exported")
