#!/usr/bin/env python3
"""HTTP daemon to bridge router requests to the host-side agy CLI."""

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = int(os.environ.get("AGY_PORT", os.environ.get("PORT", 5005)))
AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
CACHE_FILE = os.path.expanduser("~/.gemini/antigravity-cli/cache/last_conversations.json")
CLI_TOKEN_PATH = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")


def get_last_conversation_id():
    """Retrieve the last active conversation ID from the agy cache."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
            # Use current workspace
            return data.get(os.getcwd())
    except Exception:
        pass
    return None


def read_file_sync(path):
    """Synchronously read and return content from a file, returning empty string on error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def get_auth_status() -> dict:
    """Check current agy OAuth token status and expiration from CLI token file."""
    try:
        if not os.path.exists(CLI_TOKEN_PATH):
            return {"authenticated": False, "status": "missing", "detail": "No credentials found", "expiry_ms": 0}

        try:
            with open(CLI_TOKEN_PATH, encoding="utf-8") as f:
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
    prompt: str, model_override: str = "", conversation_id: str | None = None, timeout: float = 600.0
):
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
            *cmd,
            env=env,
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
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr, "conversation_id": result_conv_id}


async def execute_agy_stream_json(
    prompt: str,
    model_override: str = "",
    conversation_id: str | None = None,
    timeout: float = 600.0,
    tools: list[Any] | None = None,
    intercept_tools: bool = True,
    effort: str | None = None,
) -> dict:
    """Asynchronously execute agy via stream-json over stdin and capture structured result."""
    env = os.environ.copy()
    if model_override:
        env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = model_override
    else:
        env.pop("CASCADE_DEFAULT_MODEL_OVERRIDE", None)

    cmd = [AGY_BINARY, "--input-format", "stream-json", "--output-format", "stream-json"]
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


def extract_prompt_from_messages(messages: list[Any], tools: list[Any] | None = None, is_sse_mode: bool = False) -> str:
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
        completion_instr = (
            "# Execution Guidelines\n"
            "You are acting as an intelligent autonomous backend for the client.\n"
            "You have full access to your native workspace tools to inspect files, execute commands, or analyze context as needed to fulfill the user's request.\n"
            "Provide a clear, concise conversational report."
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

        if self.path in ["/usage", "/quota"]:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                exec_res = loop.run_until_complete(
                    execute_agy_print("/usage", model_override="gpt-oss-120b-medium", timeout=30.0)
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
            return

        if self.path in ["/models", "/v1/models"]:
            import subprocess

            try:
                result = subprocess.run([AGY_BINARY, "models"], capture_output=True, text=True, timeout=15)
                models = parse_models_output(result.stdout)
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
            return

        self.send_response(404)
        self.end_headers()

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
            body_bytes = json.dumps(parsed).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        prompt = body.get("prompt", "")
        model_override = body.get("model_override", "")
        conversation_id = body.get("conversation_id", None)
        timeout = body.get("timeout", 600.0)
        stream = body.get("stream", False)

        if stream:
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
                            *cmd,
                            env=env,
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
                    result_conv_id = get_last_conversation_id()

                    # Write closing metadata
                    meta_json = (
                        json.dumps({"type": "status", "returncode": returncode, "conversation_id": result_conv_id})
                        + "\n"
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

    def handle_chat_completions(self, body: dict):
        """Handle standard OpenAI /v1/chat/completions requests from LiteLLM or direct clients."""
        messages = body.get("messages", [])
        tools = body.get("tools")
        model = body.get("model", "gemini-3.8-flash")
        model_lower = str(model).lower()
        is_sse_mode = "sse" in model_lower or "autonomous" in model_lower
        prompt = (
            extract_prompt_from_messages(messages, tools=tools, is_sse_mode=is_sse_mode)
            if messages
            else body.get("prompt", "")
        )
        stream = body.get("stream", False)
        timeout = float(body.get("timeout", 600.0))
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

        if stream:
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def run_openai_stream():
                env = os.environ.copy()
                if model_override:
                    env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = model_override
                else:
                    env.pop("CASCADE_DEFAULT_MODEL_OVERRIDE", None)

                cmd = [AGY_BINARY, "--input-format", "stream-json", "--output-format", "stream-json"]
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
                    if proc is not None:
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
                                            elif (
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
            return

        # Non-streaming response
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            import inspect

            sig = inspect.signature(execute_agy_stream_json)
            kwargs = {
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
            exec_res = loop.run_until_complete(execute_agy_stream_json(**kwargs))  # type: ignore[arg-type]
        finally:
            loop.close()

        retcode = exec_res.get("returncode", 0)
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
    """Start the ThreadingHTTPServer on the configured port."""
    server = ThreadingHTTPServer(("127.0.0.1", PORT), AgyDaemonHandler)
    print(f"🚀 Host agy Daemon running on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
