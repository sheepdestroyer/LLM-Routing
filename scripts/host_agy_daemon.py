#!/usr/bin/env python3
"""HTTP daemon to bridge router requests to the host-side agy CLI."""
import asyncio
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 5005
AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
CACHE_FILE = os.path.expanduser("~/.gemini/antigravity-cli/cache/last_conversations.json")

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

class AgyDaemonHandler(BaseHTTPRequestHandler):
    """HTTP request handler for agy execution requests."""
    def do_POST(self):
        """Handle POST requests to execute agy commands."""
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return
            
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            body = json.loads(post_data.decode('utf-8'))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Invalid JSON payload: {str(e)}"}).encode('utf-8'))
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
                        text_norm = text.replace('\r\n', '\n')
                        chunk_json = json.dumps({"type": "token", "content": text_norm}) + "\n"
                        self.wfile.write(chunk_json.encode('utf-8'))
                        self.wfile.flush()

                    try:
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

                    result_conv_id = get_last_conversation_id()

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
                        except Exception:
                            pass
                
            loop.run_until_complete(run_stream())
            loop.close()
            return
            
        # Execute in new asyncio event loop (non-streaming legacy path)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run():
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
            
            # Create temporary files for stdout/stderr to avoid hangs from daemonized children (e.g. vlc, mpv)
            stdout_file = tempfile.NamedTemporaryFile(delete=False)
            stderr_file = tempfile.NamedTemporaryFile(delete=False)
            stdout_path = stdout_file.name
            stderr_path = stderr_file.name
            
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                stdout_file.close()
                stderr_file.close()
                
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
            finally:
                try:
                    stdout_file.close()
                except Exception:
                    pass
                try:
                    stderr_file.close()
                except Exception:
                    pass
            
            # Read output from the temporary files without blocking the event loop concurrently
            loop_ref = asyncio.get_running_loop()
            stdout, stderr = await asyncio.gather(
                loop_ref.run_in_executor(None, read_file_sync, stdout_path),
                loop_ref.run_in_executor(None, read_file_sync, stderr_path),
            )
                
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
            
        res = loop.run_until_complete(run())
        loop.close()
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(res).encode('utf-8'))

def main():
    """Start the multi-threaded HTTP server."""
    server = ThreadingHTTPServer(('0.0.0.0', PORT), AgyDaemonHandler)
    print(f"🚀 Host agy daemon listening on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down agy daemon...")
        server.server_close()

if __name__ == "__main__":
    main()
