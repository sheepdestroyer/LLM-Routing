#!/usr/bin/env python3
"""HTTP daemon to bridge router requests to the host-side agy CLI."""
import asyncio
import json
import os
import re
import tempfile
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 5005
AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
CACHE_FILE = os.path.expanduser("~/.gemini/antigravity-cli/cache/last_conversations.json")
CLI_TOKEN_PATH = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")

def get_last_conversation_id():
    """Retrieve the last active conversation ID from the agy cache."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            # Use current workspace
            return data.get(os.getcwd())
    except Exception:
        pass
    return None

def read_file_sync(path):
    """Synchronously read and return content from a file, returning empty string on error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""

def get_auth_status() -> dict:
    """Check current agy OAuth token status and expiration from CLI token file."""
    try:
        if not os.path.exists(CLI_TOKEN_PATH):
            return {"authenticated": False, "status": "missing", "detail": "No credentials found", "expiry_ms": 0}

        try:
            with open(CLI_TOKEN_PATH, "r", encoding="utf-8") as f:
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
            return {"authenticated": False, "status": "missing", "detail": "No access token in credentials", "expiry_ms": 0}

        expiry_ms = 0
        if isinstance(expiry_val, (int, float)):
            expiry_ms = int(expiry_val * 1000) if expiry_val < 10000000000 else int(expiry_val)
        elif isinstance(expiry_val, str) and expiry_val.strip():
            s = expiry_val.strip()
            normalized = re.sub(r"(\.\d{6})\d+", r"\1", s)
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                expiry_dt = datetime.fromisoformat(normalized)
                expiry_ms = int(expiry_dt.timestamp() * 1000)
            except Exception:
                expiry_ms = 0

        current_ms = int(time.time() * 1000)
        diff_sec = (expiry_ms - current_ms) / 1000.0 if expiry_ms > 0 else 0
        is_valid = diff_sec > 0 or (isinstance(token_info, dict) and bool(token_info.get("refresh_token")))

        return {
            "authenticated": bool(access_token),
            "status": "valid" if diff_sec > 0 else ("valid_silent_refresh" if is_valid else "expired"),
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
            quotas.append({
                "category": parts[0],
                "limit_type": parts[1],
                "remaining": parts[2],
                "reset_time": parts[3],
            })
        elif len(parts) == 3:
            quotas.append({
                "category": parts[0],
                "limit_type": parts[1],
                "remaining": parts[2],
                "reset_time": "",
            })
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

async def execute_agy_print(prompt: str, model_override: str = "", conversation_id: str = None, timeout: float = 120.0):
    """Asynchronously execute agy and capture full output."""
    env = os.environ.copy()
    if model_override:
        env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = model_override
    else:
        env.pop("CASCADE_DEFAULT_MODEL_OVERRIDE", None)

    cmd = [AGY_BINARY]
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
            *cmd, env=env,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        stdout_file.close()
        stderr_file.close()

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            returncode = proc.returncode or 0
        except asyncio.TimeoutError:
            try:
                if proc is not None:
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

    result_conv_id = get_last_conversation_id()
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "conversation_id": result_conv_id
    }

