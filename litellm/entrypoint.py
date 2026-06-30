#!/usr/bin/env python3
"""Entrypoint for LiteLLM container — loads secrets from bind-mounted files."""
import os
import json
import sys
import time
import socket
from datetime import datetime, timezone

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
class RobustDatetime(datetime):
    """A datetime subclass that handles flexible date format parsing in strptime."""
    @classmethod
    def strptime(cls, date_str: str, fmt: str) -> datetime:
        if not isinstance(date_str, str):
            return datetime.strptime(date_str, fmt)

        # 1. Try the original format first to maintain compatibility (returning naive if expected)
        try:
            return datetime.strptime(date_str, fmt)
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
                dt = datetime.strptime(date_str, f)
                # For fallbacks, ensure we return a UTC-aware datetime
                if dt.tzinfo is not None:
                    return dt.astimezone(timezone.utc)
                return dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

        # Fallback to original behavior to raise expected ValueError if all formats fail
        return datetime.strptime(date_str, fmt)

def apply_runtime_patches():
    print("🩹 Applying localized runtime patches for flexible date formats...")
    # Patch only specific modules to avoid global side effects (like naive/aware TypeErrors)
    targets = [
        "litellm.proxy.spend_tracking.spend_management_endpoints",
        "litellm.proxy.analytics_endpoints.analytics_endpoints",
        "litellm.proxy.auth.litellm_license"
    ]
    for target in targets:
        try:
            # We import the module and replace its 'datetime' reference
            mod = __import__(target, fromlist=["datetime"])
            if hasattr(mod, "datetime"):
                mod.datetime = RobustDatetime
                print(f"   ✓ Patched {target}")
        except (ImportError, AttributeError):
            pass
    sys.stdout.flush()

apply_runtime_patches()

# Start LiteLLM Proxy
import litellm
from litellm.proxy.proxy_cli import run_server
sys.argv = ["litellm", "--config", "/app/config.yaml", "--port", "4000"]
run_server()
