import os
import sys

import pytest
from pathlib import Path

# Dynamic project root discovery
root = Path(__file__).resolve().parent.parent

# Insert paths to sys.path
for path in [str(root), str(root / "router"), str(root / "scripts")]:
    if path not in sys.path:
        sys.path.insert(0, path)

# Set common environment variables
os.environ.setdefault("CONFIG_PATH", str(root / "router" / "config.yaml"))
os.environ.setdefault("LITELLM_MASTER_KEY", "sk-litellm-testkey")
os.environ.setdefault("ROUTER_API_KEY", "sk-router-testkey")


@pytest.fixture(autouse=True)
def _isolate_agy_daemon_state(tmp_path, monkeypatch):
    """Keep host_agy_daemon off the real agy login, daemon HOME and agy-run lock in every test."""
    import host_agy_daemon

    state = tmp_path / "agy-daemon-state"
    home = state / "home"
    monkeypatch.setattr(host_agy_daemon, "STATE_DIR", str(state))
    monkeypatch.setattr(host_agy_daemon, "DAEMON_HOME", str(home))
    monkeypatch.setattr(host_agy_daemon, "RUNS_DIR", str(state / "runs"))
    monkeypatch.setattr(
        host_agy_daemon,
        "CACHE_FILE",
        str(home / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"),
    )
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(tmp_path / "host-token.json"))
    monkeypatch.setattr(host_agy_daemon, "HOST_SETTINGS_PATH", str(tmp_path / "host-settings.json"))
    monkeypatch.setattr(host_agy_daemon, "AGY_RUN_LOCK", str(tmp_path / "agy-run.lock"))
    monkeypatch.setattr(host_agy_daemon, "ensure_auth", lambda run_timeout, background=False: "daemon")
    monkeypatch.setattr(host_agy_daemon, "LIVE_CHECK_INTERVAL", 0.0)
    yield
