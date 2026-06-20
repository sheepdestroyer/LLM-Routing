#!/usr/bin/env python3
"""Entrypoint for LiteLLM container — loads secrets from bind-mounted files."""
import os
import json
import sys
import time
import socket

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

# Patch spend_management_endpoints.py to support flexible date formats for UI logs page
import glob
import sys
import litellm

litellm_path = os.path.dirname(litellm.__file__)
endpoints_paths = [
    os.path.join(litellm_path, "proxy/spend_tracking/spend_management_endpoints.py")
] + glob.glob("/app/.venv/lib/python*/site-packages/litellm/proxy/spend_tracking/spend_management_endpoints.py")

for endpoints_path in endpoints_paths:
    if os.path.exists(endpoints_path):
        print(f"🩹 Patching {endpoints_path} for flexible date formats...")
        sys.stdout.flush()
        try:
            with open(endpoints_path, "r") as f:
                code = f.read()
            
            target1 = 'is_v2 = "/spend/logs/v2" in get_request_route(request)\n        formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"] if is_v2 else ["%Y-%m-%d %H:%M:%S"]'
            replacement1 = '''is_v2 = "/spend/logs/v2" in get_request_route(request)
        formats = [
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S%z"
        ]'''
            
            target2 = '''    start_date_obj: Optional[datetime] = None
    end_date_obj: Optional[datetime] = None
    if start_date is not None:
        start_date_obj = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    if end_date is not None:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )'''
            replacement2 = '''    start_date_obj: Optional[datetime] = None
    end_date_obj: Optional[datetime] = None
    def _parse_detail_date(date_str: str) -> datetime:
        for fmt in [
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S%z"
        ]:
            try:
                return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        raise ValueError(f"Invalid date format: {date_str}")

    if start_date is not None:
        start_date_obj = _parse_detail_date(start_date)
    if end_date is not None:
        end_date_obj = _parse_detail_date(end_date)'''
            
            patched = False
            if target1 in code:
                code = code.replace(target1, replacement1)
                print("   ✓ Patched list endpoint date parsing")
                patched = True
            else:
                print("   ⚠ Target 1 not found (already patched?)")
                
            if target2 in code:
                code = code.replace(target2, replacement2)
                print("   ✓ Patched detail endpoint date parsing")
                patched = True
            else:
                print("   ⚠ Target 2 not found (already patched?)")
                
            if patched:
                with open(endpoints_path, "w") as f:
                    f.write(code)
            sys.stdout.flush()
                
        except Exception as e:
            print(f"❌ Failed to patch {endpoints_path}: {e}")
            sys.stdout.flush()

# Exec into litellm
os.execvp("litellm", ["litellm", "--config", "/app/config.yaml", "--port", "4000"])

