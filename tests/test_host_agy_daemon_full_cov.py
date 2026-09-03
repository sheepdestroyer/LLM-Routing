import asyncio
import io
import json
import os
import subprocess
from datetime import datetime, timezone, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

import host_agy_daemon


class DummyHandler(host_agy_daemon.AgyDaemonHandler):
    """Dummy AgyDaemonHandler that captures responses into memory buffers."""

    def __init__(self, method="GET", path="/", body=b"", headers=None):
        self.command = method
        self.path = path
        self.request_version = "HTTP/1.1"
        self.headers = headers or {}
        if isinstance(body, str):
            body = body.encode("utf-8")
        if "Content-Length" not in self.headers and body:
            self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status_code = None
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.status_code = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = value

    def end_headers(self):
        pass

    def log_message(self, format, *args):
        pass


def test_read_file_sync_branches(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("  hello world  \n")
    assert host_agy_daemon.read_file_sync(str(f)) == "hello world"
    assert host_agy_daemon.read_file_sync(str(tmp_path / "non_existent.txt")) == ""


def test_get_auth_status_branches(tmp_path, monkeypatch):
    token_file = tmp_path / "token.json"
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(token_file))

    # 1. Missing file
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "missing"
    assert res["authenticated"] is False

    # 2. Invalid JSON
    token_file.write_text("not json")
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "error"
    assert "Invalid token JSON" in res["detail"]

    # 3. Empty data
    token_file.write_text("{}")
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "missing"

    # 4. No access token in credentials
    token_file.write_text(json.dumps({"some_key": "some_val"}))
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "missing"
    assert "No access token" in res["detail"]

    # 5. Non-dict token_info with root access_token and expiry_date in seconds (< 1e10)
    future_sec = int(datetime.now(UTC).timestamp() + 3600)
    token_file.write_text(json.dumps({"access_token": "tok123", "expiry_date": future_sec}))
    res = host_agy_daemon.get_auth_status()
    assert res["authenticated"] is True
    assert res["status"] == "valid"
    assert res["expiry_ms"] == future_sec * 1000

    # 6. Expiry in milliseconds (>= 1e10)
    future_ms = future_sec * 1000
    token_file.write_text(json.dumps({"access_token": "tok123", "expiry": future_ms}))
    res = host_agy_daemon.get_auth_status()
    assert res["expiry_ms"] == future_ms

    # 7. Expiry as ISO string with Z
    iso_str = "2099-01-01T12:00:00.123456789Z"
    token_file.write_text(json.dumps({"token": {"access_token": "tok123", "expiry": iso_str}}))
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "valid"

    # 8. Expiry as invalid string (catches ValueError/Exception in fromisoformat)
    token_file.write_text(json.dumps({"token": {"access_token": "tok123", "expiry": "invalid-date"}}))
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "expired"
    assert res["expiry_ms"] == 0

    # 9. Expired with refresh token -> valid_silent_refresh
    token_file.write_text(json.dumps({"token": {"access_token": "tok123", "expiry": 100, "refresh_token": "ref"}}))
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "valid_silent_refresh"

    # 10. Outer exception handling (e.g. os.path.exists raises OSError)
    def mock_exists_err(p):
        raise OSError("permission denied on path")

    monkeypatch.setattr(host_agy_daemon.os.path, "exists", mock_exists_err)
    res = host_agy_daemon.get_auth_status()
    assert res["status"] == "error"
    assert "permission denied on path" in res["detail"]


def test_parse_usage_output_branches():
    text = """
    Quota: header line to ignore
    CategoryA    LimitTypeA    50/100    Resets in 2h
    CategoryB    LimitTypeB    Unlimited
    SingleColumnOnly
    """
    res = host_agy_daemon.parse_usage_output(text)
    quotas = res["quotas"]
    assert len(quotas) == 3
    assert quotas[0] == {
        "category": "CategoryA",
        "limit_type": "LimitTypeA",
        "remaining": "50/100",
        "reset_time": "Resets in 2h",
    }
    assert quotas[1] == {
        "category": "CategoryB",
        "limit_type": "LimitTypeB",
        "remaining": "Unlimited",
        "reset_time": "",
    }
    assert quotas[2] == {"raw": "SingleColumnOnly"}


def test_parse_models_output_branches():
    raw = """
    Fetching available models...
    ⠋ model-1    Model One Full Name
    model-2-no-description

    """
    models = host_agy_daemon.parse_models_output(raw)
    assert len(models) == 2
    assert models[0] == {"id": "model-1", "name": "Model One Full Name"}
    assert models[1] == {"id": "model-2-no-description", "name": "model-2-no-description"}


@pytest.mark.asyncio
async def test_execute_agy_print_branches(monkeypatch):
    # 1. Test model_override set and conversation_id set
    captured_args = []
    captured_env = {}

    class MockFileWrapper:
        def __init__(self, f):
            self._f = f
            self.close_count = 0

        def close(self):
            self.close_count += 1
            if self.close_count > 1:
                raise OSError("error on subsequent close")
            self._f.close()

        def __getattr__(self, item):
            return getattr(self._f, item)

    real_open = open

    def mock_open_raising_on_close(path, mode="r", *args, **kwargs):
        f = real_open(path, mode, *args, **kwargs)
        if "agy_out_" in path or "agy_err_" in path:
            return MockFileWrapper(f)
        return f

    monkeypatch.setattr("builtins.open", mock_open_raising_on_close)

    async def mock_exec(*cmd, **kwargs):
        captured_args.extend(cmd)
        captured_env.update(kwargs.get("env", {}))
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: "c123")

    res = await host_agy_daemon.execute_agy_print(
        "test prompt", model_override="claude-sonnet-4-6", conversation_id="conv-99"
    )
    assert "--conversation" in captured_args
    assert captured_env["CASCADE_DEFAULT_MODEL_OVERRIDE"] == "claude-sonnet-4-6"
    assert res["conversation_id"] == "c123"

    # 2. TimeoutError with proc.kill() raising Exception
    async def mock_exec_timeout(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = None

        async def slow_wait():
            await asyncio.sleep(5)

        proc.wait = slow_wait
        proc.kill = MagicMock(side_effect=RuntimeError("kill error"))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_timeout)
    res = await host_agy_daemon.execute_agy_print("prompt", timeout=0.01)
    assert res["returncode"] == -1
    assert res["stderr"] == "TIMEOUT"

    # 3. Generic Exception during execution and os.unlink failure
    async def mock_exec_generic_exc(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = None
        proc.wait = AsyncMock(side_effect=RuntimeError("crash in wait"))
        proc.kill = MagicMock()
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_generic_exc)

    def mock_unlink_err(path):
        raise OSError("unlink error")

    monkeypatch.setattr(host_agy_daemon.os, "unlink", mock_unlink_err)
    res = await host_agy_daemon.execute_agy_print("prompt")
    assert res["returncode"] == -1


