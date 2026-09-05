#!/usr/bin/env python3
"""Entrypoint for LiteLLM container — loads secrets from bind-mounted files."""

import datetime
import json
import os
import re
import shlex
import socket
import sys
import threading
import time
import traceback
from datetime import datetime as original_datetime

# Load .env into os.environ
env_path = "/config/.env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                # effective.env is shell-quoted by start-stack.sh; parse it with
                # the same rules as the router's `source /config/.env`.
                try:
                    val = shlex.split(val, comments=False, posix=True)[0] if val else ""
                except ValueError:
                    val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)

# Load Gemini OAuth token from Antigravity CLI token file
token_path = "/config/gemini_auth/antigravity-cli/antigravity-oauth-token"
if os.path.exists(token_path) and "GEMINI_OAUTH_TOKEN" not in os.environ:
    try:
        with open(token_path) as f:
            creds = json.load(f)
            if isinstance(creds, dict):
                token = creds.get("access_token")
                if not token and isinstance(creds.get("token"), dict):
                    token = creds["token"].get("access_token")
                if token:
                    os.environ["GEMINI_OAUTH_TOKEN"] = token
    except (OSError, json.JSONDecodeError, AttributeError):
        pass


# Wait for PostgreSQL to be ready before starting LiteLLM
# This prevents "Can't reach database server" errors during pod restarts
# when LiteLLM tries to run Prisma migrations before PostgreSQL is available
def check_tcp_port(ip: str, port: int) -> bool:
    """Checks if a TCP port is accepting connections."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


max_wait = 60
postgres_port_str = os.environ.get("POSTGRES_PORT") or "5432"
try:
    postgres_port = int(postgres_port_str)
except ValueError:
    print(f"⚠️ Invalid POSTGRES_PORT '{postgres_port_str}', defaulting to 5432")
    postgres_port = 5432
print(f"🔌 Waiting for PostgreSQL on :{postgres_port} (max {max_wait}s)...")
for i in range(max_wait):
    if check_tcp_port("127.0.0.1", postgres_port):
        print(f"✅ PostgreSQL ready after {i + 1}s")
        break
    time.sleep(1)
else:
    print(f"⚠️ Warning: PostgreSQL not ready after {max_wait}s — proceeding anyway")


# Patch LiteLLM at runtime to support flexible date formats
# Based on PR feedback, we patch datetime.datetime globally for robustness.
# We ensure naive/aware safety by trying the original format first.
class RobustDatetime(original_datetime):
    """A datetime subclass that handles flexible date format parsing in strptime."""

    @classmethod
    def strptime(cls, date_str: str, fmt: str) -> original_datetime:
        """Flexible strptime implementation that handles various ISO-like formats."""
        if not isinstance(date_str, str):
            return original_datetime.strptime(date_str, fmt)

        # 1. Try the original format first to maintain compatibility (returning naive if expected)
        try:
            return original_datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            pass

        # 2. Try flexible fallbacks if the original format failed
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
        for f in formats:
            if f == fmt:
                continue
            try:
                dt = original_datetime.strptime(date_str, f)
                # For fallbacks, ensure we return a UTC-aware datetime
                if dt.tzinfo is not None:
                    return dt.astimezone(datetime.UTC)
                return dt.replace(tzinfo=datetime.UTC)
            except (ValueError, TypeError):
                continue

        # Fallback to original behavior to raise expected ValueError if all formats fail
        return original_datetime.strptime(date_str, fmt)


print("🩹 Applying global runtime patch for flexible date formats...")
datetime.datetime = RobustDatetime
sys.stdout.flush()

# Register both RobustDatetime AND the original datetime with Prisma's
# singledispatch serializer. When entrypoint.py replaces datetime.datetime
# with RobustDatetime before Prisma loads, Prisma's own
# @serializer.register(datetime.datetime) ends up registering RobustDatetime.
# But database drivers (psycopg2) return the *original* C-level datetime
# instances, which no longer match. We must register both classes.
try:
    from prisma.builder import serializer
except ImportError as e:
    if e.name is not None and e.name not in ("prisma", "prisma.builder", "serializer"):
        raise
    serializer = None

if serializer is not None:

    def _serialize_dt(dt):
        """Serialize datetime to ISO8601 with timezone (UTC if naive)."""
        if dt.utcoffset() is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        else:
            dt = dt.astimezone(datetime.UTC)
        return dt.isoformat().replace("+00:00", "Z")

    serializer.register(original_datetime, _serialize_dt)
    serializer.register(RobustDatetime, _serialize_dt)
    print("🩹 Registered original_datetime + RobustDatetime with Prisma serializer")
sys.stdout.flush()


def patch_langfuse_media_manager() -> bool:
    """Patch Langfuse MediaManager to suppress multimodal blob uploads when disabled."""
    if os.environ.get("LANGFUSE_MEDIA_UPLOAD_ENABLED", "false").lower() in ("false", "0", "no"):
        try:
            from langfuse._task_manager.media_manager import MediaManager

            MediaManager.process_media_in_event = lambda self, event: None
            print("🩹 Disabled Langfuse MediaManager event processing (multimodal blob upload suppressed)")
            sys.stdout.flush()
            return True
        except Exception:
            return False
    return False


patch_langfuse_media_manager()

# Configure logging: ensure INFO/DEBUG/WARNING route to stdout (priority 6 in journald)
# and ERROR/CRITICAL route to stderr (priority 3 in journald).
import logging

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class SingleLineFormatter(logging.Formatter):
    """Formats log records strictly on a single line with explicit severity tagging.

    Strips ANSI color escape sequences and collapses tracebacks and multiline
    messages into a single line delimited by ' | '.
    """

    def __init__(self, fmt: str | None = None, datefmt: str | None = None):
        if fmt is None:
            fmt = "%(asctime)s [%(levelname)s] [%(name)s] %(filename)s:%(lineno)s - %(message)s"
        if datefmt is None:
            datefmt = "%Y-%m-%d %H:%M:%S"
        super().__init__(fmt=fmt, datefmt=datefmt)

    def formatException(self, exc_info) -> str:
        """Format an exception traceback into a single line."""
        if not exc_info:
            return ""
        lines = traceback.format_exception(*exc_info)
        cleaned = [part.strip() for chunk in lines for part in chunk.splitlines() if part.strip()]
        return " [Traceback: " + " | ".join(cleaned) + "]"

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        cleaned = _ANSI_RE.sub("", formatted)

        # Append correlation context if present from LiteLLM CorrelationContextFilter
        trace_id = getattr(record, "trace_id", None)
        session_id = getattr(record, "session_id", None)
        if trace_id or session_id:
            parts = [f"trace_id={trace_id}" if trace_id else "", f"session_id={session_id}" if session_id else ""]
            ctx = f" [{' '.join(p for p in parts if p)}]"
            if " [Traceback:" in cleaned:
                prefix, sep, suffix = cleaned.partition(" [Traceback:")
                cleaned = f"{prefix}{ctx}{sep}{suffix}"
            else:
                cleaned = f"{cleaned}{ctx}"

        # Collapse any internal newlines or carriage returns so journald/conmon preserves it as a single line
        if "\n" in cleaned or "\r" in cleaned:
            cleaned = " | ".join(part.strip() for part in cleaned.splitlines() if part.strip())

        return cleaned


def single_line_excepthook(exc_type, exc_value, exc_tb):
    """Ensure uncaught exceptions are formatted as a single line with [CRITICAL] severity."""
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    lines = traceback.format_exception(exc_type, exc_value, exc_tb)
    cleaned = " | ".join(part.strip() for chunk in lines for part in chunk.splitlines() if part.strip())
    now = original_datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"{now} [CRITICAL] [UncaughtException] {cleaned}\n")
    sys.stderr.flush()


def _threading_excepthook(args):
    """Ensure uncaught exceptions in background threads are formatted on a single line."""
    single_line_excepthook(args.exc_type, args.exc_value, args.exc_tb)


sys.excepthook = single_line_excepthook
threading.excepthook = _threading_excepthook


class MaxLevelFilter(logging.Filter):
    """Filter that only passes log records up to a maximum severity level."""

    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


class MinLevelFilter(logging.Filter):
    """Filter that only passes log records with at least a minimum severity level."""

    def __init__(self, min_level: int):
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


CLIENT_AUTH_ERROR_PATTERNS = (
    "Key not found in database",
    "KeyNotFoundError",
    "Invalid proxy server token passed",
    "user_api_key_auth(): Exception occured - Authentication Error",
    "LiteLLM Virtual Key expected",
    "ProxyException: Key not found in database",
    "ProxyException: Key not found",
    "Key not found.",
    "Key not found:",
    "Key not found in team",
)


def is_client_auth_error(record: logging.LogRecord) -> bool:
    """Determine if a log record represents a client authentication or key lookup error."""
    try:
        msg = record.getMessage()
    except Exception:
        msg = str(record.msg)

    exc_text = ""
    if isinstance(record.exc_info, tuple) and len(record.exc_info) >= 2:
        exc_type, exc_val = record.exc_info[0], record.exc_info[1]
        type_name = getattr(exc_type, "__name__", "") if exc_type else ""
        exc_text = f"{type_name} {exc_val}"

    rec_exc_text = getattr(record, "exc_text", "") or ""
    full_text = f"{msg} {exc_text} {rec_exc_text}"
    return any(pattern in full_text for pattern in CLIENT_AUTH_ERROR_PATTERNS)


class ClientAuthLogFilter(logging.Filter):
    """Filter that intercepts client authentication and key lookup errors.

    Downgrades log severity from ERROR (40) to WARNING (30) so they are stamped
    with [WARNING] and routed to stdout instead of stderr. Also strips the redundant
    Python traceback (record.exc_info = None) to eliminate stack trace noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and is_client_auth_error(record):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            try:
                msg_text = record.getMessage()
            except Exception:
                msg_text = str(record.msg)
            if "Traceback (most recent call last):" in msg_text:
                record.msg = msg_text.split("Traceback (most recent call last):")[0].rstrip(" \t\n\r|")
                record.args = None
        return True


