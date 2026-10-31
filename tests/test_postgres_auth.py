"""Deployment-contract tests for PostgreSQL client authentication (pg_hba.conf).

The pod shares the host network, so loopback is reachable by every local process on the
host: loopback must require scram-sha-256, and every client/tool must authenticate.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUADLETS = ROOT / "quadlets"
SCRIPT = (ROOT / "start-stack.sh").read_text()
HBA_PATH = "/etc/postgresql/pg_hba.conf"


def _function(name: str) -> str:
    """Return the body of a top-level shell function of start-stack.sh."""
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", SCRIPT)
    assert match, name
    return match.group(0)


def _render_hba(tmp_path: Path) -> list[list[str]]:
    env = {**os.environ, "DATA_ROOT": str(tmp_path)}
    subprocess.run(
        ["bash", "-c", _function("generate_postgres_hba") + "generate_postgres_hba"],
        env=env,
        check=True,
        capture_output=True,
    )
    hba = tmp_path / "postgres-config" / "pg_hba.conf"
    assert oct(hba.stat().st_mode & 0o777) == "0o644"
    return [line.split() for line in hba.read_text().splitlines() if line.strip() and not line.startswith("#")]


def test_generated_hba_requires_scram_on_loopback(tmp_path):
    rules = _render_hba(tmp_path)
    assert rules == [
        ["local", "all", "postgres", "peer"],
        ["host", "all", "all", "127.0.0.1/32", "scram-sha-256"],
        ["host", "all", "all", "::1/128", "scram-sha-256"],
    ]
    for rule in rules:
        assert rule[-1] not in ("trust", "md5", "password"), rule
        if rule[0].startswith("host"):
            assert rule[-1] == "scram-sha-256", rule
    # Nothing for replication: the stack has no standby.
    assert all(rule[1] != "replication" for rule in rules)


def test_server_loads_the_generated_hba_everywhere():
    postgres = (QUADLETS / "llm-routing-postgres.container").read_text()
    execs = [line for line in postgres.splitlines() if line.startswith("Exec=")]
    assert len(execs) == 1
    assert execs[0].endswith(f"-c hba_file={HBA_PATH}")
    assert f"Volume=DATA_ROOT_PLACEHOLDER/postgres-config/pg_hba.conf:{HBA_PATH}:ro,Z" in postgres

    pod_yaml = (ROOT / "pod.yaml").read_text()
    assert f"    - -c\n    - hba_file={HBA_PATH}\n    env:\n    - name: POSTGRES_USER\n" in pod_yaml
    assert (
        f"    - mountPath: {HBA_PATH}\n      name: postgres-hba-config\n      subPath: pg_hba.conf\n      readOnly: true\n"
        in pod_yaml
    )
    assert (
        "      path: DATA_ROOT_PLACEHOLDER/postgres-config\n      type: DirectoryOrCreate\n    name: postgres-hba-config\n"
        in pod_yaml
    )

    deploy = _function("deploy_fresh_pod")
    assert deploy.index("generate_postgres_hba") < deploy.index("deploy_quadlets")


def test_in_container_admin_calls_use_the_peer_socket():
    # Every psql / pg_dump run through podman exec either uses the unix socket as the
    # postgres OS user (peer) or TCP with PGPASSWORD taken from the caller's environment.
    for name in ("start-stack.sh", "scripts/backup.sh"):
        text = (ROOT / name).read_text()
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "podman exec" not in line:
                continue
            call = line + (lines[i + 1] if line.rstrip().endswith("\\") else "")
            if not re.search(r"\b(psql|pg_dump)\b", call):
                continue
            peer = "podman exec -u postgres" in call or "-e PG_NEW_PASSWORD -u postgres" in call
            password = "podman exec -e PGPASSWORD" in call and " -h 127.0.0.1" in call
            assert peer or password, (name, call)
    backup = (ROOT / "scripts/backup.sh").read_text()
    assert (
        'podman exec -u postgres "${POD_NAME}-postgres-db" \\\n            pg_dump -U postgres -p "${POSTGRES_PORT}"'
        in backup
    )
    # Health probes need no authentication.
    assert "pg_isready -q -U postgres" in (QUADLETS / "llm-routing-postgres.container").read_text()


def test_password_is_checked_after_postgres_is_ready():
    health = _function("verify_stack_health")
    assert "if ! ensure_postgres_password; then\n                return 1\n            fi" in health
    ensure = _function("ensure_postgres_password")
    # The password never reaches argv: name-only -e copies the caller's value, and the
    # re-set reads it in psql via \getenv from a quoted heredoc.
    assert 'PGPASSWORD="$POSTGRES_PASSWORD" podman exec -e PGPASSWORD "$ctr"' in ensure
    assert 'PG_NEW_PASSWORD="$POSTGRES_PASSWORD" podman exec -i -e PG_NEW_PASSWORD -u postgres "$ctr"' in ensure
    assert "<< 'SQL'" in ensure
    assert "\\getenv pg_new_password PG_NEW_PASSWORD" in ensure
    assert "SET password_encryption = 'scram-sha-256';" in ensure
    assert "ALTER ROLE postgres PASSWORD :'pg_new_password';" in ensure
    assert "-e PGPASSWORD=" not in ensure and "-e PG_NEW_PASSWORD=" not in ensure


def _run_ensure(tmp_path: Path, exit_codes: list[int]) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run ensure_postgres_password against a fake podman that exits with exit_codes in turn."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    state = tmp_path / "n"
    state.write_text("0")
    codes = " ".join(str(c) for c in exit_codes)
    fake = bindir / "podman"
    fake.write_text(
        "#!/bin/bash\n"
        f'n=$(cat "{state}"); echo $((n + 1)) > "{state}"\n'
        f'echo "ARGV:$* PGPASSWORD=${{PGPASSWORD:-}} NEW=${{PG_NEW_PASSWORD:-}} STDIN=$(cat | tr "\\n" " ")" >> "{log}"\n'
        f"codes=({codes}); exit ${{codes[$n]}}\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "POD_NAME": "t-pod",
        "POSTGRES_PORT": "5442",
        "POSTGRES_PASSWORD": "s3cr'et$pw",
    }
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _function("ensure_postgres_password") + "ensure_postgres_password"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return result, log.read_text().splitlines()


def test_ensure_password_ok_needs_no_reset(tmp_path):
    result, calls = _run_ensure(tmp_path, [0])
    assert result.returncode == 0
    assert "password login (scram-sha-256) OK" in result.stdout
    assert len(calls) == 1
    argv = calls[0].split(" PGPASSWORD=")[0]
    assert "s3cr" not in argv
    assert "exec -e PGPASSWORD t-pod-postgres-db psql -h 127.0.0.1 -U postgres -p 5442" in argv
    assert "PGPASSWORD=s3cr'et$pw " in calls[0]
    assert "s3cr" not in result.stdout + result.stderr


def test_ensure_password_resets_from_env_when_login_fails(tmp_path):
    result, calls = _run_ensure(tmp_path, [1, 0])
    assert result.returncode == 0
    assert "re-setting the postgres role password" in result.stderr
    assert "re-set from .env" in result.stdout
    assert len(calls) == 2
    argv = calls[1].split(" PGPASSWORD=")[0]
    assert "exec -i -e PG_NEW_PASSWORD -u postgres t-pod-postgres-db psql -v ON_ERROR_STOP=1" in argv
    assert "s3cr" not in argv
    assert "NEW=s3cr'et$pw " in calls[1]
    assert "ALTER ROLE postgres PASSWORD :'pg_new_password';" in calls[1]
    assert "s3cr" not in result.stdout + result.stderr


def test_ensure_password_fails_loudly_when_reset_fails(tmp_path):
    result, calls = _run_ensure(tmp_path, [1, 1])
    assert result.returncode == 1
    assert "clients cannot log in" in result.stderr
    assert len(calls) == 2
