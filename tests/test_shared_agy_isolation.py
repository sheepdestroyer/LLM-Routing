"""Execute the actual shell integration block against recording command stubs."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("namespace", ["llm-routing-dev", "llm-routing-prod"])
def test_shared_agy_management_is_production_only(tmp_path, namespace):
    script = (ROOT / "start-stack.sh").read_text()
    block = script.split("# Ensure host agy daemon systemd service is installed and updated", 1)[1]
    block = block.split("# Ensure the env file exists", 1)[0]
    home = tmp_path / "home"
    unit = home / ".config/systemd/user/agy-daemon.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("existing production unit\n")
    log = tmp_path / "systemctl.log"
    stub = tmp_path / "systemctl"
    stub.write_text('#!/bin/sh\nprintf "%s\n" "$*" >> "$CALL_LOG"\nexit 0\n')
    stub.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "WORKDIR": str(ROOT),
        "QUADLET_NAMESPACE": namespace,
        "CALL_LOG": str(log),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    subprocess.run(["bash", "-eu", "-c", block], env=env, check=True, capture_output=True, text=True)
    if namespace == "llm-routing-dev":
        assert unit.read_text() == "existing production unit\n"
        assert not log.exists()
    else:
        assert str(ROOT / "scripts/host_agy_daemon.py") in unit.read_text()
        assert "restart agy-daemon.service" in log.read_text()
