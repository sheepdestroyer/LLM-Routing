#!/usr/bin/env python3
"""Entrypoint for LiteLLM container — loads secrets from bind-mounted files."""
import os
import json
import sys
import time
import socket
import datetime
from datetime import datetime as original_datetime, timezone

# Load .env into os.environ
env_path = "/config/.env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                os.environ[key] = val

# Load Gemini OAuth token from credentials JSON
creds_path = "/config/gemini_auth/oauth_creds.json"
if os.path.exists(creds_path):
    try:
        with open(creds_path) as f:
            creds = json.load(f)
            token = creds.get("access_token", "")
            if token:
                os.environ["GEMINI_OAUTH_TOKEN"] = token
    except (json.JSONDecodeError, IOError):
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
print(f"🔌 Waiting for PostgreSQL on :5432 (max {max_wait}s)...")
for i in range(max_wait):
    if check_tcp_port("127.0.0.1", 5432):
        print(f"✅ PostgreSQL ready after {i+1}s")
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
        if not isinstance(date_str, str):
            return original_datetime.strptime(date_str, fmt)

        # 1. Try the original format first to maintain compatibility (returning naive if expected)
        try:
            return original_datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            pass

        # 2. Try flexible fallbacks if the original format failed
        formats = [
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S%z"
        ]
        for f in formats:
            if f == fmt:
                continue
            try:
                dt = original_datetime.strptime(date_str, f)
                # For fallbacks, ensure we return a UTC-aware datetime
                if dt.tzinfo is not None:
                    return dt.astimezone(timezone.utc)
                return dt.replace(tzinfo=timezone.utc)
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
except ImportError:
    serializer = None

if serializer is not None:
    def _serialize_dt(dt):
        """Serialize datetime to ISO8601 with timezone (UTC if naive)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    serializer.register(original_datetime, _serialize_dt)
    serializer.register(RobustDatetime, _serialize_dt)
    print("🩹 Registered original_datetime + RobustDatetime with Prisma serializer")
sys.stdout.flush()

# Start LiteLLM Proxy
import litellm
from litellm.proxy.proxy_cli import run_server
sys.argv = ["litellm", "--config", "/app/config.yaml", "--port", "4000"]
run_server()