class AgyDaemonHandler(BaseHTTPRequestHandler):
    """HTTP request handler for agy execution requests."""
    def log_message(self, format, *args):
        """Override to silence standard HTTP logging."""
        pass

    def do_GET(self):
        """Handle GET requests for health checks, usage/quota, and model listings."""
        if self.path in ["/health", "/status"]:
            res = {
                "status": "ok",
                "agy_binary": AGY_BINARY,
                "agy_available": os.path.exists(AGY_BINARY),
                "auth": get_auth_status(),
                "last_conversation_id": get_last_conversation_id(),
            }
            body = json.dumps(res).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/run":
            res = {"status": "ok", "message": "Host agy daemon is running"}
            body = json.dumps(res).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path in ["/usage", "/quota"]:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                exec_res = loop.run_until_complete(execute_agy_print("/usage", model_override="gpt-oss-120b-medium", timeout=30.0))
                parsed = parse_usage_output(exec_res.get("stdout", ""))
                parsed["returncode"] = exec_res.get("returncode", 0)
                parsed["stderr"] = exec_res.get("stderr", "")
            except Exception as e:
                parsed = {"error": str(e), "quotas": []}
            finally:
                loop.close()

            body = json.dumps(parsed).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/models":
            import subprocess
            try:
                result = subprocess.run([AGY_BINARY, "models"], capture_output=True, text=True, timeout=15)
                models = parse_models_output(result.stdout)
                res = {"status": "ok", "models": models}
            except Exception as e:
                res = {"status": "error", "error": str(e), "models": []}

            body = json.dumps(res).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        """Handle POST requests to execute agy commands."""
        if self.path not in ["/run", "/usage"]:
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            body = json.loads(post_data.decode('utf-8')) if post_data else {}
        except (ValueError, json.JSONDecodeError) as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Invalid JSON payload: {e}"}).encode('utf-8'))
            return

        if self.path == "/usage":
            model = body.get("model", "gpt-oss-120b-medium")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                exec_res = loop.run_until_complete(execute_agy_print("/usage", model_override=model, timeout=30.0))
                parsed = parse_usage_output(exec_res.get("stdout", ""))
                parsed["returncode"] = exec_res.get("returncode", 0)
            except Exception as e:
                parsed = {"error": str(e), "quotas": []}
            finally:
                loop.close()
            body_bytes = json.dumps(parsed).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        
        prompt = body.get("prompt", "")
        model_override = body.get("model_override", "")
        conversation_id = body.get("conversation_id", None)
        timeout = body.get("timeout", 120.0)
        stream = body.get("stream", False)
        
        if stream:
            # 1. Send HTTP headers for streaming NDJSON
            self.protocol_version = 'HTTP/1.1'
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Connection', 'close')
            self.end_headers()
            
            # 2. Setup loop to run async process and stream output
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            async def run_stream():
                """Asynchronously execute agy and stream output via PTY."""
                import pty
                
                env = os.environ.copy()
                if model_override:
                    env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = model_override
                else:
                    env.pop("CASCADE_DEFAULT_MODEL_OVERRIDE", None)
                    
                cmd = [AGY_BINARY]
                if conversation_id:
                    cmd.extend(["--conversation", conversation_id])
                cmd.extend(["--print", prompt])
                
                master_fd, slave_fd = pty.openpty()
                proc = None
                try:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd, env=env,
                            stdout=slave_fd,
                            stderr=slave_fd,
                        )
                        os.close(slave_fd)
                    except Exception as e:
                        os.close(slave_fd)
                        # Write failure details as status
                        err_msg = json.dumps({"type": "status", "returncode": -1, "stderr": str(e)}) + "\n"
                        self.wfile.write(err_msg.encode('utf-8'))
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
                        text = data.decode('utf-8', errors='replace')
                        # PTY text can have \r\n, normalize to \n
                        text_norm = text.replace('\r\n', '\n')
                        # Yield token JSON line
                        chunk_json = json.dumps({"type": "token", "content": text_norm}) + "\n"
                        self.wfile.write(chunk_json.encode('utf-8'))
                        self.wfile.flush()

                    try:
                        await asyncio.wait_for(proc.wait(), timeout=timeout)
                        returncode = proc.returncode or 0
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                        returncode = -1
                    except Exception:
                        returncode = -1

                    # Retrieve last conversation ID
                    result_conv_id = get_last_conversation_id()

                    # Write closing metadata
                    meta_json = json.dumps({
                        "type": "status",
                        "returncode": returncode,
                        "conversation_id": result_conv_id
                    }) + "\n"
                    self.wfile.write(meta_json.encode('utf-8'))
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

        response_bytes = json.dumps(res).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

def run_server():
    """Start the ThreadingHTTPServer on the configured port."""
    server = ThreadingHTTPServer(('127.0.0.1', PORT), AgyDaemonHandler)
    print(f"🚀 Host agy Daemon running on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    run_server()
