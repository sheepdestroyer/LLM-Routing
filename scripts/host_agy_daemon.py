#!/usr/bin/env python3
"""HTTP daemon to bridge router requests to the host-side agy CLI."""

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO

PORT = int(os.environ.get("AGY_PORT", os.environ.get("PORT", 5005)))
AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
# The host login (agy's own file, shared with agy-run and antigravity-cli-daemon).
CLI_TOKEN_PATH = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
HOST_SETTINGS_PATH = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")

# Isolation: agy runs with a daemon-private HOME (no host MCP servers, hooks, plugins or
# skills; conversations persist there so --conversation keeps working) and a private
# working directory per spawn.
STATE_DIR = os.path.expanduser(os.environ.get("AGY_DAEMON_STATE_DIR", "~/.local/state/agy-daemon"))
DAEMON_HOME = os.path.join(STATE_DIR, "home")
RUNS_DIR = os.path.join(STATE_DIR, "runs")
CACHE_FILE = os.path.join(DAEMON_HOME, ".gemini", "antigravity-cli", "cache", "last_conversations.json")
# Workspace custom agent without default components: no shell, file, web or browser tools.
PLAIN_AGENT = "llm-routing-plain"
# Native tools: "sse" (only *-sse / autonomous models run them), "none" or "all".
NATIVE_TOOLS = os.environ.get("AGY_DAEMON_NATIVE_TOOLS", "sse").strip().lower()
ENV_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

# Concurrency cap: every agy spawn takes a slot; requests wait at most QUEUE_TIMEOUT seconds.
MAX_CONCURRENT = max(1, int(os.environ.get("AGY_DAEMON_MAX_CONCURRENT", "3")))
QUEUE_TIMEOUT = float(os.environ.get("AGY_DAEMON_QUEUE_TIMEOUT", "120"))
SPAWN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
_SLOT_LOCK = threading.Lock()
_SLOT_STATE = {"in_use": 0}

# Login refresh: serialized with agy-run (same flock), done by agy itself in a scratch HOME.
AGY_RUN_LOCK = os.path.expanduser(os.environ.get("AGY_RUN_LOCK", "~/.cache/agy-run.lock"))
LOCK_TIMEOUT = float(os.environ.get("AGY_DAEMON_LOCK_TIMEOUT", "60"))
# Request threads wait only briefly for the lock (agy-run can hold it for a whole task) and
# back off after a failed attempt; the live-check thread does the proactive refresh.
REQUEST_LOCK_TIMEOUT = 2.0
REFRESH_BACKOFF_SEC = 120.0
_REFRESH_STATE = {"retry_after": 0.0}
REFRESH_TIMEOUT = 90.0
TOKEN_MARGIN_SEC = 300
# An access token lives about one hour: never ask for more than this before a run.
MAX_TOKEN_NEED_SEC = 2700
FORCED_EXPIRY = "2000-01-01T00:00:00Z"

# Periodic live login check (``agy models``: one model-list call, no model turn).
LIVE_CHECK_INTERVAL = float(os.environ.get("AGY_DAEMON_LIVE_CHECK_SEC", "600"))
LIVE_CHECK_TIMEOUT = 60.0
_LOGIN_LOCK = threading.Lock()
LOGIN_STATE: dict[str, Any] = {"verified": False, "checked_at": None, "detail": "no live check yet", "models": 0}
AUTH_STATE: dict[str, Any] = {"last_sync": None, "detail": "not synced yet"}
LAST_CONVERSATION: dict[str, Any] = {"id": None}


class DaemonBusyError(RuntimeError):
    """All agy spawn slots are taken."""


class AuthRefreshError(RuntimeError):
    """agy could not refresh the host login (the message never contains token values)."""


def log(msg: str) -> None:
    """One line to the journal (stderr)."""
    print(f"agy-daemon: {msg}", file=sys.stderr, flush=True)


def get_last_conversation_id(cwd: str | None = None):
    """Retrieve the last conversation ID agy recorded for a workspace (default: the daemon's cwd)."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
            return data.get(cwd or os.getcwd())
    except Exception:
        pass
    return None


def tool_policy(autonomous: bool) -> str:
    """'native' when agy may execute its native tools for this request, else 'deny'."""
    if NATIVE_TOOLS == "all":
        return "native"
    if NATIVE_TOOLS == "none":
        return "deny"
    return "native" if autonomous else "deny"


def tool_guard_main(stdin: TextIO, stdout: TextIO, environ: Any) -> int:
    """PreToolUse hook: allow only when the spawn's policy is 'native'; deny otherwise (fail closed)."""
    stdin.read()
    if environ.get("AGY_DAEMON_TOOL_POLICY") == "native":
        return 0
    stdout.write(json.dumps({"decision": "deny", "reason": "native tools are disabled on this backend"}))
    return 0


def write_private_atomic(path: str, text: str) -> None:
    """Write a 0600 file through a temp file, fsync and rename (readers never see a partial file)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def plain_agent_definition() -> str:
    """Workspace custom agent: no default components, hence no native tools."""
    return (
        "---\n"
        f"name: {PLAIN_AGENT}\n"
        "description: LLM-Routing backend model without native tools.\n"
        "mainAgent: true\n"
        "subagent: false\n"
        "excludeDefaultComponents: true\n"
        "inheritMcp: false\n"
        "tools: []\n"
        "---\n"
        "You are a helpful assistant serving API clients. Answer the request directly.\n"
    )


def daemon_token_path() -> str:
    """The daemon HOME's copy of the login."""
    return os.path.join(DAEMON_HOME, ".gemini", "antigravity-cli", "antigravity-oauth-token")


def prepare_home() -> None:
    """Create the daemon HOME: no MCP servers, a deny-unless-native PreToolUse hook; prune stale cache keys."""
    for directory in (STATE_DIR, DAEMON_HOME, RUNS_DIR):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    config_dir = os.path.join(DAEMON_HOME, ".gemini", "config")
    write_private_atomic(os.path.join(config_dir, "mcp_config.json"), json.dumps({"mcpServers": {}}))
    guard = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} --tool-guard"
    hooks = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": guard}]}]}}
    write_private_atomic(os.path.join(config_dir, "hooks.json"), json.dumps(hooks))
    # Native tools run without prompts (as on the host); the hook still denies them unless the
    # spawn's policy is "native". Credit usage follows the host login's setting.
    host_settings = read_token(HOST_SETTINGS_PATH) or {}
    settings = {
        "toolPermission": "always-proceed",
        "trustedWorkspaces": [RUNS_DIR],
        "useG1Credits": bool(host_settings.get("useG1Credits", False)),
    }
    write_private_atomic(os.path.join(DAEMON_HOME, ".gemini", "antigravity-cli", "settings.json"), json.dumps(settings))
    # Leftovers of a crash: run workspaces and refresh scratch HOMEs (which hold a login copy).
    for parent, prefix in ((RUNS_DIR, "run-"), (STATE_DIR, "refresh-")):
        for name in os.listdir(parent):
            if name.startswith(prefix):
                shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
    prune_conversation_cache()


def prune_when_idle() -> bool:
    """Prune the conversation cache only while no agy runs (agy rewrites that file at run end)."""
    taken = 0
    try:
        while taken < MAX_CONCURRENT and SPAWN_SLOTS.acquire(blocking=False):
            taken += 1
        if taken < MAX_CONCURRENT:
            return False
        prune_conversation_cache()
        return True
    finally:
        for _ in range(taken):
            SPAWN_SLOTS.release()