# Configure uvicorn default logging before LiteLLM proxy starts
try:
    import uvicorn.config

    if "handlers" in uvicorn.config.LOGGING_CONFIG:
        if "default" in uvicorn.config.LOGGING_CONFIG["handlers"]:
            uvicorn.config.LOGGING_CONFIG["handlers"]["default"]["stream"] = "ext://sys.stdout"
except Exception:
    pass

# Start LiteLLM Proxy
from litellm.proxy.proxy_cli import run_server

try:
    import litellm._logging as ll_log

    _ll_level_str = os.environ.get("LITELLM_LOG", "INFO").upper()
    _ll_level = getattr(logging, _ll_level_str, logging.INFO)
    _single_line_fmt = SingleLineFormatter()
    _client_auth_filter = ClientAuthLogFilter()

    _stdout_h = logging.StreamHandler(sys.stdout)
    _stdout_h.setLevel(_ll_level)
    _stdout_h.addFilter(_client_auth_filter)
    _stdout_h.addFilter(MaxLevelFilter(logging.WARNING))
    for _f in getattr(ll_log.handler, "filters", []):
        _stdout_h.addFilter(_f)
    _stdout_h.setFormatter(_single_line_fmt)

    _stderr_h = logging.StreamHandler(sys.stderr)
    _stderr_h.setLevel(logging.ERROR)
    _stderr_h.addFilter(_client_auth_filter)
    _stderr_h.addFilter(MinLevelFilter(logging.ERROR))
    for _f in getattr(ll_log.handler, "filters", []):
        _stderr_h.addFilter(_f)
    _stderr_h.setFormatter(_single_line_fmt)

    # Set underlying stream of default handler to stdout and apply single line formatter
    ll_log.handler.setStream(sys.stdout)
    ll_log.handler.setFormatter(_single_line_fmt)
    ll_log.handler.addFilter(_client_auth_filter)

    for _lg in [
        getattr(ll_log, "verbose_logger", None),
        getattr(ll_log, "verbose_proxy_logger", None),
        getattr(ll_log, "verbose_router_logger", None),
        logging.getLogger(),
        logging.getLogger("LiteLLM Proxy"),
        logging.getLogger("LiteLLM"),
        logging.getLogger("LiteLLM Router"),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("litellm_proxy_extras"),
        logging.getLogger("prisma"),
        logging.getLogger("apscheduler"),
    ]:
        if _lg is not None:
            _lg.addFilter(_client_auth_filter)
            _lg.handlers = [_stdout_h, _stderr_h]
            _lg.setLevel(_ll_level)
            _lg.propagate = False
except Exception as e:
    sys.stderr.write(f"⚠️ Warning: Failed to configure custom single-line logging: {e}\n")

litellm_port = os.environ.get("LITELLM_PORT") or os.environ.get("PORT") or "4000"
sys.argv = ["litellm", "--config", "/app/config.yaml", "--port", litellm_port]
run_server()