@pytest.mark.asyncio
async def test_execute_agy_stream_json_mock_branches(monkeypatch):
    # Tests the communicate (AsyncMock) branch of execute_agy_stream_json
    # 1. Tools interception
    captured_cmd = []

    async def mock_exec(*cmd, **kwargs):
        captured_cmd.extend(cmd)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock()

        stdout_lines = [
            "",
            json.dumps({"event": "init", "conversation_id": "c-mock-1"}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {"parameters": {"CommandLine": "whoami"}},
                    },
                }
            ),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}),
        ]
        proc.communicate.return_value = ("\n".join(stdout_lines).encode("utf-8"), b"")
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    tools = [{"type": "function", "function": {"name": "terminal"}}]
    res = await host_agy_daemon.execute_agy_stream_json(
        "run whoami",
        model_override="test-model",
        conversation_id="conv-1",
        effort="high",
        tools=tools,
        intercept_tools=True,
    )
    assert "--conversation" in captured_cmd
    assert "--effort" in captured_cmd
    assert res["returncode"] == 0
    assert len(res["tool_calls"]) == 1
    assert res["tool_calls"][0]["function"]["name"] == "terminal"

    # 2. Result ERROR event with stderr and without stderr, and invalid json line
    async def mock_exec_err(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock()
        stdout_lines = [
            "invalid json line",
            json.dumps({"event": "result", "result": {"status": "ERROR", "error": "fatal failure"}}),
        ]
        proc.communicate.return_value = ("\n".join(stdout_lines).encode("utf-8"), b"err details")
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_err)
    res_err = await host_agy_daemon.execute_agy_stream_json("fail prompt")
    assert res_err["returncode"] == 1
    assert "fatal failure - err details" in res_err["stderr"]

    # 3. Result with conversation_id fallback when init event was missing
    async def mock_exec_res_conv(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock()
        stdout_lines = [
            json.dumps({"event": "step_update", "step_update": {"text_delta": "chunk1"}}),
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "conversation_id": "c-fallback", "response": ""},
                }
            ),
        ]
        proc.communicate.return_value = ("\n".join(stdout_lines).encode("utf-8"), b"")
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_res_conv)
    res_conv = await host_agy_daemon.execute_agy_stream_json("prompt")
    assert res_conv["conversation_id"] == "c-fallback"
    assert res_conv["stdout"] == "chunk1"


@pytest.mark.asyncio
async def test_execute_agy_stream_json_real_process_branches(monkeypatch):
    # Tests the line-by-line reading branch (when proc.communicate is None or not AsyncMock)

    # 1. Success with text deltas, init event, and stderr reader
    async def mock_exec_real(*cmd, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stdin.wait_closed = AsyncMock(side_effect=RuntimeError("stdin close error"))

        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(side_effect=[b"stderr line 1\n", b""])

        proc.stdout = MagicMock()
        lines = [
            b"\n",  # empty line
            b"not a valid json\n",
            json.dumps({"event": "init", "conversation_id": "c-stream-1"}).encode("utf-8") + b"\n",
            json.dumps({"event": "step_update", "step_update": {"text_delta": "part A"}}).encode("utf-8") + b"\n",
            json.dumps({"event": "step_update", "step_update": {"text_delta": "part B"}}).encode("utf-8") + b"\n",
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "final response"}}).encode(
                "utf-8"
            )
            + b"\n",
            b"",  # EOF
        ]
        proc.stdout.readline = AsyncMock(side_effect=lines)
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_real)
    res = await host_agy_daemon.execute_agy_stream_json("stream prompt")
    assert res["stdout"] == "final response"
    assert res["conversation_id"] == "c-stream-1"
    assert "stderr line 1" in res["stderr"]

    # 2. Tool interception in real process mode with proc.wait raising Exception
    async def mock_exec_tool_real(*cmd, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stdin.wait_closed = AsyncMock()

        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(return_value=b"")

        proc.stdout = MagicMock()
        tool_event = (
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {"parameters": {"CommandLine": "ls"}},
                    },
                }
            ).encode("utf-8")
            + b"\n"
        )
        proc.stdout.readline = AsyncMock(side_effect=[tool_event, b""])
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=RuntimeError("wait after kill error"))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_tool_real)
    tools = [{"type": "function", "function": {"name": "terminal"}}]
    res_tool = await host_agy_daemon.execute_agy_stream_json("ls", tools=tools, intercept_tools=True)
    assert res_tool["returncode"] == 0
    assert len(res_tool["tool_calls"]) == 1

    # 3. Result ERROR event in real process mode
    async def mock_exec_err_real(*cmd, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.returncode = 1
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(side_effect=[b"stderr details", b""])
        proc.stdout = MagicMock()
        err_event = (
            json.dumps({"event": "result", "result": {"status": "ERROR", "error": "agy real error"}}).encode("utf-8")
            + b"\n"
        )
        proc.stdout.readline = AsyncMock(side_effect=[err_event, b""])
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_err_real)
    res_err = await host_agy_daemon.execute_agy_stream_json("fail prompt")
    assert res_err["returncode"] == 1
    assert "agy real error" in res_err["stderr"]

    # 4. TimeoutError with proc.kill and proc.wait raising Exception
    async def mock_exec_timeout_real(*cmd, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(return_value=b"")
        proc.stdout = MagicMock()

        async def slow_readline():
            await asyncio.sleep(5)
            return b""

        proc.stdout.readline = slow_readline
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=RuntimeError("wait fail on timeout"))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_timeout_real)
    res_timeout = await host_agy_daemon.execute_agy_stream_json("timeout prompt", timeout=0.01)
    assert res_timeout["returncode"] == -1
    assert "Execution timed out" in res_timeout["stderr"]

    # 5. Generic Exception during execution
    async def mock_exec_generic_real(*cmd, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(return_value=b"")
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=RuntimeError("generic crash"))
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=RuntimeError("wait fail"))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_generic_real)
    res_crash = await host_agy_daemon.execute_agy_stream_json("crash prompt")
    assert res_crash["returncode"] == -1
    assert "generic crash" in res_crash["stderr"]