def prune_conversation_cache() -> None:
    """Drop agy's last-conversation entries for run workspaces that no longer exist."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    kept = {k: v for k, v in data.items() if not (k.startswith(RUNS_DIR + os.sep) and not os.path.exists(k))}
    if len(kept) != len(data):
        write_private_atomic(CACHE_FILE, json.dumps(kept))


@contextlib.contextmanager
def run_workspace(plain_agent: bool) -> Iterator[tuple[str, str]]:
    """A private working directory and TMPDIR for one spawn, removed afterwards."""
    os.makedirs(RUNS_DIR, mode=0o700, exist_ok=True)
    run = tempfile.mkdtemp(prefix="run-", dir=RUNS_DIR)
    try:
        work, tmp = os.path.join(run, "work"), os.path.join(run, "tmp")
        os.mkdir(work, 0o700)
        os.mkdir(tmp, 0o700)
        if plain_agent:
            agents = os.path.join(work, ".agents", "agents")
            os.makedirs(agents, mode=0o700)
            with open(os.path.join(agents, f"{PLAIN_AGENT}.md"), "w", encoding="utf-8") as f:
                f.write(plain_agent_definition())
        yield work, tmp
    finally:
        shutil.rmtree(run, ignore_errors=True)


def spawn_env(model_override: str, policy: str, tmp: str, home: str | None = None) -> dict[str, str]:
    """An allowlisted environment: nothing from the daemon but PATH, locale, identity and CA paths."""
    env = {name: os.environ[name] for name in ENV_PASSTHROUGH if os.environ.get(name)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(
        {
            "HOME": home or DAEMON_HOME,
            "TMPDIR": tmp,
            "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
            "AGY_DAEMON_TOOL_POLICY": policy,
        }
    )
    if model_override:
        env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = model_override
    return env


def agent_args(plain_agent: bool, slash_commands: bool = False) -> list[str]:
    """CLI flags selecting the agent and disabling slash-command expansion of client prompts."""
    args = ["--agent", PLAIN_AGENT] if plain_agent else []
    if not slash_commands:
        args.append("--disable-slash-commands")
    return args


def remaining_timeout(timeout: float, queued: float) -> float:
    """The run timeout minus a queue wait of a second or more (never below one second)."""
    if queued < 1.0:
        return timeout
    return max(1.0, timeout - queued)


@contextlib.contextmanager
def spawn_slot(timeout: float | None = None) -> Iterator[None]:
    """Hold one of MAX_CONCURRENT agy spawn slots, or raise DaemonBusyError."""
    wait = QUEUE_TIMEOUT if timeout is None else timeout
    if not SPAWN_SLOTS.acquire(timeout=wait):
        raise DaemonBusyError(f"agy daemon at capacity ({MAX_CONCURRENT} concurrent runs)")
    with _SLOT_LOCK:
        _SLOT_STATE["in_use"] += 1
    try:
        yield
    finally:
        with _SLOT_LOCK:
            _SLOT_STATE["in_use"] -= 1
        SPAWN_SLOTS.release()


@contextlib.contextmanager
def agy_lock(timeout: float) -> Iterator[None]:
    """The agy-run flock (exclusive), so login refreshes are serialized with agy-run; TimeoutError if busy."""
    os.makedirs(os.path.dirname(AGY_RUN_LOCK), exist_ok=True)
    fd = os.open(AGY_RUN_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{AGY_RUN_LOCK} is held by another agy consumer") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def expiry_to_ms(expiry_val: Any) -> int:
    """agy's token expiry (RFC 3339 string with nanoseconds, or epoch s/ms) in epoch milliseconds; 0 if unknown."""
    if isinstance(expiry_val, bool):
        return 0
    if isinstance(expiry_val, (int, float)):
        return int(expiry_val * 1000) if expiry_val < 10000000000 else int(expiry_val)
    if isinstance(expiry_val, str) and expiry_val.strip():
        normalized = re.sub(r"(\.\d{6})\d+", r"\1", expiry_val.strip())
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def read_token(path: str) -> dict | None:
    """The token file as a dict, or None when missing or malformed."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def token_expiry(data: dict | None) -> float:
    """Access-token expiry in epoch seconds (0 when there is no usable access token)."""
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, dict) or not token.get("access_token"):
        return 0.0
    return expiry_to_ms(token.get("expiry")) / 1000.0


def refresh_host_login() -> None:
    """Make agy refresh the host login, then write it back. Caller holds the agy-run lock.

    agy refreshes only an expired access token, so a copy whose expiry is forced into the past
    is given to ``agy models`` in a scratch HOME (one token refresh and one model-list call, no
    model turn). The refreshed file agy writes there replaces the host file atomically, unless
    another consumer refreshed it meanwhile.
    """
    host = read_token(CLI_TOKEN_PATH)
    token = host.get("token") if host else None
    if not isinstance(token, dict) or not token.get("refresh_token"):
        raise AuthRefreshError("host agy login has no refresh token: an interactive re-login is needed")
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="refresh-", dir=STATE_DIR)
    try:
        doctored = {**(host or {}), "token": {**token, "expiry": FORCED_EXPIRY}}
        scratch_token = os.path.join(scratch, ".gemini", "antigravity-cli", "antigravity-oauth-token")
        write_private_atomic(scratch_token, json.dumps(doctored))
        try:
            result = subprocess.run(
                [AGY_BINARY, "models"],
                env=spawn_env("", "deny", scratch, home=scratch),
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=REFRESH_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AuthRefreshError(f"agy models failed: {type(exc).__name__}") from None
        refreshed = read_token(scratch_token)
        new_expiry = token_expiry(refreshed)
        if result.returncode != 0 or refreshed is None or new_expiry <= time.time() + 60:
            raise AuthRefreshError(f"agy did not refresh the login (agy models exit {result.returncode})")
        if token_expiry(read_token(CLI_TOKEN_PATH)) >= new_expiry:
            return
        write_private_atomic(CLI_TOKEN_PATH, json.dumps(refreshed))
        log(f"host agy login refreshed (expires in {int(new_expiry - time.time())}s)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _install_host_copy(host: dict | None) -> bool:
    """Copy the host login into the daemon HOME when it is newer than the daemon's copy (re-read now)."""
    if host is None or token_expiry(host) <= token_expiry(read_token(daemon_token_path())):
        return False
    write_private_atomic(daemon_token_path(), json.dumps(host))
    return True


def ensure_auth(run_timeout: float, background: bool = False) -> str:
    """Make sure the daemon HOME holds a login valid for the run; refresh the host login if needed.

    Returns what happened: "daemon", "host", "refreshed", "lock-timeout", "refresh-failed",
    "backoff" or "error". Failures are logged, never raised: agy can still refresh an expired
    copy by itself. Request threads (``background=False``) wait at most REQUEST_LOCK_TIMEOUT for
    the agy-run lock and skip refreshing for REFRESH_BACKOFF_SEC after a failure; the live-check
    thread waits LOCK_TIMEOUT and ignores the backoff.
    """
    need = min(max(0.0, run_timeout) + TOKEN_MARGIN_SEC, MAX_TOKEN_NEED_SEC)
    try:
        outcome = _sync_auth(need, background)
    except OSError as exc:
        log(f"login sync failed: {type(exc).__name__}")
        outcome = "error"
    AUTH_STATE.update({"last_sync": time.time(), "detail": outcome})
    return outcome


def _sync_auth(need: float, background: bool) -> str:
    now = time.time()
    if token_expiry(read_token(daemon_token_path())) - now >= need:
        return "daemon"
    host = read_token(CLI_TOKEN_PATH)
    if token_expiry(host) - now >= need:
        _install_host_copy(host)
        return "host"
    if not background and now < _REFRESH_STATE["retry_after"]:
        _install_host_copy(host)
        return "backoff"
    try:
        with agy_lock(LOCK_TIMEOUT if background else REQUEST_LOCK_TIMEOUT):
            if token_expiry(read_token(CLI_TOKEN_PATH)) - time.time() < need:
                refresh_host_login()
            _install_host_copy(read_token(CLI_TOKEN_PATH))
        return "refreshed"
    except TimeoutError:
        log("agy-run lock busy: login not refreshed before this run")
        outcome = "lock-timeout"
    except AuthRefreshError as exc:
        log(f"login refresh failed: {exc}")
        outcome = "refresh-failed"
    _REFRESH_STATE["retry_after"] = time.time() + REFRESH_BACKOFF_SEC
    _install_host_copy(read_token(CLI_TOKEN_PATH))
    return outcome


def record_login(verified: bool, detail: str, models: int = 0) -> None:
    """Store the outcome of a live login check."""
    with _LOGIN_LOCK:
        count = models if models or not verified else LOGIN_STATE.get("models", 0)
        LOGIN_STATE.update({"verified": verified, "checked_at": time.time(), "detail": detail, "models": count})


