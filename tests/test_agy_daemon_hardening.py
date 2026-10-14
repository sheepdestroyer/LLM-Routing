"""Isolation, login refresh, concurrency cap and live health of the host agy daemon."""

import contextlib
import errno
import fcntl
import io
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import host_agy_daemon as d

# The conftest stubs ensure_auth for the legacy suites; these tests exercise the real one.
REAL_ENSURE_AUTH = d.ensure_auth


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def token_doc(expires_in: float, access: str = "at-1", refresh: str | None = "rt-1") -> dict:
    token = {"access_token": access, "token_type": "Bearer", "expiry": iso(time.time() + expires_in)}
    if refresh is not None:
        token["refresh_token"] = refresh
    return {"token": token, "auth_method": "consumer", "id_token": "idt"}


def write_json(path, data) -> None:
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def fake_agy(tmp_path, body: str) -> str:
    """An executable stand-in for agy; ``body`` is Python run with the token path as ``p``."""
    script = tmp_path / "fake-agy"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys, time, datetime\n"
        "p = os.path.join(os.environ['HOME'], '.gemini', 'antigravity-cli', 'antigravity-oauth-token')\n"
        "def iso(e):\n"
        "    return datetime.datetime.fromtimestamp(e, datetime.UTC).strftime('%Y-%m-%dT%H:%M:%S.000000001Z')\n" + body
    )
    script.chmod(0o755)
    return str(script)