def test_format_tools_instruction_branches():
    # 1. Empty / non-list tools
    assert host_agy_daemon.format_tools_instruction(None) == ""
    assert host_agy_daemon.format_tools_instruction("not-a-list") == ""

    # 2. json.dumps failure fallback
    class Unserializable:
        pass

    unserializable_tools = [Unserializable()]
    instr = host_agy_daemon.format_tools_instruction(unserializable_tools, is_sse_mode=False)
    assert "# Available Tools" in instr

    # 3. is_sse_mode True
    tools = [{"name": "test_tool"}]
    instr_sse = host_agy_daemon.format_tools_instruction(tools, is_sse_mode=True)
    assert "# Available Client Tools" in instr_sse


def test_map_native_tool_call_branches():
    # 1. client_tools parsing variations (function dict, dict with name, invalid)
    client_tools = [
        {"function": {"name": "bash"}},
        {"name": "exec"},
        {"invalid": True},
        "not-a-dict",
    ]
    # run_command -> bash
    c1 = host_agy_daemon.map_native_tool_call("run_command", {"CommandLine": "pwd"}, client_tools)
    assert c1["function"]["name"] == "bash"
    assert json.loads(c1["function"]["arguments"]) == {"command": "pwd"}

    # run_command -> exec (when bash not in list)
    c2 = host_agy_daemon.map_native_tool_call("run_command", {"command": "uptime"}, [{"function": {"name": "exec"}}])
    assert c2["function"]["name"] == "exec"

    # run_command -> run_command (when run_command in client tools)
    c3 = host_agy_daemon.map_native_tool_call("run_command", {"command": "df"}, [{"function": {"name": "run_command"}}])
    assert c3["function"]["name"] == "run_command"

    # run_command -> single client tool
    c4 = host_agy_daemon.map_native_tool_call("run_command", {"command": "top"}, [{"function": {"name": "custom_cli"}}])
    assert c4["function"]["name"] == "custom_cli"

    # view_file / read_file -> read_file
    c5 = host_agy_daemon.map_native_tool_call(
        "view_file", {"AbsolutePath": "/a/b"}, [{"function": {"name": "read_file"}}]
    )
    assert c5["function"]["name"] == "read_file"

    # view_file / read_file -> view_file
    c6 = host_agy_daemon.map_native_tool_call("read_file", {"path": "/a/b"}, [{"function": {"name": "view_file"}}])
    assert c6["function"]["name"] == "view_file"

    # view_file -> single client tool
    c7 = host_agy_daemon.map_native_tool_call("view_file", {"path": "/a/b"}, [{"function": {"name": "fs_reader"}}])
    assert c7["function"]["name"] == "fs_reader"

    # other tool with single client tool
    c8 = host_agy_daemon.map_native_tool_call("search_code", {"q": "main"}, [{"function": {"name": "code_search"}}])
    assert c8["function"]["name"] == "code_search"

    # ct with function not dict, falls back to ct.get("name")
    c8b = host_agy_daemon.map_native_tool_call(
        "run_command", {"command": "top"}, [{"function": None, "name": "custom_cli"}]
    )
    assert c8b["function"]["name"] == "custom_cli"

    # mapped_args is not dict or list
    c9 = host_agy_daemon.map_native_tool_call(
        "custom", "scalar_arg", [{"function": {"name": "tool1"}}, {"function": {"name": "tool2"}}]
    )
    assert c9["function"]["arguments"] == "scalar_arg"


def test_extract_reasoning_effort_branches():
    assert host_agy_daemon.extract_reasoning_effort(None) is None
    assert host_agy_daemon.extract_reasoning_effort({}) is None
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": ""}) is None
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": "HIGH"}) == "high"
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": "med"}) == "medium"
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": "minimal"}) == "low"
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": "disabled"}) == "low"
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": "custom_tier"}) == "custom_tier"
    assert host_agy_daemon.extract_reasoning_effort({"extra_body": {"reasoning": {"effort": "high"}}}) == "high"
    assert host_agy_daemon.extract_reasoning_effort({"extra_body": {"reasoning_effort": "low"}}) == "low"
    assert host_agy_daemon.extract_reasoning_effort({"reasoning": {"effort": "medium"}}) == "medium"
    assert host_agy_daemon.extract_reasoning_effort({"reasoning_effort": {"effort": "high"}}) == "high"


def test_parse_tool_calls_from_text_branches():
    # 1. Empty text
    assert host_agy_daemon.parse_tool_calls_from_text("") == ("", [])

    # 2. Invalid JSON inside <tool_call> returns raw block
    raw_invalid = "<tool_call>invalid json</tool_call>"
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text(raw_invalid)
    assert raw_invalid in cleaned
    assert calls == []

    # 3. Fallback: text is JSON with name and parameters
    json_text = json.dumps({"name": "fetch_url", "parameters": {"url": "https://example.com"}})
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text(json_text)
    assert cleaned == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "fetch_url"

    # 4. Fallback: text is JSON with tool_calls list
    json_list_text = json.dumps(
        {
            "tool_calls": [
                {"name": "call1", "arguments": {"x": 1}},
                {"name": "call2", "arguments": "scalar"},
            ]
        }
    )
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text(json_list_text)
    assert cleaned == ""
    assert len(calls) == 2
    assert calls[0]["function"]["name"] == "call1"
    assert calls[1]["function"]["name"] == "call2"

    # 5. Fallback: text is valid JSON but neither name nor tool_calls
    other_json = json.dumps({"key": "value"})
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text(other_json)
    assert cleaned == other_json
    assert calls == []