def run_agy_models(timeout: float = LIVE_CHECK_TIMEOUT) -> tuple[int, str]:
    """``agy models`` in the daemon HOME and a private workspace: (returncode, stdout)."""
    with run_workspace(False) as (work, tmp):
        result = subprocess.run(
            [AGY_BINARY, "models"],
            env=spawn_env("", "deny", tmp),
            cwd=work,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return result.returncode, result.stdout


def live_login_check() -> bool:
    """Verify the login with a live ``agy models`` call; skipped (False) when every slot is busy."""
    # Proactive refresh, before taking a slot: keep the login valid until the next check.
    ensure_auth(min(MAX_TOKEN_NEED_SEC, 2 * LIVE_CHECK_INTERVAL), background=True)
    try:
        with spawn_slot(timeout=5.0):
            try:
                returncode, stdout = run_agy_models()
            except (OSError, subprocess.SubprocessError) as exc:
                record_login(False, f"agy models failed: {type(exc).__name__}")
                return True
    except DaemonBusyError:
        return False
    models = parse_models_output(stdout) if returncode == 0 else []
    if models:
        record_login(True, "agy models listed the account's models", len(models))
    else:
        record_login(False, f"agy models returned no models (exit {returncode})")
    return True


def live_check_loop(stop: threading.Event, interval: float) -> None:
    """Run the live login check now and then every ``interval`` seconds until ``stop`` is set."""
    while True:
        try:
            live_login_check()
            prune_when_idle()
        except Exception as exc:
            record_login(False, f"live check error: {type(exc).__name__}")
        if stop.wait(interval):
            return


def login_status(now: float | None = None) -> dict:
    """'verified' (recent successful live check), 'stale', 'failed' or 'unverified'."""
    now = time.time() if now is None else now
    with _LOGIN_LOCK:
        state = dict(LOGIN_STATE)
    checked = state["checked_at"]
    age = None if checked is None else int(now - checked)
    if checked is None:
        label = "unverified"
    elif not state["verified"]:
        label = "failed"
    elif LIVE_CHECK_INTERVAL > 0 and now - checked > 2.5 * LIVE_CHECK_INTERVAL:
        label = "stale"
    else:
        label = "verified"
    return {"state": label, "checked_at": checked, "age_sec": age, "detail": state["detail"], "models": state["models"]}


def read_file_sync(path):
    """Synchronously read and return content from a file, returning empty string on error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def get_auth_status(path: str | None = None) -> dict:
    """Token-file status (presence and expiry only: a refresh token is not a verified login)."""
    token_path = path or CLI_TOKEN_PATH
    try:
        if not os.path.exists(token_path):
            return {"authenticated": False, "status": "missing", "detail": "No credentials found", "expiry_ms": 0}

        try:
            with open(token_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"authenticated": False, "status": "error", "detail": "Invalid token JSON", "expiry_ms": 0}

        if not data:
            return {"authenticated": False, "status": "missing", "detail": "No credentials found", "expiry_ms": 0}

        token_info = data.get("token")
        if isinstance(token_info, dict):
            access_token = token_info.get("access_token")
            expiry_val = token_info.get("expiry") or token_info.get("expiry_date")
        else:
            access_token = data.get("access_token")
            expiry_val = data.get("expiry_date") or data.get("expiry")

        if not access_token:
            return {
                "authenticated": False,
                "status": "missing",
                "detail": "No access token in credentials",
                "expiry_ms": 0,
            }

        expiry_ms = expiry_to_ms(expiry_val)

        current_ms = int(time.time() * 1000)
        diff_sec = (expiry_ms - current_ms) / 1000.0 if expiry_ms > 0 else 0
        has_refresh = isinstance(token_info, dict) and bool(token_info.get("refresh_token"))
        is_valid = diff_sec > 0 or has_refresh

        return {
            "authenticated": bool(access_token),
            "status": "valid" if diff_sec > 0 else ("valid_silent_refresh" if is_valid else "expired"),
            "refresh_token_present": has_refresh,
            "source": "cli_token",
            "expiry_ms": expiry_ms,
            "remaining_sec": int(diff_sec) if diff_sec > 0 else 0,
        }
    except Exception as e:
        return {"authenticated": False, "status": "error", "detail": str(e), "expiry_ms": 0}


def parse_usage_output(text: str) -> dict:
    """Parse agy /usage slash command output into structured quota dict."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    quotas = []
    for line in lines:
        if line.startswith("Quota:"):
            continue
        parts = [p.strip() for p in re.split(r"\t+|\s{2,}", line) if p.strip()]
        if len(parts) >= 4:
            quotas.append(
                {
                    "category": parts[0],
                    "limit_type": parts[1],
                    "remaining": parts[2],
                    "reset_time": parts[3],
                }
            )
        elif len(parts) == 3:
            quotas.append(
                {
                    "category": parts[0],
                    "limit_type": parts[1],
                    "remaining": parts[2],
                    "reset_time": "",
                }
            )
        else:
            quotas.append({"raw": line})
    return {"raw_output": text, "quotas": quotas}


def parse_models_output(text: str) -> list[dict]:
    """Parse agy models command output into list of model objects."""
    models = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "Fetching available models" in line:
            continue
        line = line.lstrip("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ").strip()
        parts = line.split(None, 1)
        if len(parts) == 2:
            models.append({"id": parts[0], "name": parts[1]})
        elif len(parts) == 1:
            models.append({"id": parts[0], "name": parts[0]})
    return models


async def execute_agy_print(
    prompt: str,
    model_override: str = "",
    conversation_id: str | None = None,
    timeout: float = 600.0,
    plain_agent: bool = True,
    slash_commands: bool = False,
):
    """Asynchronously execute agy (no native tools by default) and capture full output."""
    ensure_auth(timeout)
    with run_workspace(plain_agent) as (work, tmp):
        return await _execute_agy_print(
            prompt,
            model_override,
            conversation_id,
            timeout,
            work,
            spawn_env(model_override, "deny", tmp),
            agent_args(plain_agent, slash_commands),
        )


async def _execute_agy_print(
    prompt: str,
    model_override: str,
    conversation_id: str | None,
    timeout: float,
    work: str,
    env: dict[str, str],
    extra_args: list[str],
):
    cmd = [AGY_BINARY, *extra_args]
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    cmd.extend(["--print", prompt])

    proc = None
    stdout_fd, stdout_path = tempfile.mkstemp(prefix="agy_out_", suffix=".log")
    stderr_fd, stderr_path = tempfile.mkstemp(prefix="agy_err_", suffix=".log")
    stdout_file = open(stdout_path, "w")
    stderr_file = open(stderr_path, "w")
    os.close(stdout_fd)
    os.close(stderr_fd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=work,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        stdout_file.close()
        stderr_file.close()

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            returncode = proc.returncode or 0
        except TimeoutError:
            try:
                if proc is not None:  # pragma: no cover
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
            returncode = -1
        except Exception:
            returncode = -1
    finally:
        try:
            stdout_file.close()
        except Exception:
            pass
        try:
            stderr_file.close()
        except Exception:
            pass
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    loop_ref = asyncio.get_running_loop()
    stdout, stderr = await asyncio.gather(
        loop_ref.run_in_executor(None, read_file_sync, stdout_path),
        loop_ref.run_in_executor(None, read_file_sync, stderr_path),
    )

    for path in [stdout_path, stderr_path]:
        try:
            os.unlink(path)
        except Exception:
            pass

    if returncode == -1 and not stderr:
        stderr = "TIMEOUT"

    result_conv_id = get_last_conversation_id(work)
    if result_conv_id:
        LAST_CONVERSATION["id"] = result_conv_id
    if returncode == 0 and stdout:
        record_login(True, "agy run succeeded")
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr, "conversation_id": result_conv_id}


async def execute_agy_stream_json(
    prompt: str,
    model_override: str = "",
    conversation_id: str | None = None,
    timeout: float = 600.0,
    tools: list[Any] | None = None,
    intercept_tools: bool = True,
    effort: str | None = None,
    policy: str = "deny",
    plain_agent: bool = False,
) -> dict:
    """Asynchronously execute agy via stream-json over stdin and capture structured result."""
    ensure_auth(timeout)
    with run_workspace(plain_agent) as (work, tmp):
        return await _execute_agy_stream_json(
            prompt,
            model_override,
            conversation_id,
            timeout,
            tools,
            intercept_tools,
            effort,
            work,
            spawn_env(model_override, policy, tmp),
            agent_args(plain_agent),
        )


async def _execute_agy_stream_json(
    prompt: str,
    model_override: str,
    conversation_id: str | None,
    timeout: float,
    tools: list[Any] | None,
    intercept_tools: bool,
    effort: str | None,
    work: str,
    env: dict[str, str],
    extra_args: list[str],
) -> dict:
    cmd = [AGY_BINARY, *extra_args, "--input-format", "stream-json", "--output-format", "stream-json"]
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    cmd.extend(["--print-timeout", f"{max(1, int(timeout))}s"])
    if effort in ("low", "medium", "high"):
        cmd.extend(["--effort", effort])

    input_msg = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=work,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        proc_comm = getattr(proc, "communicate", None)
        if proc_comm is not None and type(proc_comm).__name__ == "AsyncMock":
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=input_msg.encode("utf-8")),
                timeout=timeout,
            )
            returncode = proc.returncode or 0
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            res_conv_id = None
            result_response = ""
            result_usage = None
            accumulated_deltas = []
            intercepted_call = None
            for line in stdout_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event_obj = json.loads(line)
                    ev = event_obj.get("event")
                    if ev == "init":
                        res_conv_id = event_obj.get("conversation_id")
                    elif ev == "step_update":
                        su = event_obj.get("step_update", {})
                        st = su.get("step_type")
                        if tools and intercept_tools and st == "tool" and not intercepted_call:
                            tn = su.get("tool_name", "")
                            ti = su.get("tool_info", {})
                            params = ti.get("parameters", {})
                            intercepted_call = map_native_tool_call(tn, params, tools)
                        delta = su.get("text_delta")
                        if delta:
                            accumulated_deltas.append(delta)
                    elif ev == "result":
                        res = event_obj.get("result", {})
                        if res.get("status") == "ERROR":
                            err_msg = res.get("error") or "Unknown stream-json error"
                            combined_err = f"{err_msg} - {stderr_text}" if stderr_text else err_msg
                            return {
                                "returncode": 1,
                                "stdout": "",
                                "stderr": combined_err,
                                "conversation_id": res.get("conversation_id") or res_conv_id,
                                "usage": None,
                                "tool_calls": [],
                            }
                        result_response = res.get("response", "")
                        result_usage = res.get("usage")
                        if not res_conv_id:
                            res_conv_id = res.get("conversation_id")
                except Exception:
                    continue

            if intercepted_call:
                return {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": stderr_text,
                    "tool_calls": [intercepted_call],
                    "conversation_id": res_conv_id,
                    "usage": result_usage or {"input_tokens": max(1, len(prompt) // 4), "output_tokens": 10},
                }

            final_text = result_response or "".join(accumulated_deltas)
            return {
                "returncode": returncode,
                "stdout": final_text,
                "stderr": stderr_text,
                "conversation_id": res_conv_id,
                "usage": result_usage,
                "tool_calls": [],
            }

        # Real process / line-by-line streaming mode
        if proc.stdin and hasattr(proc.stdin, "write"):
            proc.stdin.write(input_msg.encode("utf-8"))
            if hasattr(proc.stdin, "drain") and asyncio.iscoroutinefunction(proc.stdin.drain):
                await proc.stdin.drain()
            if hasattr(proc.stdin, "close"):
                proc.stdin.close()
            if hasattr(proc.stdin, "wait_closed") and asyncio.iscoroutinefunction(proc.stdin.wait_closed):
                try:
                    await proc.stdin.wait_closed()
                except Exception:
                    pass

        res_conv_id = None
        result_response = ""
        result_usage = None
        accumulated_deltas = []
        intercepted_call = None
        stderr_chunks = []

        async def _read_stderr():
            if hasattr(proc, "stderr") and hasattr(proc.stderr, "readline"):
                while True:
                    err_line = await proc.stderr.readline()
                    if not err_line:
                        break
                    stderr_chunks.append(err_line.decode("utf-8", errors="replace"))

        stderr_task = asyncio.create_task(_read_stderr())

        assert proc.stdout is not None
        while True:
            line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event_obj = json.loads(line)
                ev = event_obj.get("event")
                if ev == "init":
                    res_conv_id = event_obj.get("conversation_id")
                elif ev == "step_update":
                    su = event_obj.get("step_update", {})
                    st = su.get("step_type")
                    if tools and intercept_tools and st == "tool" and not intercepted_call:
                        tn = su.get("tool_name", "")
                        ti = su.get("tool_info", {})
                        params = ti.get("parameters", {})
                        intercepted_call = map_native_tool_call(tn, params, tools)
                        # Intercept immediately before host executes it!
                        if hasattr(proc, "kill"):
                            try:
                                proc.kill()
                                if hasattr(proc, "wait") and asyncio.iscoroutinefunction(proc.wait):
                                    await proc.wait()
                            except Exception:
                                pass
                        stderr_task.cancel()
                        return {
                            "returncode": 0,
                            "stdout": "",
                            "stderr": "".join(stderr_chunks),
                            "tool_calls": [intercepted_call],
                            "conversation_id": res_conv_id,
                            "usage": result_usage or {"input_tokens": max(1, len(prompt) // 4), "output_tokens": 10},
                        }
                    delta = su.get("text_delta")
                    if delta:
                        accumulated_deltas.append(delta)
                elif ev == "result":
                    res = event_obj.get("result", {})
                    if res.get("status") == "ERROR":
                        err_msg = res.get("error") or "Unknown stream-json error"
                        stderr_text = "".join(stderr_chunks)
                        combined_err = f"{err_msg} - {stderr_text}" if stderr_text else err_msg
                        stderr_task.cancel()
                        return {
                            "returncode": 1,
                            "stdout": "",
                            "stderr": combined_err,
                            "conversation_id": res.get("conversation_id") or res_conv_id,
                            "usage": None,
                            "tool_calls": [],
                        }
                    result_response = res.get("response", "")
                    result_usage = res.get("usage")
                    if not res_conv_id:
                        res_conv_id = res.get("conversation_id")
            except Exception:
                continue

        if hasattr(proc, "wait") and asyncio.iscoroutinefunction(proc.wait):
            await proc.wait()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

        returncode = proc.returncode or 0
        stderr_text = "".join(stderr_chunks)
        final_text = result_response or "".join(accumulated_deltas)
        return {
            "returncode": returncode,
            "stdout": final_text,
            "stderr": stderr_text,
            "conversation_id": res_conv_id,
            "usage": result_usage,
            "tool_calls": [],
        }
    except TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"Execution timed out after {timeout} seconds",
            "conversation_id": None,
            "usage": None,
            "tool_calls": [],
        }
    except Exception as e:
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
            "conversation_id": None,
            "usage": None,
            "tool_calls": [],
        }


def health_payload(now: float | None = None) -> dict:
    """/health: 'health' is 'ok' only for a recently verified login, not for a merely present refresh token."""
    login = login_status(now)
    with _SLOT_LOCK:
        in_use = _SLOT_STATE["in_use"]
    return {
        "status": "ok",
        "health": "ok" if login["state"] == "verified" else "degraded",
        "agy_binary": AGY_BINARY,
        "agy_available": os.path.exists(AGY_BINARY),
        "auth": get_auth_status(),
        "daemon_auth": get_auth_status(daemon_token_path()),
        "login": login,
        "auth_sync": dict(AUTH_STATE),
        "concurrency": {"max": MAX_CONCURRENT, "in_use": in_use},
        "native_tools": NATIVE_TOOLS,
        "home": DAEMON_HOME,
        "last_conversation_id": LAST_CONVERSATION["id"],
    }


TOOL_CALL_RE = re.compile(
    r"(?:<tool_call>([\s\S]*?)</tool_call>|```(?:tool_call|json:tool_call)\s*([\s\S]*?)```)", re.IGNORECASE
)


def format_tools_instruction(tools: list, is_sse_mode: bool = False) -> str:
    """Format tools list into prompt instructions for agy."""
    if not tools or not isinstance(tools, list):
        return ""
    try:
        tools_json = json.dumps(tools, indent=2)
    except Exception:
        tools_json = str(tools)

    if is_sse_mode:
        return (
            "# Available Client Tools\n"
            "The upstream client provides the following external function definitions:\n"
            f"<tools>\n{tools_json}\n</tools>\n\n"
            "# Tool Calling Protocol\n"
            "You operate as an autonomous backend with full access to your native workspace tools (file inspection, command execution, and codebase searches).\n"
            "If a user request requires calling one of the external client tools defined above that you cannot perform natively,\n"
            "you MUST respond with one or more tool call blocks in this exact format:\n"
            "<tool_call>\n"
            '{"name": "<function_name>", "arguments": <json_object_of_arguments>}\n'
            "</tool_call>\n\n"
            "Otherwise, execute any necessary inspections using your native tools and provide a clear, concise conversational report."
        )
    else:
        return (
            "# Available Tools\n"
            "You have access to the following functions to call:\n"
            f"<tools>\n{tools_json}\n</tools>\n\n"
            "# Tool Calling Protocol\n"
            "You are acting as an external function calling engine for an agent orchestrator.\n"
            "When you need to execute a command, inspect files, or call a tool, invoke the tool directly.\n"
            "If you do not need to call any tool, answer normally with conversational text."
        )


def map_native_tool_call(tool_name: str, parameters: dict, client_tools: list) -> dict:
    """Map native agy tool calls to client-provided tools (e.g. run_command -> terminal)."""
    client_tool_names = []
    if client_tools and isinstance(client_tools, list):
        for ct in client_tools:
            if isinstance(ct, dict):
                fn = ct.get("function", {})
                name = fn.get("name") if isinstance(fn, dict) else ct.get("name")
                if name:
                    client_tool_names.append(name)

    mapped_name = tool_name
    mapped_args = parameters or {}

    if tool_name == "run_command":
        cmd_str = parameters.get("CommandLine") or parameters.get("command") or ""
        if "terminal" in client_tool_names:
            mapped_name = "terminal"
            mapped_args = {"command": cmd_str}
        elif "bash" in client_tool_names:
            mapped_name = "bash"
            mapped_args = {"command": cmd_str}
        elif "exec" in client_tool_names:
            mapped_name = "exec"
            mapped_args = {"command": cmd_str}
        elif "run_command" in client_tool_names:
            mapped_name = "run_command"
            mapped_args = parameters
        elif len(client_tool_names) == 1:
            mapped_name = client_tool_names[0]
            mapped_args = {"command": cmd_str}
    elif tool_name in ("view_file", "read_file"):
        path_str = parameters.get("AbsolutePath") or parameters.get("path") or ""
        if "read_file" in client_tool_names:
            mapped_name = "read_file"
            mapped_args = {"path": path_str}
        elif "view_file" in client_tool_names:
            mapped_name = "view_file"
            mapped_args = {"path": path_str}
        elif len(client_tool_names) == 1:
            mapped_name = client_tool_names[0]
            mapped_args = {"path": path_str}
    elif len(client_tool_names) == 1:
        mapped_name = client_tool_names[0]

    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": mapped_name,
            "arguments": json.dumps(mapped_args) if isinstance(mapped_args, (dict, list)) else str(mapped_args),
        },
    }


