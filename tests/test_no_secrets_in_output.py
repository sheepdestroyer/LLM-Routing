"""Deploy output never carries a secret value.

start-stack.sh output (also printed by upgrade-prod.sh and start-dev.sh) ends up in terminals,
upgrade logs and agent transcripts. The success banner names where each credential lives
(variable and file), never its value; no echo/printf in the deploy scripts expands a credential.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "start-stack.sh").read_text()
DEPLOY_SCRIPTS = (
    "start-stack.sh",
    "start-dev.sh",
    "scripts/upgrade-prod.sh",
    "scripts/backup.sh",
    "scripts/dev-setup.sh",
)

VALUES = {
    "LITELLM_MASTER_KEY": "sk-litellm-0123456789abcdef0123",
    "UI_PASSWORD": "ui-pass-fedcba9876543210",
    "UI_USERNAME": "ui-user-not-for-output",
    "ROUTER_API_KEY": "router-key-00112233445566778899",
}


def _function(name: str) -> str:
    """Return the body of a top-level shell function of start-stack.sh."""
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", SCRIPT)
    assert match, name
    return match.group(0)


def _banner(tmp_path: Path, env_names: list[str], overlay_names: list[str] | None = None, **values: str) -> str:
    """Run print_success_banner the way start-stack.sh does: files sourced, values in the environment."""
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f'{name}="{VALUES[name]}"\n' for name in env_names))
    overlay = ""
    if overlay_names is not None:
        overlay_file = tmp_path / ".env.dev"
        overlay_file.write_text("".join(f"export {name}={VALUES[name]}\n" for name in overlay_names))
        overlay = str(overlay_file)
    env = {key: val for key, val in os.environ.items() if key not in VALUES}
    env.update(
        {
            "ENV_FILE": str(env_file),
            "ENV_OVERLAY_FILE": overlay,
            "PUBLIC_BASE_URL": "https://llm-routing.example.lan",
            "LOCAL_BASE_URL": "http://localhost:5000",
            "PROXY_BASE_URL_DERIVED": "https://litellm.example.lan",
            **values,
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + _function("env_var_source")
            + _function("print_success_banner")
            + 'print_success_banner "LLM Triage Gateway restarted!"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    output = result.stdout + result.stderr
    for value in VALUES.values():
        assert value not in output
    assert "🎉 SUCCESS: LLM Triage Gateway restarted!" in result.stdout
    assert "🔐 LiteLLM Admin UI: https://litellm.example.lan/ui/" in result.stdout
    return result.stdout


def test_banner_names_where_the_ui_login_lives_not_its_value(tmp_path):
    out = _banner(tmp_path, list(VALUES), **VALUES)
    env_file = tmp_path / ".env"
    assert f"   Username: UI_USERNAME in {env_file}\n" in out
    assert f"   Password: UI_PASSWORD in {env_file}\n" in out
    assert f"🔑 Gateway API key : a LiteLLM virtual key, or ROUTER_API_KEY in {env_file}\n" in out
    assert "gateway-pass" not in out  # test-only router key, rejected outside pytest


def test_banner_points_at_the_master_key_when_ui_password_is_unset(tmp_path):
    out = _banner(
        tmp_path,
        ["LITELLM_MASTER_KEY", "ROUTER_API_KEY"],
        LITELLM_MASTER_KEY=VALUES["LITELLM_MASTER_KEY"],
        ROUTER_API_KEY=VALUES["ROUTER_API_KEY"],
    )
    assert "   Username: admin (UI_USERNAME unset)\n" in out
    assert f"   Password: LITELLM_MASTER_KEY in {tmp_path / '.env'} (UI_PASSWORD unset)\n" in out


def test_overlay_wins_and_the_calling_environment_is_named(tmp_path):
    out = _banner(tmp_path, ["LITELLM_MASTER_KEY", "UI_PASSWORD"], ["UI_PASSWORD"], **VALUES)
    assert f"   Password: UI_PASSWORD in {tmp_path / '.env.dev'}\n" in out
    # Set by the caller, defined in neither file.
    assert "   Username: UI_USERNAME in the calling environment\n" in out
    assert "or ROUTER_API_KEY in the calling environment\n" in out


def test_both_success_paths_print_the_banner_and_record_the_overlay():
    assert SCRIPT.count('print_success_banner "') == 2
    assert 'print_success_banner "LLM Triage Gateway restarted!"' in SCRIPT
    assert 'print_success_banner "LLM Triage Gateway successfully deployed!"' in SCRIPT
    assert 'ENV_OVERLAY_FILE="$DEV_ENV_FILE"' in SCRIPT
    assert 'ENV_OVERLAY_FILE="${WORKDIR}/.env.dev"' in SCRIPT
    assert "Password: $LITELLM_MASTER_KEY" not in SCRIPT
    assert 'Password: ${ui_password}"' in _function("print_success_banner")


def _credential_names() -> set[str]:
    """Credentials: secret-like names of start-stack.sh's container env allowlist, plus local holders."""
    allowlist = SCRIPT.split("APPLICATION_ENV = {", 1)[1].split("}", 1)[0]
    names = set(re.findall(r'"([A-Z0-9_]+)"', allowlist))
    secret = {n for n in names if re.search(r"KEY|PASSWORD|SECRET|SALT|AUTH|TOKEN", n) and not n.endswith("_URL")}
    assert {"LITELLM_MASTER_KEY", "UI_PASSWORD", "POSTGRES_PASSWORD", "ROUTER_API_KEY"} <= secret
    # Kept out of the allowlist on purpose (Podman secrets of the LiteLLM container only).
    litellm_secrets = {"LITELLM_SALT_KEY", "LITELLM_MIGRATE_FROM_MASTER_KEY"}
    assert not names & litellm_secrets
    return (
        secret
        | litellm_secrets
        | {"escaped_key", "rand_key", "ACTIVE_OAUTH", "verifier", "PGPASSWORD", "PG_NEW_VERIFIER"}
    )


def test_no_output_statement_expands_a_credential():
    secrets = _credential_names()
    offenders = []
    for name in DEPLOY_SCRIPTS:
        for number, line in enumerate((ROOT / name).read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not re.search(r"\b(echo|printf)\b", stripped):
                continue
            # Persisting a generated value into .env, or piping it to podman's stdin, prints nothing.
            if stripped.endswith('>> "$ENV_FILE"') or "| podman secret create" in stripped:
                continue
            expanded = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", stripped))
            if expanded & secrets:
                offenders.append(f"{name}:{number}: {sorted(expanded & secrets)}")
    assert not offenders, offenders