def test_extract_prompt_from_messages_branches():
    # 1. Empty or non-list
    assert host_agy_daemon.extract_prompt_from_messages([]) == ""
    assert host_agy_daemon.extract_prompt_from_messages(None) == ""

    # 2. Message list with non-dict items, string content blocks, empty content, tool_calls, tool_call_id
    messages = [
        "not-a-dict",
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": ["Text block 1", {"type": "text", "text": "Text block 2"}]},
        {
            "role": "assistant",
            "content": "Checking...",
            "tool_calls": [{"function": {"name": "cmd", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_123", "content": "Command finished successfully."},
        {"role": "tool", "content": "Second tool output without id."},
        {"role": "user", "content": ""},  # empty content to skip
    ]
    prompt = host_agy_daemon.extract_prompt_from_messages(messages)
    assert "System: You are a helpful assistant." in prompt
    assert "Text block 1" in prompt
    assert "Text block 2" in prompt
    assert "[Tool Call: cmd({})]" in prompt
    assert "[Tool Call ID: call_123]" in prompt
    assert "Second tool output without id." in prompt

    # 3. Prompt starting with System: and tools provided
    prompt_with_tools = host_agy_daemon.extract_prompt_from_messages(
        [{"role": "system", "content": "Initial system"}, {"role": "user", "content": "Hi"}],
        tools=[{"name": "tool1"}],
    )
    assert "# Available Tools" in prompt_with_tools
    assert prompt_with_tools.startswith("System: # Available Tools")

    # 4. Prompt NOT starting with System: and tools provided
    prompt_no_sys_tools = host_agy_daemon.extract_prompt_from_messages(
        [{"role": "user", "content": "Hi only"}],
        tools=[{"name": "tool1"}],
    )
    assert prompt_no_sys_tools.startswith("System: # Available Tools")

    # 5. Prompt NOT starting with System: and NO tools
    prompt_no_sys_no_tools = host_agy_daemon.extract_prompt_from_messages(
        [{"role": "user", "content": "Hi only"}],
        tools=None,
    )
    assert prompt_no_sys_no_tools.startswith("System: # Execution Guidelines")


def test_daemon_handler_do_get_branches(monkeypatch):
    # 1. GET /health
    h_health = DummyHandler(method="GET", path="/health")
    h_health.do_GET()
    assert h_health.status_code == 200
    res = json.loads(h_health.wfile.getvalue().decode())
    assert res["status"] == "ok"

    # 2. GET /run
    h_run = DummyHandler(method="GET", path="/run")
    h_run.do_GET()
    assert h_run.status_code == 200

    # 3. GET /usage with execute_agy_print raising Exception
    async def mock_print_err(*args, **kwargs):
        raise RuntimeError("quota query failed")

    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print_err)
    h_usage = DummyHandler(method="GET", path="/usage")
    h_usage.do_GET()
    assert h_usage.status_code == 200
    assert "quota query failed" in json.loads(h_usage.wfile.getvalue().decode())["error"]

    # 4. GET /models with subprocess.run raising Exception
    def mock_subp_err(*args, **kwargs):
        raise OSError("models binary missing")

    monkeypatch.setattr(subprocess, "run", mock_subp_err)
    h_models = DummyHandler(method="GET", path="/models")
    h_models.do_GET()
    assert h_models.status_code == 200
    assert json.loads(h_models.wfile.getvalue().decode())["status"] == "error"

    # 5. GET /unknown -> 404
    h_404 = DummyHandler(method="GET", path="/unknown")
    h_404.do_GET()
    assert h_404.status_code == 404


def test_daemon_handler_do_post_branches(monkeypatch):
    # 1. POST /unknown -> 404
    h_404 = DummyHandler(method="POST", path="/unknown_post", body=b"{}")
    h_404.do_POST()
    assert h_404.status_code == 404

    # 2. POST with invalid JSON -> 400
    h_400 = DummyHandler(method="POST", path="/run", body=b"invalid json")
    h_400.do_POST()
    assert h_400.status_code == 400
    assert "Invalid JSON payload" in json.loads(h_400.wfile.getvalue().decode())["error"]

    # 3. POST /usage with custom model and failure
    async def mock_print_err(*args, **kwargs):
        raise RuntimeError("post usage failed")

    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print_err)
    h_usage_err = DummyHandler(method="POST", path="/usage", body=json.dumps({"model": "gpt-oss-120b-medium"}))
    h_usage_err.do_POST()
    assert h_usage_err.status_code == 200
    assert "post usage failed" in json.loads(h_usage_err.wfile.getvalue().decode())["error"]

    # 4. POST /usage success
    async def mock_print_ok(*args, **kwargs):
        return {"returncode": 0, "stdout": "Category Limit 10 2h", "stderr": ""}

    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print_ok)
    h_usage_ok = DummyHandler(method="POST", path="/usage", body=json.dumps({"model": "custom-model"}))
    h_usage_ok.do_POST()
    assert h_usage_ok.status_code == 200
    assert len(json.loads(h_usage_ok.wfile.getvalue().decode())["quotas"]) == 1

    # 5. POST /run with stream=True where self.wfile.write raises BrokenPipeError during token streaming
    async def mock_exec_stream(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_stream)

    read_calls_5 = 0

    def mock_read_5(fd, n):
        nonlocal read_calls_5
        read_calls_5 += 1
        if read_calls_5 == 1:
            return b"data\r\n"
        return b""

    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read_5)

    h_stream = DummyHandler(method="POST", path="/run", body=json.dumps({"prompt": "hi", "stream": True}))
    h_stream.wfile = MagicMock()

    def mock_write_stream(payload):
        if b"token" in payload:
            raise BrokenPipeError("pipe broken")

    h_stream.wfile.write = mock_write_stream
    h_stream.do_POST()

    # 6. POST /run with stream=True where os.close(master_fd) and proc.wait raise OSError
    import pty

    target_master_fd = None
    real_openpty = pty.openpty
    real_close = os.close

    def mock_openpty():
        nonlocal target_master_fd
        m, s = real_openpty()
        target_master_fd = m
        return m, s

    def mock_close_err(fd):
        if target_master_fd is not None and fd == target_master_fd:
            raise OSError("master fd close error")
        return real_close(fd)

    read_calls_6 = 0

    def mock_read_6(fd, n):
        nonlocal read_calls_6
        read_calls_6 += 1
        if read_calls_6 == 1:
            return b"data\r\n"
        return b""

    monkeypatch.setattr(pty, "openpty", mock_openpty)
    monkeypatch.setattr(host_agy_daemon.os, "close", mock_close_err)
    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read_6)
    h_stream2 = DummyHandler(method="POST", path="/run", body=json.dumps({"prompt": "hi", "stream": True}))
    h_stream2.do_POST()