REFRESHING_AGY = (
    "d = json.load(open(p))\n"
    "d['token']['access_token'] = 'at-new'\n"
    "d['token']['expiry'] = iso(time.time() + 3600)\n"
    "json.dump(d, open(p, 'w'))\n"
    "print('Fetching available models...')\n"
    "print('gemini-3.8-flash-low\\tGemini 3.8 Flash (Low)')\n"
)


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(monkeypatch):
    port = find_free_port()
    srv = d.ThreadingHTTPServer(("127.0.0.1", port), d.AgyDaemonHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def no_slots(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(d, "SPAWN_SLOTS", sem)
    monkeypatch.setattr(d, "QUEUE_TIMEOUT", 0.01)
    yield
    sem.release()


# ── policy and guard ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "autonomous", "expected"),
    [
        ("all", False, "native"),
        ("none", True, "deny"),
        ("sse", True, "native"),
        ("sse", False, "deny"),
    ],
)
def test_tool_policy(monkeypatch, mode, autonomous, expected):
    monkeypatch.setattr(d, "NATIVE_TOOLS", mode)
    assert d.tool_policy(autonomous) == expected


@pytest.mark.parametrize(("policy", "denied"), [("native", False), ("deny", True), (None, True)])
def test_tool_guard(policy, denied):
    out = io.StringIO()
    environ = {} if policy is None else {"AGY_DAEMON_TOOL_POLICY": policy}
    assert d.tool_guard_main(io.StringIO('{"toolCall": {"name": "run_command"}}'), out, environ) == 0
    if denied:
        assert json.loads(out.getvalue())["decision"] == "deny"
    else:
        assert out.getvalue() == ""


def test_main_tool_guard_and_server(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(d.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(d.sys, "stdout", out)
    monkeypatch.delenv("AGY_DAEMON_TOOL_POLICY", raising=False)
    assert d.main(["host_agy_daemon.py", "--tool-guard"]) == 0
    assert "deny" in out.getvalue()
    called = []
    monkeypatch.setattr(d, "run_server", lambda: called.append(True))
    assert d.main(["host_agy_daemon.py"]) == 0
    assert called == [True]


def test_log_writes_stderr(capsys):
    d.log("hello")
    assert "agy-daemon: hello" in capsys.readouterr().err


# ── files, home and workspaces ───────────────────────────────────────────────


def test_write_private_atomic(tmp_path):
    target = tmp_path / "sub" / "f.json"
    d.write_private_atomic(str(target), "x")
    assert target.read_text() == "x"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["f.json"]


def test_write_private_atomic_failure_cleans_up(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(d.os, "replace", boom)
    with pytest.raises(OSError, match="rename failed"):
        d.write_private_atomic(str(tmp_path / "f.json"), "x")
    assert list(tmp_path.iterdir()) == []


def test_run_workspace(monkeypatch):
    with d.run_workspace(True) as (work, tmp):
        agent = os.path.join(work, ".agents", "agents", f"{d.PLAIN_AGENT}.md")
        text = open(agent).read()
        assert "excludeDefaultComponents: true" in text and "tools: []" in text
        assert os.path.isdir(tmp)
        run = os.path.dirname(work)
    assert not os.path.exists(run)
    with d.run_workspace(False) as (work, _tmp):
        assert not os.path.exists(os.path.join(work, ".agents"))


def test_prepare_home_writes_config_and_prunes_cache():
    os.makedirs(d.RUNS_DIR, exist_ok=True)
    live = os.path.join(d.RUNS_DIR, "run-live")
    os.makedirs(live)
    gone = os.path.join(d.RUNS_DIR, "run-gone")
    write_json(d.CACHE_FILE, {gone: "c1", live: "c2", "/elsewhere": "c3"})
    d.prepare_home()
    config = os.path.join(d.DAEMON_HOME, ".gemini", "config")
    assert json.load(open(os.path.join(config, "mcp_config.json"))) == {"mcpServers": {}}
    hooks = json.load(open(os.path.join(config, "hooks.json")))
    command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command.endswith("host_agy_daemon.py --tool-guard")
    assert stat.S_IMODE(os.stat(d.DAEMON_HOME).st_mode) == 0o700
    settings_path = os.path.join(d.DAEMON_HOME, ".gemini", "antigravity-cli", "settings.json")
    settings = json.load(open(settings_path))
    assert settings == {"toolPermission": "always-proceed", "trustedWorkspaces": [d.RUNS_DIR], "useG1Credits": False}
    write_json(d.HOST_SETTINGS_PATH, {"useG1Credits": True, "permissions": {"allow": ["command(cat)"]}})
    d.prepare_home()
    assert json.load(open(settings_path))["useG1Credits"] is True
    assert json.load(open(d.CACHE_FILE)) == {live: "c2", "/elsewhere": "c3"}
    # Nothing left to prune: the file is not rewritten.
    before = os.stat(d.CACHE_FILE).st_mtime_ns
    d.prepare_home()
    assert os.stat(d.CACHE_FILE).st_mtime_ns == before


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_prepare_home_tolerates_bad_cache(content):
    if content is not None:
        os.makedirs(os.path.dirname(d.CACHE_FILE), exist_ok=True)
        with open(d.CACHE_FILE, "w") as f:
            f.write(content)
    d.prepare_home()
    assert os.path.isdir(d.RUNS_DIR)


def test_spawn_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = d.spawn_env("gemini-3.8-flash-low", "deny", "/t")
    assert "GITHUB_TOKEN" not in env
    assert env["LANG"] == "C.UTF-8"
    assert env["HOME"] == d.DAEMON_HOME
    assert env["TMPDIR"] == "/t"
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert env["AGY_DAEMON_TOOL_POLICY"] == "deny"
    assert env["CASCADE_DEFAULT_MODEL_OVERRIDE"] == "gemini-3.8-flash-low"
    monkeypatch.delenv("PATH")
    env = d.spawn_env("", "native", "/t", home="/h")
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["HOME"] == "/h"
    assert "CASCADE_DEFAULT_MODEL_OVERRIDE" not in env


def test_agent_args():
    assert d.agent_args(True) == ["--agent", d.PLAIN_AGENT, "--disable-slash-commands"]
    assert d.agent_args(False) == ["--disable-slash-commands"]
    assert d.agent_args(False, slash_commands=True) == []


def test_get_last_conversation_id_by_workspace():
    write_json(d.CACHE_FILE, {"/w1": "c1"})
    assert d.get_last_conversation_id("/w1") == "c1"
    assert d.get_last_conversation_id("/w2") is None


# ── concurrency ──────────────────────────────────────────────────────────────


def test_spawn_slot_counts(monkeypatch):
    monkeypatch.setattr(d, "SPAWN_SLOTS", threading.BoundedSemaphore(2))
    with d.spawn_slot():
        assert d._SLOT_STATE["in_use"] == 1
    assert d._SLOT_STATE["in_use"] == 0


def test_spawn_slot_busy(no_slots):
    with pytest.raises(d.DaemonBusyError, match="capacity"), d.spawn_slot():
        pass  # pragma: no cover - never entered


# ── agy-run lock ─────────────────────────────────────────────────────────────


def test_agy_lock_exclusive(monkeypatch):
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    with d.agy_lock(1.0):
        fd = os.open(d.AGY_RUN_LOCK, os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    holder = os.open(d.AGY_RUN_LOCK, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError), d.agy_lock(0.05):
            pass  # pragma: no cover - never entered
    finally:
        os.close(holder)


def test_agy_lock_unexpected_error(monkeypatch):
    def broken(fd, op):
        raise OSError(errno.EBADF, "bad fd")

    monkeypatch.setattr(d.fcntl, "flock", broken)
    with pytest.raises(OSError, match="bad fd"), d.agy_lock(1.0):
        pass  # pragma: no cover - never entered


# ── token parsing ────────────────────────────────────────────────────────────


def test_expiry_to_ms():
    assert d.expiry_to_ms(True) == 0
    assert d.expiry_to_ms(1700000000) == 1700000000000
    assert d.expiry_to_ms(1700000000000) == 1700000000000
    assert d.expiry_to_ms("2026-10-14T13:47:17.135452299+02:00") == 1791978437135
    assert d.expiry_to_ms("2026-10-14T11:47:17Z") == 1791978437000
    assert d.expiry_to_ms("garbage") == 0
    assert d.expiry_to_ms("  ") == 0
    assert d.expiry_to_ms(None) == 0


def test_read_token_and_expiry(tmp_path):
    path = tmp_path / "t.json"
    assert d.read_token(str(path)) is None
    path.write_text("nope")
    assert d.read_token(str(path)) is None
    path.write_text("[]")
    assert d.read_token(str(path)) is None
    doc = token_doc(100)
    write_json(path, doc)
    assert d.read_token(str(path)) == doc
    assert d.token_expiry(None) == 0.0
    assert d.token_expiry({"token": {"expiry": "2026-01-01T00:00:00Z"}}) == 0.0
    assert d.token_expiry(doc) == pytest.approx(time.time() + 100, abs=5)


def test_get_auth_status_reports_refresh_token(tmp_path):
    path = tmp_path / "t.json"
    write_json(path, token_doc(-10))
    status = d.get_auth_status(str(path))
    assert status["status"] == "valid_silent_refresh"
    assert status["refresh_token_present"] is True


# ── host login refresh ───────────────────────────────────────────────────────


@pytest.mark.parametrize("doc", [None, token_doc(-10, refresh=None)])
def test_refresh_needs_a_refresh_token(doc):
    if doc is not None:
        write_json(d.CLI_TOKEN_PATH, doc)
    with pytest.raises(d.AuthRefreshError, match="re-login"):
        d.refresh_host_login()


def test_refresh_host_login_success(tmp_path, monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(120, access="at-old"))
    monkeypatch.setattr(d, "AGY_BINARY", fake_agy(tmp_path, REFRESHING_AGY))
    d.refresh_host_login()
    host = json.load(open(d.CLI_TOKEN_PATH))
    assert host["token"]["access_token"] == "at-new"
    assert host["token"]["refresh_token"] == "rt-1"
    assert host["id_token"] == "idt"
    assert d.token_expiry(host) > time.time() + 3000
    assert stat.S_IMODE(os.stat(d.CLI_TOKEN_PATH).st_mode) == 0o600
    assert [p for p in os.listdir(d.STATE_DIR) if p.startswith("refresh-")] == []


def test_refresh_keeps_a_newer_host_token(tmp_path, monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(7200, access="at-other"))
    monkeypatch.setattr(d, "AGY_BINARY", fake_agy(tmp_path, REFRESHING_AGY))
    d.refresh_host_login()
    assert json.load(open(d.CLI_TOKEN_PATH))["token"]["access_token"] == "at-other"


@pytest.mark.parametrize(
    "body",
    [
        "sys.exit(1)\n",  # agy failed
        "print('m')\n",  # agy did not refresh (forced expiry kept)
    ],
)
def test_refresh_host_login_failures(tmp_path, monkeypatch, body):
    write_json(d.CLI_TOKEN_PATH, token_doc(60))
    monkeypatch.setattr(d, "AGY_BINARY", fake_agy(tmp_path, body))
    with pytest.raises(d.AuthRefreshError, match="did not refresh"):
        d.refresh_host_login()
    assert json.load(open(d.CLI_TOKEN_PATH))["token"]["access_token"] == "at-1"


def test_refresh_host_login_spawn_error(tmp_path, monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(60))
    monkeypatch.setattr(d, "AGY_BINARY", str(tmp_path / "missing-agy"))
    with pytest.raises(d.AuthRefreshError, match="FileNotFoundError"):
        d.refresh_host_login()


def test_install_host_copy():
    daemon_path = d.daemon_token_path()
    assert d._install_host_copy(None, None) is False
    assert d._install_host_copy(token_doc(100), token_doc(200)) is False
    assert d._install_host_copy(token_doc(300), token_doc(200)) is True
    assert stat.S_IMODE(os.stat(daemon_path).st_mode) == 0o600


# ── ensure_auth ──────────────────────────────────────────────────────────────


def test_ensure_auth_daemon_copy_valid():
    write_json(d.daemon_token_path(), token_doc(3000))
    assert REAL_ENSURE_AUTH(600) == "daemon"
    assert d.AUTH_STATE["detail"] == "daemon"


def test_ensure_auth_copies_host():
    write_json(d.CLI_TOKEN_PATH, token_doc(3000, access="at-host"))
    assert REAL_ENSURE_AUTH(600) == "host"
    assert json.load(open(d.daemon_token_path()))["token"]["access_token"] == "at-host"


def test_ensure_auth_refreshes(monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(60))

    def refresh():
        write_json(d.CLI_TOKEN_PATH, token_doc(3600, access="at-fresh"))

    monkeypatch.setattr(d, "refresh_host_login", refresh)
    assert REAL_ENSURE_AUTH(600) == "refreshed"
    assert json.load(open(d.daemon_token_path()))["token"]["access_token"] == "at-fresh"


def test_ensure_auth_other_consumer_refreshed_while_waiting(monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(60))

    @contextlib.contextmanager
    def lock(timeout):
        write_json(d.CLI_TOKEN_PATH, token_doc(3600, access="at-other"))
        yield

    def refresh():  # pragma: no cover - must not be called
        raise AssertionError("refreshed although the host token is fresh")

    monkeypatch.setattr(d, "agy_lock", lock)
    monkeypatch.setattr(d, "refresh_host_login", refresh)
    assert REAL_ENSURE_AUTH(600) == "refreshed"
    assert json.load(open(d.daemon_token_path()))["token"]["access_token"] == "at-other"


def test_ensure_auth_lock_timeout(monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(60))

    @contextlib.contextmanager
    def lock(timeout):
        raise TimeoutError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(d, "agy_lock", lock)
    assert REAL_ENSURE_AUTH(600) == "lock-timeout"
    assert os.path.exists(d.daemon_token_path())


def test_ensure_auth_refresh_failed(monkeypatch):
    write_json(d.CLI_TOKEN_PATH, token_doc(60))

    def refresh():
        raise d.AuthRefreshError("nope")

    monkeypatch.setattr(d, "refresh_host_login", refresh)
    assert REAL_ENSURE_AUTH(600) == "refresh-failed"


# ── live login check and health ──────────────────────────────────────────────


def test_login_status_states(monkeypatch):
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": False, "checked_at": None, "detail": "x", "models": 0})
    monkeypatch.setattr(d, "LIVE_CHECK_INTERVAL", 600.0)
    assert d.login_status()["state"] == "unverified"
    d.record_login(False, "bad")
    assert d.login_status()["state"] == "failed"
    d.record_login(True, "good", 3)
    assert d.login_status()["state"] == "verified"
    assert d.login_status(time.time() + 2000)["state"] == "stale"
    monkeypatch.setattr(d, "LIVE_CHECK_INTERVAL", 0.0)
    assert d.login_status(time.time() + 2000)["state"] == "verified"


def test_run_agy_models(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "AGY_BINARY", fake_agy(tmp_path, "print(os.environ['AGY_DAEMON_TOOL_POLICY'])\n"))
    assert d.run_agy_models() == (0, "deny\n")


@pytest.mark.parametrize(
    ("body", "state"),
    [("print('gemini-3.8-flash-low\\tGemini')\n", "verified"), ("sys.exit(2)\n", "failed")],
)
def test_live_login_check(tmp_path, monkeypatch, body, state):
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": False, "checked_at": None, "detail": "", "models": 0})
    monkeypatch.setattr(d, "AGY_BINARY", fake_agy(tmp_path, body))
    assert d.live_login_check() is True
    assert d.login_status()["state"] == state


def test_live_login_check_spawn_error(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": True, "checked_at": None, "detail": "", "models": 0})
    monkeypatch.setattr(d, "AGY_BINARY", str(tmp_path / "missing"))
    assert d.live_login_check() is True
    assert d.login_status()["detail"] == "agy models failed: FileNotFoundError"


def test_live_login_check_busy(no_slots):
    assert d.live_login_check() is False


def test_live_check_loop(monkeypatch):
    calls = []
    stop = threading.Event()

    def check():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()
        return True

    monkeypatch.setattr(d, "live_login_check", check)
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": True, "checked_at": None, "detail": "", "models": 0})
    d.live_check_loop(stop, 0.01)
    assert len(calls) == 2
    assert d.LOGIN_STATE["detail"] == "live check error: RuntimeError"


def test_health_payload(monkeypatch):
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": True, "checked_at": time.time(), "detail": "", "models": 2})
    payload = d.health_payload()
    assert payload["health"] == "ok" and payload["status"] == "ok"
    assert payload["concurrency"] == {"max": d.MAX_CONCURRENT, "in_use": 0}
    d.record_login(False, "bad")
    assert d.health_payload()["health"] == "degraded"


def test_health_endpoint(server):
    with urllib.request.urlopen(f"{server}/health", timeout=5) as resp:
        data = json.loads(resp.read())
    assert data["health"] in ("ok", "degraded")
    assert "login" in data and "daemon_auth" in data


# ── HTTP: capacity, models, usage, chat wiring ───────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/models", None),
        ("POST", "/run", {"prompt": "hi"}),
        ("POST", "/v1/chat/completions", {"model": "agy-gemini", "messages": [{"role": "user", "content": "hi"}]}),
    ],
)
def test_busy_returns_503(server, no_slots, method, path, payload):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{server}{path}", data=data, method=method)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 503
    assert exc.value.headers["Retry-After"] == "5"
    assert "capacity" in json.loads(exc.value.read())["error"]["message"]


def test_get_models_records_login(server, monkeypatch):
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": False, "checked_at": None, "detail": "", "models": 0})
    monkeypatch.setattr(d, "run_agy_models", lambda timeout=60: (0, "gemini-3.8-flash-low\tGemini\n"))
    with urllib.request.urlopen(f"{server}/models", timeout=5) as resp:
        data = json.loads(resp.read())
    assert data["models"][0]["id"] == "gemini-3.8-flash-low"
    assert d.login_status()["state"] == "verified"


def test_get_models_empty_does_not_verify(server, monkeypatch):
    monkeypatch.setattr(d, "LOGIN_STATE", {"verified": False, "checked_at": None, "detail": "", "models": 0})
    monkeypatch.setattr(d, "run_agy_models", lambda timeout=60: (1, ""))
    with urllib.request.urlopen(f"{server}/models", timeout=5) as resp:
        assert json.loads(resp.read())["models"] == []
    assert d.login_status()["state"] == "unverified"


def test_get_usage_success(server, monkeypatch):
    seen = {}

    async def fake_print(prompt, **kwargs):
        seen.update(kwargs)
        return {"returncode": 0, "stdout": "Quota:\nGemini  daily  80%  tomorrow", "stderr": ""}

    monkeypatch.setattr(d, "execute_agy_print", fake_print)
    with urllib.request.urlopen(f"{server}/usage", timeout=5) as resp:
        data = json.loads(resp.read())
    assert data["quotas"][0]["category"] == "Gemini"
    assert seen["slash_commands"] is True and seen["plain_agent"] is False


def test_complete_chat_passes_policy(server, monkeypatch):
    seen = {}

    async def fake_stream_json(
        prompt,
        model_override="",
        conversation_id=None,
        timeout=600.0,
        tools=None,
        intercept_tools=True,
        effort=None,
        policy="deny",
        plain_agent=False,
    ):
        seen.update(policy=policy, plain_agent=plain_agent, prompt=prompt)
        return {"returncode": 0, "stdout": "hello", "stderr": "", "usage": None, "tool_calls": []}

    monkeypatch.setattr(d, "execute_agy_stream_json", fake_stream_json)
    monkeypatch.setattr(d, "NATIVE_TOOLS", "sse")
    body = {"model": "agy-gemini", "messages": [{"role": "user", "content": "hi"}]}
    req = urllib.request.Request(f"{server}/v1/chat/completions", data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert json.loads(resp.read())["choices"][0]["message"]["content"] == "hello"
    assert seen["policy"] == "deny" and seen["plain_agent"] is True
    assert "no workspace, file or command tools" in seen["prompt"]
    body["model"] = "agy-sse"
    req = urllib.request.Request(f"{server}/v1/chat/completions", data=json.dumps(body).encode())
    urllib.request.urlopen(req, timeout=5).read()
    assert seen["policy"] == "native" and seen["plain_agent"] is False


@pytest.mark.asyncio
async def test_stream_json_spawn_is_isolated(monkeypatch):
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs["env"]
        seen["cwd"] = kwargs["cwd"]
        seen["agent_file"] = os.path.exists(os.path.join(kwargs["cwd"], ".agents", "agents", f"{d.PLAIN_AGENT}.md"))
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b'{"event":"result","result":{"response":"ok"}}\n', b""))
        return proc

    monkeypatch.setattr(d.asyncio, "create_subprocess_exec", fake_exec)
    res = await d.execute_agy_stream_json("p", policy="deny", plain_agent=True)
    assert res["stdout"] == "ok"
    assert seen["cmd"][1:4] == ("--agent", d.PLAIN_AGENT, "--disable-slash-commands")
    assert seen["env"]["HOME"] == d.DAEMON_HOME
    assert seen["env"]["AGY_DAEMON_TOOL_POLICY"] == "deny"
    assert seen["cwd"].startswith(d.RUNS_DIR) and seen["agent_file"]
    assert not os.path.exists(seen["cwd"])


def test_run_server_starts_live_check(monkeypatch):
    started = []
    monkeypatch.setattr(d, "PORT", find_free_port())
    monkeypatch.setattr(d, "LIVE_CHECK_INTERVAL", 600.0)
    monkeypatch.setattr(d, "live_check_loop", lambda stop, interval: started.append(interval))

    def interrupt(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(d.ThreadingHTTPServer, "serve_forever", interrupt)
    d.run_server()
    for _ in range(50):
        if started:
            break
        time.sleep(0.01)
    assert started == [600.0]
    assert os.path.exists(os.path.join(d.DAEMON_HOME, ".gemini", "config", "hooks.json"))
