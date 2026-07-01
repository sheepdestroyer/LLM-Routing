#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 5005
AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
CACHE_FILE = os.path.expanduser("~/.gemini/antigravity-cli/cache/last_conversations.json")

def get_last_conversation_id():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            # Use current workspace
            return data.get(os.getcwd())
    except Exception:
        pass
    return None

class AgyDaemonHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return
            
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        body = json.loads(post_data.decode('utf-8'))
        
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
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, env=env,
                        stdout=slave_fd,
                        stderr=slave_fd,
                    )
                except Exception as e:
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    # Write failure details as status
                    try:
                        err_msg = json.dumps({"type": "status", "returncode": -1, "stderr": str(e)}) + "\n"
                        self.wfile.write(err_msg.encode('utf-8'))
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                finally:
                    # Always close the slave end in the parent process
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass

                returncode = -1
                try:
                    loop_ref = asyncio.get_running_loop()

                    def read_bytes():
                        try:
                            return os.read(master_fd, 1024)
                        except OSError:
                            return b""

                    while True:
                        data = await loop_ref.run_in_executor(None, read_bytes)
                        if not data:
                            break
                        text = data.decode('utf-8', errors='replace')
                        text_norm = text.replace('\r\n', '\n')
                        chunk_json = json.dumps({"type": "token", "content": text_norm}) + "\n"
                        self.wfile.write(chunk_json.encode('utf-8'))
                        self.wfile.flush()

                    # Wait for subprocess
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                    returncode = proc.returncode or 0
                except asyncio.TimeoutError:
                    returncode = -1
                except Exception:
                    returncode = -1
                finally:
                    # Ensure process is killed and cleaned up
                    if proc and proc.returncode is None:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass

                    # Ensure master FD is closed
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass

                # Retrieve last conversation ID and write closing status
                try:
                    result_conv_id = get_last_conversation_id()
                    meta_json = json.dumps({
                        "type": "status",
                        "returncode": returncode,
                        "conversation_id": result_conv_id
                    }) + "\n"
                    self.wfile.write(meta_json.encode('utf-8'))
                    self.wfile.flush()
                except Exception:
                    pass
                
            loop.run_until_complete(run_stream())
            loop.close()
            return
            
        # Execute in new asyncio event loop (non-streaming legacy path)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run():
            env = os.environ.copy()
            if model_override:
                env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = model_override
            else:
                env.pop("CASCADE_DEFAULT_MODEL_OVERRIDE", None)
                
            cmd = [AGY_BINARY]
            if conversation_id:
                cmd.extend(["--conversation", conversation_id])
            cmd.extend(["--print", prompt])
            
            # Create temporary files for stdout/stderr to avoid hangs from daemonized children (e.g. vlc, mpv)
            with tempfile.NamedTemporaryFile(delete=False) as stdout_file, \
                 tempfile.NamedTemporaryFile(delete=False) as stderr_file:
                 
                stdout_path = stdout_file.name
                stderr_path = stderr_file.name
                
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, env=env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                    )
                    
                    # Wait only for the main agy process to exit
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                    returncode = proc.returncode or 0
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    returncode = -1
                except Exception:
                    returncode = -1
                
                # Read output from the temporary files
                try:
                    with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
                        stdout = f.read().strip()
                except Exception:
                    stdout = ""
                    
                try:
                    with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
                        stderr = f.read().strip()
                except Exception:
                    stderr = ""
                    
                # Clean up temporary files
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
            
        result = loop.run_until_complete(run())
        loop.close()
        
        response_bytes = json.dumps(result).encode('utf-8')
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        # Silence HTTP log outputs in standard output to keep service clean
        pass

def run_server():
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