def test_daemon_chat_completions_model_resolution(monkeypatch):
    captured = {}

    async def mock_stream_json(**kwargs):
        captured.update(kwargs)
        return {"returncode": 0, "stdout": "response", "stderr": "", "tool_calls": []}

    monkeypatch.setattr(host_agy_daemon, "execute_agy_stream_json", mock_stream_json)

    models_to_test = [
        ("claude-opus-4.6", None, "claude-opus-4-6-thinking"),
        ("claude-sonnet-4.6", None, "claude-sonnet-4-6"),
        ("gptoss-120b", None, "gpt-oss-120b-medium"),
        ("gpt_oss_test", None, "gpt-oss-120b-medium"),
        ("gemini-3.8-flash-high", None, "gemini-3.8-flash-high"),
        ("gemini-3.8-flash-medium", None, "gemini-3.8-flash-medium"),
        ("gemini-3.8-flash-low", None, "gemini-3.8-flash-low"),
        ("gemini-3.1-pro", "high", "gemini-3.1-pro-high"),
        ("gemini-3.1-pro", "low", "gemini-3.1-pro-low"),
        ("gemini-3.8-flash", "high", "gemini-3.8-flash-high"),
        ("gemini-3.8-flash", "medium", "gemini-3.8-flash-medium"),
        ("gemini-3.8-flash", "low", "gemini-3.8-flash-low"),
    ]

    for model, effort, expected_override in models_to_test:
        captured.clear()
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        if effort:
            body["reasoning_effort"] = effort
        h = DummyHandler(method="POST", path="/v1/chat/completions", body=json.dumps(body))
        h.handle_chat_completions(body)
        assert captured["model_override"] == expected_override


def test_daemon_chat_completions_conversation_id_filtering(monkeypatch):
    captured = {}

    async def mock_stream_json(**kwargs):
        captured.update(kwargs)
        return {"returncode": 0, "stdout": "ok", "stderr": "", "tool_calls": []}

    monkeypatch.setattr(host_agy_daemon, "execute_agy_stream_json", mock_stream_json)

    # 1. sess- ID should be ignored (None)
    h1 = DummyHandler()
    h1.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "conversation_id": "sess-temporary-123"}
    )
    assert captured["conversation_id"] is None

    # 2. Real conversation ID preserved
    h2 = DummyHandler()
    h2.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "conversation_id": "conv-persistent-456"}
    )
    assert captured["conversation_id"] == "conv-persistent-456"


def test_daemon_chat_completions_non_streaming_errors_and_tool_calls(monkeypatch):
    # 1. Quota error (429)
    async def mock_err_quota(**kwargs):
        return {"returncode": 1, "stderr": "Error: resource_exhausted rate limit exceeded"}

    monkeypatch.setattr(host_agy_daemon, "execute_agy_stream_json", mock_err_quota)
    h_quota = DummyHandler()
    h_quota.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}]})
    assert h_quota.status_code == 429
    assert "rate_limit_error" in json.loads(h_quota.wfile.getvalue().decode())["error"]["type"]

    # 2. General error (502)
    async def mock_err_gen(**kwargs):
        return {"returncode": 1, "stderr": "fatal daemon error"}

    monkeypatch.setattr(host_agy_daemon, "execute_agy_stream_json", mock_err_gen)
    h_gen = DummyHandler()
    h_gen.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}]})
    assert h_gen.status_code == 502
    assert "api_error" in json.loads(h_gen.wfile.getvalue().decode())["error"]["type"]

    # 3. Intercepted tool call
    async def mock_tool_call(**kwargs):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "test", "arguments": "{}"}}],
        }

    monkeypatch.setattr(host_agy_daemon, "execute_agy_stream_json", mock_tool_call)
    h_tool = DummyHandler()
    h_tool.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}]})
    assert h_tool.status_code == 200
    res = json.loads(h_tool.wfile.getvalue().decode())
    assert res["choices"][0]["finish_reason"] == "tool_calls"


def test_daemon_chat_completions_streaming_branches(monkeypatch):
    # 1. Subprocess spawn error (502 error chunk)
    async def mock_spawn_err(*cmd, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_spawn_err)
    h_spawn_err = DummyHandler()
    h_spawn_err.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}], "stream": True})
    output = h_spawn_err.wfile.getvalue().decode()
    assert "Failed to spawn agy process" in output

    # 2. SSE Mode with tool output exceeding 800 characters
    long_output = "x" * 900
    tool_event_long = (
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "tool_info": {"parameters": {"path": "/etc/hosts"}, "output": long_output},
                },
            }
        ).encode("utf-8")
        + b"\n"
    )

    async def mock_exec_sse(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock(side_effect=RuntimeError("stdin drain err"))
        proc.stdin.close = MagicMock()
        proc.stdin.wait_closed = AsyncMock()

        proc.stderr = MagicMock()
        proc.stderr.read = MagicMock(return_value=b"some stderr details")

        proc.stdout = MagicMock()
        lines = [
            b"   \n",  # whitespace line
            b"invalid json\n",
            tool_event_long,
            json.dumps({"event": "step_update", "step_update": {"text_delta": "done"}}).encode("utf-8") + b"\n",
            json.dumps({"event": "result", "result": {"status": "SUCCESS"}}).encode("utf-8") + b"\n",
            b"",
        ]
        proc.stdout.readline = AsyncMock(side_effect=lines)
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_sse)
    h_sse = DummyHandler()
    h_sse.handle_chat_completions(
        {
            "model": "llm-routing-agy-sse",
            "messages": [{"role": "user", "content": "read file"}],
            "tools": [{"name": "view_file"}],
            "stream": True,
        }
    )
    sse_out = h_sse.wfile.getvalue().decode()
    assert "..." in sse_out

    # 3. Streaming timeout and stderr concatenation
    async def mock_exec_stream_timeout(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = -1
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"err context")
        proc.stdout = MagicMock()

        async def slow_read():
            await asyncio.sleep(5)
            return b""

        proc.stdout.readline = slow_read
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_stream_timeout)
    h_timeout = DummyHandler()
    h_timeout.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "stream": True, "timeout": 0.01}
    )
    timeout_out = h_timeout.wfile.getvalue().decode()
    assert "Execution timed out" in timeout_out
    assert "err context" in timeout_out

    # 4. Streaming quota error payload (429)
    async def mock_exec_quota(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.stdout = MagicMock()
        err_res = (
            json.dumps(
                {"event": "result", "result": {"status": "ERROR", "error": "rate_limit: 429 quota exhausted"}}
            ).encode("utf-8")
            + b"\n"
        )
        proc.stdout.readline = AsyncMock(side_effect=[err_res, b""])
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_quota)
    h_quota_stream = DummyHandler()
    h_quota_stream.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}], "stream": True})
    quota_stream_out = h_quota_stream.wfile.getvalue().decode()
    assert "rate_limit_error" in quota_stream_out

    # 5. Non-zero exit code without stream_error and without intercepted tool
    async def mock_exec_nonzero(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 127
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"command not found")
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=127)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_nonzero)
    h_nonzero = DummyHandler()
    h_nonzero.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}], "stream": True})
    nonzero_out = h_nonzero.wfile.getvalue().decode()
    assert "command not found" in nonzero_out

    # 6. Stream with tools not in SSE mode generating tool_calls from text
    async def mock_exec_text_tools(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.stdout = MagicMock()
        tc_delta = (
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"text_delta": '<tool_call>{"name": "fetch", "arguments": {"id": 1}}</tool_call>'},
                }
            ).encode("utf-8")
            + b"\n"
        )
        proc.stdout.readline = AsyncMock(side_effect=[tc_delta, b""])
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_text_tools)
    h_text_tools = DummyHandler()
    h_text_tools.handle_chat_completions(
        {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "fetch"}],
            "tools": [{"name": "fetch"}],
            "stream": True,
        }
    )
    text_tools_out = h_text_tools.wfile.getvalue().decode()
    assert "tool_calls" in text_tools_out
    assert "fetch" in text_tools_out