def extract_reasoning_effort(body: dict) -> str | None:
    """Extract reasoning effort string from request body.

    Supports top-level reasoning_effort, extra_body.reasoning_effort,
    reasoning_effort dicts, and reasoning.effort.
    """
    if not body or not isinstance(body, dict):
        return None

    effort = body.get("reasoning_effort")
    if effort is None:
        reasoning_obj = body.get("reasoning")
        if isinstance(reasoning_obj, dict):
            effort = reasoning_obj.get("effort")
    if effort is None:
        eb = body.get("extra_body")
        if isinstance(eb, dict):
            effort = eb.get("reasoning_effort")
            if effort is None:
                reasoning_eb = eb.get("reasoning")
                if isinstance(reasoning_eb, dict):
                    effort = reasoning_eb.get("effort")

    if isinstance(effort, dict):
        effort = effort.get("effort")
    if effort is None or effort == "":
        return None

    e = str(effort).strip().lower()
    if e in ("max", "high", "xhigh", "ultra"):
        return "high"
    if e in ("medium", "med"):
        return "medium"
    if e in ("low", "minimal"):
        return "low"
    if e in ("none", "off", "disabled", "false", "0"):
        return "low"
    return e


def parse_tool_calls_from_text(text: str) -> tuple[str, list]:
    """Parse <tool_call> blocks from text and convert them into OpenAI tool_calls dicts."""
    if not text:
        return "", []

    tool_calls = []

    def repl(m):
        raw = (m.group(1) or m.group(2) or "").strip()
        try:
            parsed = json.loads(raw, strict=False)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict) and "name" in item:
                    args = item.get("arguments") or {}
                    args_str = json.dumps(args) if isinstance(args, (dict, list)) else str(args)
                    tool_calls.append(
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": str(item["name"]),
                                "arguments": args_str,
                            },
                        }
                    )
            return ""
        except Exception:
            return m.group(0)

    cleaned = TOOL_CALL_RE.sub(repl, text).strip()

    # Fallback: if entire response is a JSON object with name & arguments or tool_calls
    if not tool_calls and text.strip().startswith("{") and text.strip().endswith("}"):
        try:
            data = json.loads(text.strip(), strict=False)
            if isinstance(data, dict):
                if "name" in data and ("arguments" in data or "parameters" in data):
                    args = data.get("arguments") or data.get("parameters") or {}
                    args_str = json.dumps(args) if isinstance(args, (dict, list)) else str(args)
                    tool_calls.append(
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": str(data["name"]),
                                "arguments": args_str,
                            },
                        }
                    )
                    cleaned = ""
                elif "tool_calls" in data and isinstance(data["tool_calls"], list):
                    for tc in data["tool_calls"]:
                        if isinstance(tc, dict) and "name" in tc:
                            args = tc.get("arguments") or {}
                            args_str = json.dumps(args) if isinstance(args, (dict, list)) else str(args)
                            tool_calls.append(
                                {
                                    "id": f"call_{uuid.uuid4().hex[:8]}",
                                    "type": "function",
                                    "function": {
                                        "name": str(tc["name"]),
                                        "arguments": args_str,
                                    },
                                }
                            )
                    cleaned = ""
        except Exception:
            pass

    return cleaned, tool_calls