def test_daemon_chat_completions_safe_write_failures(monkeypatch):
    # Tests all the safe_write return False early exit branches

    async def base_proc():
        proc = MagicMock()
        proc.returncode = 0
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.stdout = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    # 1. safe_write fail in SSE mode tool update
    async def mock_sse_fail(*cmd, **kwargs):
        p = await base_proc()
        p.stdout.readline = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"step_type": "tool", "tool_name": "t1", "tool_info": {}},
                    }
                ).encode("utf-8")
                + b"\n",
                b"",
            ]
        )
        return p

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_sse_fail)
    h1 = DummyHandler()
    h1.wfile.write = MagicMock(side_effect=BrokenPipeError("pipe fail"))
    h1.handle_chat_completions(
        {"model": "agy-sse", "messages": [{"role": "user", "content": "hi"}], "stream": True, "tools": [{"name": "t1"}]}
    )

    # 2. safe_write fail in text delta streaming
    async def mock_delta_fail(*cmd, **kwargs):
        p = await base_proc()
        p.stdout.readline = AsyncMock(
            side_effect=[
                json.dumps({"event": "step_update", "step_update": {"text_delta": "some token"}}).encode("utf-8")
                + b"\n",
                b"",
            ]
        )
        return p

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_delta_fail)
    h2 = DummyHandler()
    h2.wfile.write = MagicMock(side_effect=BrokenPipeError("pipe fail"))
    h2.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}], "stream": True})

    # 3. safe_write fail in intercepted tool call (tool chunk & finish chunk)
    async def mock_intercept_fail(*cmd, **kwargs):
        p = await base_proc()
        p.stdout.readline = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "tool",
                            "tool_name": "run_command",
                            "tool_info": {"parameters": {"CommandLine": "ls"}},
                        },
                    }
                ).encode("utf-8")
                + b"\n",
                b"",
            ]
        )
        return p

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_intercept_fail)

    # 3a. fail on tool chunk
    h3a = DummyHandler()
    h3a.wfile.write = MagicMock(side_effect=BrokenPipeError("fail"))
    h3a.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "terminal"}], "stream": True}
    )

    # 3b. fail on finish chunk
    h3b = DummyHandler()
    write_calls = 0

    def mock_write_3b(payload):
        nonlocal write_calls
        write_calls += 1
        if write_calls > 1:
            raise BrokenPipeError("fail finish")

    h3b.wfile.write = mock_write_3b
    h3b.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "terminal"}], "stream": True}
    )

    # 4. safe_write fail in tools mode (text parsed tool calls)
    async def mock_text_tools_fail(*cmd, **kwargs):
        p = await base_proc()
        p.stdout.readline = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"text_delta": '<tool_call>{"name": "fetch", "arguments": {}}</tool_call>'},
                    }
                ).encode("utf-8")
                + b"\n",
                b"",
            ]
        )
        return p

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_text_tools_fail)

    # 4a. fail on tool chunk
    h4a = DummyHandler()
    h4a.wfile.write = MagicMock(side_effect=BrokenPipeError("fail"))
    h4a.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "fetch"}], "stream": True}
    )

    # 4b. fail on finish chunk
    h4b = DummyHandler()
    write_calls_4b = 0

    def mock_write_4b(payload):
        nonlocal write_calls_4b
        write_calls_4b += 1
        if write_calls_4b > 1:
            raise BrokenPipeError("fail finish")

    h4b.wfile.write = mock_write_4b
    h4b.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "fetch"}], "stream": True}
    )

    # 5. safe_write fail in tools mode when no tool calls found (text chunk & finish chunk)
    async def mock_tools_no_call_fail(*cmd, **kwargs):
        p = await base_proc()
        p.stdout.readline = AsyncMock(
            side_effect=[
                json.dumps({"event": "step_update", "step_update": {"text_delta": "Just conversational text"}}).encode(
                    "utf-8"
                )
                + b"\n",
                b"",
            ]
        )
        return p

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_tools_no_call_fail)

    # 5a. fail on text chunk
    h5a = DummyHandler()
    h5a.wfile.write = MagicMock(side_effect=BrokenPipeError("fail text"))
    h5a.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "fetch"}], "stream": True}
    )

    # 5b. fail on finish chunk
    h5b = DummyHandler()
    write_calls_5b = 0

    def mock_write_5b(payload):
        nonlocal write_calls_5b
        write_calls_5b += 1
        if write_calls_5b > 1:
            raise BrokenPipeError("fail finish")

    h5b.wfile.write = mock_write_5b
    h5b.handle_chat_completions(
        {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "fetch"}], "stream": True}
    )

    # 6. safe_write fail in fallback text mode (when has_streamed_deltas is False and accumulated_chunks has response)
    async def mock_fallback_fail(*cmd, **kwargs):
        p = await base_proc()
        p.stdout.readline = AsyncMock(
            side_effect=[
                json.dumps(
                    {"event": "result", "result": {"status": "SUCCESS", "response": "result fallback text"}}
                ).encode("utf-8")
                + b"\n",
                b"",
            ]
        )
        return p

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_fallback_fail)

    # 6a. fail on fallback chunk
    h6a = DummyHandler()
    h6a.wfile.write = MagicMock(side_effect=BrokenPipeError("fail fallback"))
    h6a.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}], "stream": True})

    # 6b. fail on finish chunk
    h6b = DummyHandler()
    write_calls_6b = 0

    def mock_write_6b(payload):
        nonlocal write_calls_6b
        write_calls_6b += 1
        if write_calls_6b > 1:
            raise BrokenPipeError("fail finish")

    h6b.wfile.write = mock_write_6b
    h6b.handle_chat_completions({"messages": [{"role": "user", "content": "hi"}], "stream": True})


def test_remaining_edge_branches(monkeypatch, tmp_path):
    # 1. get_auth_status: expiry_val is None (line 77->88)
    token_file = tmp_path / "token_no_expiry.json"
    token_file.write_text(json.dumps({"token": {"access_token": "tok123"}}))
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(token_file))
    res = host_agy_daemon.get_auth_status()
    assert res["expiry_ms"] == 0

    # 2. parse_models_output: line containing only spinner chars (line 145->137)
    res_m = host_agy_daemon.parse_models_output("⠋  \nmodel-ok")
    assert len(res_m) == 1

    # 3. parse_tool_calls_from_text:
    # 3a. item in items not dict or no name (line 630->629)
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text('<tool_call>[123, {"noname": 1}]</tool_call>')
    assert calls == []

    # 3b. monkeypatch json.loads so data is not dict for {} (line 653->687)
    real_loads = json.loads

    def mock_loads(s, *args, **kwargs):
        if s.strip() == "{non_dict}":
            return [1, 2]
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(host_agy_daemon.json, "loads", mock_loads)
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text("{non_dict}")
    assert cleaned == "{non_dict}"

    # 3c. data["tool_calls"] has non-dict or no name (line 670->669)
    tc_json = json.dumps({"tool_calls": ["string_tc", {"no_name": 1}]})
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text(tc_json)
    assert calls == []

    # 3d. text starts with { and ends with } but is invalid json syntax (line 684-685)
    cleaned, calls = host_agy_daemon.parse_tool_calls_from_text("{invalid json syntax}")
    assert cleaned == "{invalid json syntax}"

    # 4. extract_reasoning_effort: extra_body reasoning is not a dict (line 597->600)
    assert host_agy_daemon.extract_reasoning_effort({"extra_body": {"reasoning": "not-a-dict"}}) is None

    # 5. extract_prompt_from_messages:
    # 5a. block is neither text dict nor str (line 706->703)
    # 5b. tool_calls has non-dict (line 715->714)
    msgs = [{"role": "user", "content": [12345], "tool_calls": ["not-a-dict"]}]
    assert host_agy_daemon.extract_prompt_from_messages(msgs) == ""

    # 5c. tools is not a list (truthy non-list) -> tool_instr is "" (line 740->757)
    msgs_real = [{"role": "user", "content": "hello"}]
    p = host_agy_daemon.extract_prompt_from_messages(msgs_real, tools="not-a-list")
    assert p == "User: hello"

    # 6. map_native_tool_call:
    # 6a. client_tools is None (line 525->533)
    res_map = host_agy_daemon.map_native_tool_call("run_command", {}, None)
    assert res_map["function"]["name"] == "run_command"
    # 6b. view_file with len(client_tool_names) == 2 not matching read/view (line 561->567)
    res_vf = host_agy_daemon.map_native_tool_call(
        "view_file", {}, [{"function": {"name": "t1"}}, {"function": {"name": "t2"}}]
    )
    assert res_vf["function"]["name"] == "view_file"


@pytest.mark.asyncio
async def test_execute_agy_stream_json_mock_unmatched_event(monkeypatch):
    # ev == "ping" in mock branch (line 299->279)
    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(json.dumps({"event": "ping"}).encode("utf-8"), b""))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    res = await host_agy_daemon.execute_agy_stream_json("prompt")
    assert res["stdout"] == ""


@pytest.mark.asyncio
async def test_execute_agy_stream_json_real_process_more_branches(monkeypatch):
    # 1. proc.stdin is None (line 340->352), proc.stderr is None (line 360->exit)
    # step_update without text_delta (line 408->370)
    # event is ping (line 410->370)
    # result without res_conv_id (line 428)
    # proc.wait is not coroutine (line 432->434)
    async def mock_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.stdin = None
        proc.stderr = None
        proc.stdout = MagicMock()
        lines = [
            json.dumps({"event": "step_update", "step_update": {"step_type": "note"}}).encode("utf-8") + b"\n",
            json.dumps({"event": "ping"}).encode("utf-8") + b"\n",
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "conversation_id": "c-res-only"}}).encode(
                "utf-8"
            )
            + b"\n",
            b"",
        ]
        proc.stdout.readline = AsyncMock(side_effect=lines)
        proc.wait = MagicMock()  # not a coroutine
        proc.returncode = 0
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    res = await host_agy_daemon.execute_agy_stream_json("prompt")
    assert res["conversation_id"] == "c-res-only"

    # 2. proc.stdin has write but no close (line 344->346)
    async def mock_exec_no_close(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.stdin = MagicMock()
        del proc.stdin.close
        del proc.stdin.wait_closed
        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(return_value=b"")
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_no_close)
    await host_agy_daemon.execute_agy_stream_json("prompt")

    # 3. Tool interception without proc.kill (line 391->398) or proc.wait not coroutine (line 394->398)
    async def mock_exec_tool_nokill(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(return_value=b"")
        del proc.kill
        tool_ev = (
            json.dumps(
                {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "bash", "tool_info": {}}}
            ).encode("utf-8")
            + b"\n"
        )
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[tool_ev, b""])
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_tool_nokill)
    res_tool = await host_agy_daemon.execute_agy_stream_json(
        "prompt", tools=[{"function": {"name": "bash"}}], intercept_tools=True
    )
    assert res_tool["returncode"] == 0

    # 3b. Tool interception with proc.kill but proc.wait not a coroutine (line 394->398)
    async def mock_exec_tool_wait_not_coro(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline = AsyncMock(return_value=b"")
        proc.kill = MagicMock()
        proc.wait = lambda: None  # not a coroutine function
        tool_ev = (
            json.dumps(
                {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "bash", "tool_info": {}}}
            ).encode("utf-8")
            + b"\n"
        )
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[tool_ev, b""])
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_tool_wait_not_coro)
    res_tool_nc = await host_agy_daemon.execute_agy_stream_json(
        "prompt", tools=[{"function": {"name": "bash"}}], intercept_tools=True
    )
    assert res_tool_nc["returncode"] == 0

    # 4. stderr_task cancelled triggers lines 436-437
    async def mock_exec_cancel_stderr(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = None
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()

        async def hang_stderr():
            await asyncio.sleep(10)
            return b""

        proc.stderr.readline = hang_stderr
        proc.stdout = MagicMock()

        async def read_stdout_and_cancel_others():
            cur = asyncio.current_task()
            for t in asyncio.all_tasks():
                if t is not cur:
                    t.cancel()
            return b""

        proc.stdout.readline = read_stdout_and_cancel_others
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_cancel_stderr)
    await host_agy_daemon.execute_agy_stream_json("prompt")

    # 5. create_subprocess_exec raises TimeoutError -> proc is None (line 451->457)
    async def mock_exec_to(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_to)
    res_to = await host_agy_daemon.execute_agy_stream_json("prompt")
    assert res_to["returncode"] == -1

    # 6. create_subprocess_exec raises RuntimeError -> proc is None (line 466->472)
    async def mock_exec_rt(*args, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_rt)
    res_rt = await host_agy_daemon.execute_agy_stream_json("prompt")
    assert res_rt["returncode"] == -1