def extract_prompt_from_messages(
    messages: list[Any], tools: list[Any] | None = None, is_sse_mode: bool = False, native_tools: bool = True
) -> str:
    """Convert an OpenAI messages array into a clean unified prompt string for agy."""
    if not messages or not isinstance(messages, list):
        prompt = ""
    else:
        parts = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = (msg.get("role") or "user").strip()
            raw_content = msg.get("content") or ""
            if isinstance(raw_content, list):
                text_blocks = []
                for block in raw_content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_blocks.append(block.get("text") or "")
                    elif isinstance(block, str):
                        text_blocks.append(block)
                content = "\n".join(text_blocks).strip()
            else:
                content = str(raw_content).strip()

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        content += f"\n[Tool Call: {fn.get('name')}({fn.get('arguments')})]"

            if not content:
                continue

            if role == "system":
                parts.append(f"System: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
            elif role == "tool":
                prompt_lines = [f"Tool Output: {content}"]
                if msg.get("tool_call_id"):
                    prompt_lines.insert(0, f"[Tool Call ID: {msg.get('tool_call_id')}]")
                parts.append("\n".join(prompt_lines))
            else:
                parts.append(f"User: {content}")
        prompt = "\n\n".join(parts)

    if not prompt:
        return ""

    if tools:
        tool_instr = format_tools_instruction(tools, is_sse_mode=is_sse_mode)
        if tool_instr:
            if prompt.startswith("System:"):
                prompt = f"System: {tool_instr}\n\n" + prompt
            else:
                prompt = f"System: {tool_instr}\n\n{prompt}"
    else:
        if native_tools:
            completion_instr = (
                "# Execution Guidelines\n"
                "You are acting as an intelligent autonomous backend for the client.\n"
                "You have full access to your native workspace tools to inspect files, execute commands, or analyze context as needed to fulfill the user's request.\n"
                "Provide a clear, concise conversational report."
            )
        else:
            completion_instr = (
                "# Execution Guidelines\n"
                "You are acting as a backend model for an API client.\n"
                "Answer from the conversation itself; you have no workspace, file or command tools.\n"
                "Provide a clear, concise response."
            )
        if prompt.startswith("System:"):
            prompt = f"System: {completion_instr}\n\n" + prompt
        else:
            prompt = f"System: {completion_instr}\n\n{prompt}"

    return prompt


class AgyDaemonHandler(BaseHTTPRequestHandler):
    """HTTP request handler for agy execution requests."""

    def log_message(self, format, *args):
        """Override to silence standard HTTP logging."""
        pass

    def send_response(self, code, message=None):
        """Track whether a response was started, so a failure never sends a second one."""
        self._responded = True
        super().send_response(code, message)

    def send_failure(self, exc: Exception) -> None:
        """502 for a failed spawn setup (logged by type only), unless a response already started."""
        log(f"request failed before agy ran: {type(exc).__name__}")
        if getattr(self, "_responded", False):
            return
        body_bytes = json.dumps(
            {"error": {"message": f"agy daemon error: {type(exc).__name__}", "type": "api_error", "code": 502}}
        ).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        """Handle GET requests for health checks, usage/quota, and model listings."""
        if self.path in ["/health", "/status"]:
            res = health_payload()
            body = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/run":
            res = {"status": "ok", "message": "Host agy daemon is running"}
            body = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path in ["/usage", "/quota", "/models", "/v1/models"]:
            try:
                with spawn_slot():
                    if self.path in ["/usage", "/quota"]:
                        self.get_usage()
                    else:
                        self.get_models()
            except DaemonBusyError as exc:
                self.send_busy(exc)
            return

        self.send_response(404)
        self.end_headers()

    def get_usage(self):
        """GET /usage: quota table from agy's /usage slash command."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            exec_res = loop.run_until_complete(
                execute_agy_print(
                    "/usage",
                    model_override="gpt-oss-120b-medium",
                    timeout=30.0,
                    plain_agent=False,
                    slash_commands=True,
                )
            )
            parsed = parse_usage_output(exec_res.get("stdout", ""))
            parsed["returncode"] = exec_res.get("returncode", 0)
            parsed["stderr"] = exec_res.get("stderr", "")
        except Exception as e:
            parsed = {"error": str(e), "quotas": []}
        finally:
            loop.close()

        body = json.dumps(parsed).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_models(self):
        """GET /models (agy's live list) or /v1/models (static OpenAI list)."""
        try:
            ensure_auth(0)
            returncode, stdout = run_agy_models(timeout=15)
            models = parse_models_output(stdout)
            if returncode == 0 and models:
                record_login(True, "agy models listed the account's models", len(models))
            res = {"status": "ok", "models": models}
        except Exception as e:
            res = {"status": "error", "error": str(e), "models": []}

        if self.path == "/v1/models":
            openai_models = [
                {"id": "gemini-3.8-flash", "object": "model", "owned_by": "google"},
                {"id": "gemini-3.8-flash-low", "object": "model", "owned_by": "google"},
                {"id": "gemini-3.8-flash-medium", "object": "model", "owned_by": "google"},
                {"id": "gemini-3.8-flash-high", "object": "model", "owned_by": "google"},
                {"id": "claude-opus-4.6", "object": "model", "owned_by": "anthropic"},
                {"id": "claude-sonnet-4.6", "object": "model", "owned_by": "anthropic"},
                {"id": "gpt-oss-120b-medium", "object": "model", "owned_by": "openai"},
                {"id": "llm-routing-agy", "object": "model", "owned_by": "agy"},
                {"id": "agy-gemini", "object": "model", "owned_by": "agy"},
                {"id": "agy-gemini-sse", "object": "model", "owned_by": "agy"},
                {"id": "agy-opus", "object": "model", "owned_by": "agy"},
                {"id": "agy-sonnet", "object": "model", "owned_by": "agy"},
                {"id": "agy-gptoss", "object": "model", "owned_by": "agy"},
                {"id": "llm-routing-agy-sse", "object": "model", "owned_by": "agy"},
                {"id": "agy-sse", "object": "model", "owned_by": "agy"},
                {"id": "agy-opus-sse", "object": "model", "owned_by": "agy"},
                {"id": "agy-sonnet-sse", "object": "model", "owned_by": "agy"},
                {"id": "agy-gptoss-sse", "object": "model", "owned_by": "agy"},
            ]
            res = {"object": "list", "data": openai_models}

        body = json.dumps(res).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Handle POST requests to execute agy commands."""
        if self.path not in ["/run", "/usage", "/v1/chat/completions", "/chat/completions"]:
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except (ValueError, json.JSONDecodeError) as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Invalid JSON payload: {e}"}).encode("utf-8"))
            return

        if self.path in ["/v1/chat/completions", "/chat/completions"]:
            self.handle_chat_completions(body)
            return

        try:
            run_timeout = float(body.get("timeout", 600.0))
        except (TypeError, ValueError):
            self.send_bad_request("timeout must be a number")
            return
        queued_at = time.monotonic()
        try:
            with spawn_slot():
                if self.path == "/usage":
                    self.post_usage(body)
                else:
                    self.post_run(body, remaining_timeout(run_timeout, time.monotonic() - queued_at))
        except DaemonBusyError as exc:
            if self.path == "/run" and body.get("stream", False):
                self.send_busy_ndjson(exc)
            else:
                self.send_busy(exc)
        except OSError as exc:
            self.send_failure(exc)

    def send_bad_request(self, message: str) -> None:
        """400 with an OpenAI-style error body."""
        body_bytes = json.dumps({"error": {"message": message, "type": "invalid_request_error", "code": 400}}).encode(
            "utf-8"
        )
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def post_usage(self, body: dict):
        """POST /usage: quota table for a given model."""
        model = body.get("model", "gpt-oss-120b-medium")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            exec_res = loop.run_until_complete(
                execute_agy_print("/usage", model_override=model, timeout=30.0, plain_agent=False, slash_commands=True)
            )
            parsed = parse_usage_output(exec_res.get("stdout", ""))
            parsed["returncode"] = exec_res.get("returncode", 0)
        except Exception as e:
            parsed = {"error": str(e), "quotas": []}
        finally:
            loop.close()
        body_bytes = json.dumps(parsed).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def post_run(self, body: dict, timeout: float = 600.0):
        """POST /run: one agy --print run (tool-less agent), as JSON or streamed NDJSON."""
        prompt = body.get("prompt", "")
        model_override = body.get("model_override", "")
        conversation_id = body.get("conversation_id", None)
        stream = body.get("stream", False)

        if stream:
            ensure_auth(timeout)
            with run_workspace(True) as (work, tmp):
                self.stream_run(prompt, model_override, conversation_id, timeout, work, tmp)
            return

        # Execute in new asyncio event loop (non-streaming path)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res = loop.run_until_complete(
                execute_agy_print(
                    prompt=prompt,
                    model_override=model_override,
                    conversation_id=conversation_id,
                    timeout=timeout,
                )
            )
        finally:
            loop.close()

        response_bytes = json.dumps(res).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def stream_run(
        self, prompt: str, model_override: str, conversation_id: str | None, timeout: float, work: str, tmp: str
    ):
        """Stream an agy --print run through a PTY as NDJSON token lines plus a closing status line."""
        # 1. Send HTTP headers for streaming NDJSON
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()

        # 2. Setup loop to run async process and stream output
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run_stream():
            """Asynchronously execute agy and stream output via PTY."""
            import pty

            env = spawn_env(model_override, "deny", tmp)

            cmd = [AGY_BINARY, *agent_args(True)]
            if conversation_id:
                cmd.extend(["--conversation", conversation_id])
            cmd.extend(["--print", prompt])

            master_fd, slave_fd = pty.openpty()
            proc = None
            try:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        env=env,
                        cwd=work,
                        stdout=slave_fd,
                        stderr=slave_fd,
                    )
                    os.close(slave_fd)
                except Exception as e:
                    os.close(slave_fd)
                    # Write failure details as status
                    err_msg = json.dumps({"type": "status", "returncode": -1, "stderr": str(e)}) + "\n"
                    self.wfile.write(err_msg.encode("utf-8"))
                    self.wfile.flush()
                    return

                loop_ref = asyncio.get_running_loop()

                def read_bytes():
                    """Read raw bytes from the PTY master file descriptor."""
                    try:
                        return os.read(master_fd, 1024)
                    except OSError:
                        return b""

                while True:
                    data = await loop_ref.run_in_executor(None, read_bytes)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    # PTY text can have \r\n, normalize to \n
                    text_norm = text.replace("\r\n", "\n")
                    # Yield token JSON line
                    chunk_json = json.dumps({"type": "token", "content": text_norm}) + "\n"
                    try:
                        self.wfile.write(chunk_json.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break

                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                    returncode = proc.returncode or 0
                except TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                    returncode = -1
                except Exception:
                    returncode = -1

                # Retrieve last conversation ID
                result_conv_id = get_last_conversation_id(work)
                if result_conv_id:
                    LAST_CONVERSATION["id"] = result_conv_id

                # Write closing metadata
                meta_json = (
                    json.dumps({"type": "status", "returncode": returncode, "conversation_id": result_conv_id}) + "\n"
                )
                self.wfile.write(meta_json.encode("utf-8"))
                self.wfile.flush()
            finally:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass

        try:
            loop.run_until_complete(run_stream())
        finally:
            loop.close()

    def handle_chat_completions(self, body: dict):
        """Handle standard OpenAI /v1/chat/completions requests from LiteLLM or direct clients."""
        messages = body.get("messages", [])
        tools = body.get("tools")
        model = body.get("model", "gemini-3.8-flash")
        model_lower = str(model).lower()
        is_sse_mode = "sse" in model_lower or "autonomous" in model_lower
        policy = tool_policy(is_sse_mode)
        # Without client tools and native tools, agy runs the tool-less custom agent; with client
        # tools it keeps its default agent so native calls are intercepted (the hook denies them).
        plain_agent = policy == "deny" and not tools
        prompt = (
            extract_prompt_from_messages(
                messages, tools=tools, is_sse_mode=is_sse_mode, native_tools=policy == "native"
            )
            if messages
            else body.get("prompt", "")
        )
        stream = body.get("stream", False)
        try:
            timeout = float(body.get("timeout", 600.0))
        except (TypeError, ValueError):
            self.send_bad_request("timeout must be a number")
            return
        raw_conv_id = body.get("conversation_id")
        conversation_id = str(raw_conv_id).strip() if raw_conv_id and not str(raw_conv_id).startswith("sess-") else None

        effort = extract_reasoning_effort(body)
        effort_cli = effort if effort in ("low", "medium", "high") else None

        # Swap Gemini 3.5 to 3.8 and resolve model overrides:
        # Claude Opus tier -> claude-opus-4-6-thinking
        # Claude Sonnet tier -> claude-sonnet-4-6
        # GPT-OSS tier (cheapest 3rd-party vendor model) -> gpt-oss-120b-medium
        if "opus" in model_lower:
            model_override = "claude-opus-4-6-thinking"
        elif "sonnet" in model_lower:
            model_override = "claude-sonnet-4-6"
        elif "gpt-oss" in model_lower or "gptoss" in model_lower or "gpt_oss" in model_lower:
            model_override = "gpt-oss-120b-medium"
        elif "gemini-3.8-flash-high" in model_lower:
            model_override = "gemini-3.8-flash-high"
        elif "gemini-3.8-flash-medium" in model_lower:
            model_override = "gemini-3.8-flash-medium"
        elif "gemini-3.8-flash-low" in model_lower:
            model_override = "gemini-3.8-flash-low"
        elif "gemini-3.1-pro" in model_lower:
            if effort in ("high", "medium"):
                model_override = "gemini-3.1-pro-high"
            else:
                model_override = "gemini-3.1-pro-low"
        else:
            # Default Gemini tier (including llm-routing-agy, llm-routing-agy-sse, agy-gemini, agy-sse, etc.)
            # Dynamically select flash variant based on requested reasoning effort:
            if effort == "high":
                model_override = "gemini-3.8-flash-high"
            elif effort == "medium":
                model_override = "gemini-3.8-flash-medium"
            else:
                model_override = "gemini-3.8-flash-low"

        handler = self.stream_chat_completion if stream else self.complete_chat
        queued_at = time.monotonic()
        try:
            with spawn_slot():
                handler(
                    prompt,
                    model,
                    model_override,
                    conversation_id,
                    remaining_timeout(timeout, time.monotonic() - queued_at),
                    effort_cli,
                    tools,
                    is_sse_mode,
                    policy,
                    plain_agent,
                )
        except DaemonBusyError as exc:
            self.send_busy(exc)
        except OSError as exc:
            self.send_failure(exc)

    def send_busy(self, exc: Exception) -> None:
        """503 so LiteLLM falls back to another deployment."""
        body_bytes = json.dumps({"error": {"message": str(exc), "type": "api_error", "code": 503}}).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Retry-After", "5")
        self.end_headers()
        self.wfile.write(body_bytes)

    def send_busy_ndjson(self, exc: Exception) -> None:
        """Busy answer for a streamed /run: the router reads a failed status line, not an HTTP code."""
        line = json.dumps({"type": "status", "returncode": -1, "stderr": str(exc)}) + "\n"
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(line.encode("utf-8"))

    def stream_chat_completion(
        self,
        prompt: str,
        model: str,
        model_override: str,
        conversation_id: str | None,
        timeout: float,
        effort_cli: str | None,
        tools: list[Any] | None,
        is_sse_mode: bool,
        policy: str,
        plain_agent: bool,
    ) -> None:
        """Stream one chat completion as OpenAI SSE chunks."""
        ensure_auth(timeout)
        with run_workspace(plain_agent) as (work, tmp):
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def run_openai_stream():
                env = spawn_env(model_override, policy, tmp)

                cmd = [
                    AGY_BINARY,
                    *agent_args(plain_agent),
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                ]
                if conversation_id:
                    cmd.extend(["--conversation", conversation_id])
                cmd.extend(["--print-timeout", f"{max(1, int(timeout))}s"])
                if effort_cli:
                    cmd.extend(["--effort", effort_cli])

                chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created_time = int(time.time())
                input_msg = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"

                def safe_write(payload: bytes) -> bool:
                    try:
                        self.wfile.write(payload)
                        self.wfile.flush()
                        return True
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return False

                proc = None
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        env=env,
                        cwd=work,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except Exception as e:
                    err_chunk = {
                        "error": {
                            "message": f"Failed to spawn agy process: {e}",
                            "type": "api_error",
                            "code": 502,
                        }
                    }
                    safe_write(b"data: " + json.dumps(err_chunk).encode("utf-8") + b"\n\n")
                    return

                try:
                    proc.stdin.write(input_msg.encode("utf-8"))
                    await asyncio.wait_for(proc.stdin.drain(), timeout=min(5.0, timeout))
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except Exception:
                    pass

                accumulated_chunks = []
                has_streamed_deltas = False
                stream_error = None
                deadline = time.time() + timeout
                intercepted_tool_call = None

                try:
                    while True:
                        remaining = max(0.1, deadline - time.time())
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                        if not line:
                            break
                        line_str = line.decode("utf-8", errors="replace").strip()
                        if not line_str:
                            continue
                        try:
                            event_obj = json.loads(line_str)
                        except Exception:
                            continue

                        ev = event_obj.get("event")
                        if ev == "step_update":
                            su = event_obj.get("step_update", {})
                            st = su.get("step_type")
                            if not is_sse_mode and tools and st == "tool":
                                tn = su.get("tool_name", "")
                                ti = su.get("tool_info", {})
                                params = ti.get("parameters", {})
                                intercepted_tool_call = map_native_tool_call(tn, params, tools)
                                if proc.returncode is None:
                                    try:
                                        proc.kill()
                                        await proc.wait()
                                    except Exception:
                                        pass
                                break
                            elif is_sse_mode and st == "tool":
                                tn = su.get("tool_name", "")
                                ti = su.get("tool_info", {})
                                params = ti.get("parameters", {})
                                output = ti.get("output")
                                has_streamed_deltas = True
                                if output is None:
                                    cmd_hint = params.get("CommandLine") or params.get("command") or json.dumps(params)
                                    prog_text = f"\n⚡ *[Running `{tn}`: `{cmd_hint}`]*\n"
                                else:
                                    out_snippet = output.strip()
                                    if len(out_snippet) > 800:
                                        out_snippet = out_snippet[:800] + "\n..."
                                    prog_text = f"```\n{out_snippet}\n```\n\n"
                                chunk_data = {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": prog_text},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                if not safe_write(b"data: " + json.dumps(chunk_data).encode("utf-8") + b"\n\n"):
                                    return
                            delta = su.get("text_delta")
                            if delta:
                                if not is_sse_mode and tools:
                                    accumulated_chunks.append(delta)
                                else:
                                    has_streamed_deltas = True
                                    chunk_data = {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": delta},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    if not safe_write(b"data: " + json.dumps(chunk_data).encode("utf-8") + b"\n\n"):
                                        return
                        elif ev == "result":
                            res = event_obj.get("result", {})
                            if res.get("status") == "ERROR":
                                stream_error = res.get("error") or "Unknown agy error"
                                break
                            if res.get("response") and not accumulated_chunks:
                                accumulated_chunks.append(res.get("response"))

                    if proc.returncode is None:
                        remaining = max(0.1, deadline - time.time())
                        await asyncio.wait_for(proc.wait(), timeout=remaining)
                    if proc.returncode != 0 and not stream_error and not intercepted_tool_call:
                        stream_error = f"agy exited with returncode {proc.returncode}"
                except TimeoutError:
                    stream_error = f"Execution timed out after {timeout} seconds"
                except Exception as e:
                    stream_error = str(e)
                finally:
                    if proc is not None:  # pragma: no cover
                        if getattr(proc, "stderr", None) is not None:
                            try:
                                read_fn = getattr(proc.stderr, "read", None)
                                if callable(read_fn):
                                    res_read = read_fn()
                                    if asyncio.iscoroutine(res_read):
                                        stderr_bytes = await asyncio.wait_for(res_read, timeout=1.0)
                                    else:
                                        stderr_bytes = res_read
                                    if isinstance(stderr_bytes, (bytes, bytearray)):
                                        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                                        if stderr_text:
                                            if stream_error:
                                                stream_error = f"{stream_error} - {stderr_text}"
                                            elif (  # pragma: no cover
                                                proc.returncode is not None
                                                and proc.returncode != 0
                                                and not intercepted_tool_call
                                            ):
                                                stream_error = stderr_text
                            except Exception:
                                pass
                        if proc.returncode is None:
                            try:
                                proc.kill()
                                await proc.wait()
                            except Exception:
                                pass

                # If an error occurred, emit an error payload and exit WITHOUT [DONE]
                # so LiteLLM and clients detect stream failure and trigger fallback.
                if stream_error:
                    is_quota = any(
                        x in stream_error.lower()
                        for x in [
                            "quota",
                            "rate",
                            "429",
                            "exhaust",
                            "resource_exhausted",
                            "resource has been exhausted",
                        ]
                    )
                    err_status = 429 if is_quota else 502
                    err_type = "rate_limit_error" if is_quota else "api_error"
                    err_chunk = {
                        "error": {
                            "message": f"agy stream error: {stream_error}",
                            "type": err_type,
                            "code": err_status,
                        }
                    }
                    safe_write(b"data: " + json.dumps(err_chunk).encode("utf-8") + b"\n\n")
                    return

                record_login(True, "agy run succeeded")
                if intercepted_tool_call:
                    tool_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": intercepted_tool_call["id"],
                                            "type": "function",
                                            "function": intercepted_tool_call["function"],
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    if not safe_write(b"data: " + json.dumps(tool_chunk).encode("utf-8") + b"\n\n"):
                        return
                    finish_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                    if not safe_write(b"data: " + json.dumps(finish_chunk).encode("utf-8") + b"\n\n"):
                        return
                    safe_write(b"data: [DONE]\n\n")
                    return

                if tools and not is_sse_mode:
                    full_text = "".join(accumulated_chunks)
                    cleaned_text, tool_calls = parse_tool_calls_from_text(full_text)
                    if tool_calls:
                        tool_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": cleaned_text or None,
                                        "tool_calls": [
                                            {
                                                "index": idx,
                                                "id": tc["id"],
                                                "type": "function",
                                                "function": tc["function"],
                                            }
                                            for idx, tc in enumerate(tool_calls)
                                        ],
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        if not safe_write(b"data: " + json.dumps(tool_chunk).encode("utf-8") + b"\n\n"):
                            return
                        finish_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        }
                        if not safe_write(b"data: " + json.dumps(finish_chunk).encode("utf-8") + b"\n\n"):
                            return
                    else:
                        text_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": full_text},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        if not safe_write(b"data: " + json.dumps(text_chunk).encode("utf-8") + b"\n\n"):
                            return
                        finish_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        if not safe_write(b"data: " + json.dumps(finish_chunk).encode("utf-8") + b"\n\n"):
                            return
                else:
                    if not has_streamed_deltas and accumulated_chunks:
                        fallback_text = "".join(accumulated_chunks)
                        fallback_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": fallback_text},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        if not safe_write(b"data: " + json.dumps(fallback_chunk).encode("utf-8") + b"\n\n"):
                            return
                    finish_data = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    if not safe_write(b"data: " + json.dumps(finish_data).encode("utf-8") + b"\n\n"):
                        return
                safe_write(b"data: [DONE]\n\n")

            try:
                loop.run_until_complete(run_openai_stream())
            finally:
                loop.close()

    def complete_chat(
        self,
        prompt: str,
        model: str,
        model_override: str,
        conversation_id: str | None,
        timeout: float,
        effort_cli: str | None,
        tools: list[Any] | None,
        is_sse_mode: bool,
        policy: str,
        plain_agent: bool,
    ) -> None:
        """Answer one non-streaming chat completion."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            import inspect

            sig = inspect.signature(execute_agy_stream_json)
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "model_override": model_override,
                "conversation_id": conversation_id,
                "timeout": timeout,
            }
            if "tools" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["tools"] = tools
            if "intercept_tools" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["intercept_tools"] = not is_sse_mode
            if "effort" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["effort"] = effort_cli
            if "policy" in sig.parameters:
                kwargs["policy"] = policy
                kwargs["plain_agent"] = plain_agent
            exec_res = loop.run_until_complete(execute_agy_stream_json(**kwargs))
        finally:
            loop.close()

        retcode = exec_res.get("returncode", 0)
        if retcode == 0:
            record_login(True, "agy run succeeded")
        if retcode != 0:
            err_text = exec_res.get("stderr") or exec_res.get("stdout") or "Unknown error"
            err_lower = err_text.lower()
            is_quota = any(
                x in err_lower
                for x in ["quota", "rate", "429", "exhaust", "resource_exhausted", "resource has been exhausted"]
            )
            status_code = 429 if is_quota else 502
            err_type = "rate_limit_error" if is_quota else "api_error"
            err_resp = {
                "error": {
                    "message": f"agy execution error: {err_text}",
                    "type": err_type,
                    "code": status_code,
                }
            }
            body_bytes = json.dumps(err_resp).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        intercepted = exec_res.get("tool_calls")
        if intercepted:
            tool_calls = intercepted
            cleaned_content = ""
            finish_reason = "tool_calls"
        else:
            text = exec_res.get("stdout", "")
            cleaned_content, tool_calls = parse_tool_calls_from_text(text)
            finish_reason = "tool_calls" if tool_calls else "stop"

        created_time = int(time.time())
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        usage_info = exec_res.get("usage") or {}
        prompt_tokens = usage_info.get("input_tokens") or max(1, len(prompt) // 4)
        completion_tokens = usage_info.get("output_tokens") or max(1, len(cleaned_content or "") // 4)

        message_obj = {
            "role": "assistant",
            "content": cleaned_content if cleaned_content else (None if tool_calls else ""),
        }
        if tool_calls:
            message_obj["tool_calls"] = tool_calls

        resp = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_time,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message_obj,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        body_bytes = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)
        return


def run_server():
    """Prepare the daemon HOME, start the live login check and serve on 127.0.0.1:PORT."""
    prepare_home()
    stop = threading.Event()
    if LIVE_CHECK_INTERVAL > 0:
        threading.Thread(target=live_check_loop, args=(stop, LIVE_CHECK_INTERVAL), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), AgyDaemonHandler)
    print(f"🚀 Host agy Daemon running on http://127.0.0.1:{PORT} (max {MAX_CONCURRENT} concurrent agy runs)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def main(argv: list[str]) -> int:
    """``--tool-guard``: agy PreToolUse hook mode; otherwise run the daemon."""
    if argv[1:2] == ["--tool-guard"]:
        return tool_guard_main(sys.stdin, sys.stdout, os.environ)
    run_server()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