def test_run_openai_stream_more_branches(monkeypatch):
    # 1. conversation_id passed (line 1115)
    # 2. text_delta streamed successfully (line 1246->1167)
    # 3. generic readline exception (lines 1261-1262)
    # 4. finally: proc.stderr is None (line 1265->1287), proc.returncode is None kill error (lines 1291-1292)
    async def mock_exec_generic_err(*cmd, **kwargs):
        assert "--conversation" in cmd
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        delta_chunk = (
            json.dumps({"event": "step_update", "step_update": {"text_delta": "hello"}}).encode("utf-8") + b"\n"
        )
        proc.stdout.readline = AsyncMock(side_effect=[delta_chunk, RuntimeError("readline crash")])
        proc.stderr = None
        proc.returncode = None
        proc.kill = MagicMock(side_effect=RuntimeError("kill crash"))
        proc.wait = AsyncMock(side_effect=RuntimeError("wait crash"))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_generic_err)
    h = DummyHandler()
    h.handle_chat_completions(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": "conv-stream-123",
            "stream": True,
        }
    )
    out = h.wfile.getvalue().decode()
    assert "readline crash" in out

    # 5. Tool interception proc.wait raises exception (lines 1193-1194)
    async def mock_exec_tool_wait_err(*cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        tool_ev = (
            json.dumps(
                {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "terminal", "tool_info": {}}}
            ).encode("utf-8")
            + b"\n"
        )
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[tool_ev, b""])
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=[RuntimeError("wait fail"), 0])
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_tool_wait_err)
    h_tool = DummyHandler()
    h_tool.handle_chat_completions(
        {
            "messages": [{"role": "user", "content": "run cmd"}],
            "tools": [{"function": {"name": "terminal"}}],
            "stream": True,
        }
    )
    assert "tool_calls" in h_tool.wfile.getvalue().decode()

    # 6. finally: read_fn is not callable (line 1268->1287)
    # 7. finally: read_fn raises exception (lines 1285-1286)
    async def mock_exec_stderr_err(*cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.stderr = MagicMock()
        proc.stderr.read = MagicMock(side_effect=RuntimeError("stderr read crash"))
        proc.returncode = 1
        proc.wait = AsyncMock(return_value=1)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_stderr_err)
    h_err = DummyHandler()
    h_err.handle_chat_completions(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )
    assert "agy exited with returncode 1" in h_err.wfile.getvalue().decode()

    # 8. finally: stream_error is None, proc.returncode != 0, stderr_text present -> stream_error = stderr_text (line 1284)
    async def mock_exec_stderr_fallback(*cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"fatal stderr only")
        proc.returncode = 2
        proc.wait = AsyncMock(return_value=2)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_stderr_fallback)
    h_fb = DummyHandler()
    h_fb.handle_chat_completions(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )
    assert "fatal stderr only" in h_fb.wfile.getvalue().decode()

    # 9. Multiple text deltas and init event to test branch 1246->1167
    async def mock_exec_multi_delta(*cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        init_chunk = json.dumps({"event": "init", "conversation_id": "conv-1"}).encode("utf-8") + b"\n"
        delta1 = json.dumps({"event": "step_update", "step_update": {"text_delta": "hello "}}).encode("utf-8") + b"\n"
        delta2 = json.dumps({"event": "step_update", "step_update": {"text_delta": "world"}}).encode("utf-8") + b"\n"
        proc.stdout.readline = AsyncMock(side_effect=[init_chunk, delta1, delta2, b""])
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_multi_delta)
    h_multi = DummyHandler()
    h_multi.handle_chat_completions(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )
    assert "hello " in h_multi.wfile.getvalue().decode()
    assert "world" in h_multi.wfile.getvalue().decode()

    # 10. read_fn not callable (line 1268->1287)
    async def mock_exec_not_callable_read(*cmd, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.stderr = MagicMock()
        proc.stderr.read = "not callable"
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_not_callable_read)
    h_nc = DummyHandler()
    h_nc.handle_chat_completions(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )


def test_post_run_stream_proc_kill_wait_error(monkeypatch):
    # Lines 1012-1013 in do_POST run_stream finally: proc.returncode is None, kill/wait raises
    async def mock_exec_stream_finally_err(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = None
        proc.wait = AsyncMock(side_effect=RuntimeError("finally wait fail"))
        proc.kill = MagicMock(side_effect=RuntimeError("finally kill fail"))
        return proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec_stream_finally_err)
    monkeypatch.setattr(host_agy_daemon.os, "read", lambda fd, n: b"")
    h_fin = DummyHandler(method="POST", path="/run", body=json.dumps({"prompt": "hi", "stream": True}))
    h_fin.do_POST()
