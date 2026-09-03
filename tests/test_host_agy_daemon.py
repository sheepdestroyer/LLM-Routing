import asyncio
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from unittest.mock import AsyncMock

import pytest

import host_agy_daemon

def make_run_request(daemon_server, payload):
    return urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

@pytest.fixture
def daemon_server(monkeypatch):
    port = find_free_port()
    monkeypatch.setattr(host_agy_daemon, "PORT", port)

    server = host_agy_daemon.ThreadingHTTPServer(('127.0.0.1', port), host_agy_daemon.AgyDaemonHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)

def test_get_last_conversation_id(monkeypatch, tmp_path):
    cache_file = tmp_path / "last_conversations.json"
    cache_file.write_text(json.dumps({"/fake/cwd": "conv_123"}))

    monkeypatch.setattr(host_agy_daemon, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(host_agy_daemon.os, "getcwd", lambda: "/fake/cwd")

    assert host_agy_daemon.get_last_conversation_id() == "conv_123"

    monkeypatch.setattr(host_agy_daemon.os, "getcwd", lambda: "/other/cwd")
    assert host_agy_daemon.get_last_conversation_id() is None

def test_get_last_conversation_id_no_file(monkeypatch):
    monkeypatch.setattr(host_agy_daemon, "CACHE_FILE", "/does/not/exist.json")
    assert host_agy_daemon.get_last_conversation_id() is None

def test_get_last_conversation_id_invalid_json(monkeypatch, tmp_path):
    cache_file = tmp_path / "last_conversations.json"
    cache_file.write_text("invalid json")

    monkeypatch.setattr(host_agy_daemon, "CACHE_FILE", str(cache_file))
    assert host_agy_daemon.get_last_conversation_id() is None

def test_get_last_conversation_id_io_error(monkeypatch):
    monkeypatch.setattr(host_agy_daemon, "CACHE_FILE", "/fake/cache.json")
    monkeypatch.setattr(host_agy_daemon.os.path, "exists", lambda x: True)
    def mock_open_err(*args, **kwargs):
        raise IOError("permission denied")
    monkeypatch.setattr("builtins.open", mock_open_err)
    assert host_agy_daemon.get_last_conversation_id() is None

def test_daemon_post_404(daemon_server):
    req = urllib.request.Request(f"{daemon_server}/invalid", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 404

def test_daemon_post_stream_false(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False, "conversation_id": "conv_abc", "model_override": "gpt-4"})

    captured = {}
    async def mock_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env", {})
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        if "stdout" in kwargs:
            with open(kwargs["stdout"].name, "w") as f:
                f.write("mocked stdout output")
        if "stderr" in kwargs:
            with open(kwargs["stderr"].name, "w") as f:
                f.write("mocked stderr output")

        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: "last_conv_456")

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert captured.get("args") == (host_agy_daemon.AGY_BINARY, "--conversation", "conv_abc", "--print", "test prompt")
    assert captured.get("env", {}).get("CASCADE_DEFAULT_MODEL_OVERRIDE") == "gpt-4"
    assert data["returncode"] == 0
    assert data["stdout"] == "mocked stdout output"
    assert data["stderr"] == "mocked stderr output"
    assert data["conversation_id"] == "last_conv_456"

def test_daemon_post_stream_false_timeout(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False, "timeout": 0.1})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        # Make wait take longer than timeout
        async def slow_wait():
            await asyncio.sleep(0.5)
        mock_proc.wait = slow_wait
        # Make kill synchronous
        mock_proc.kill = lambda: None
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == -1
    assert data["stderr"] == "TIMEOUT"

def test_daemon_post_stream_true(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True, "model_override": "test-model"})

    captured = {}
    async def mock_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env", {})
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: "last_conv_456")

    read_calls = 0
    def mock_read(fd, n):
        nonlocal read_calls
        if read_calls == 0:
            read_calls += 1
            return b"token1\r\n"
        elif read_calls == 1:
            read_calls += 1
            return b"token2\r\n"
        return b""

    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read)

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert captured.get("args") == (host_agy_daemon.AGY_BINARY, "--print", "test prompt")
    assert captured.get("env", {}).get("CASCADE_DEFAULT_MODEL_OVERRIDE") == "test-model"
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"type": "token", "content": "token1\n"}
    assert json.loads(lines[1]) == {"type": "token", "content": "token2\n"}
    assert json.loads(lines[2]) == {"type": "status", "returncode": 0, "conversation_id": "last_conv_456"}

def test_daemon_post_stream_true_exec_error(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True})

    async def mock_exec(*args, **kwargs):
        raise Exception("exec failed")

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": -1, "stderr": "exec failed"}

def test_daemon_post_stream_true_timeout(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True, "timeout": 0.1})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def slow_wait():
            await asyncio.sleep(0.5)
        mock_proc.wait = slow_wait
        # Make kill synchronous
        mock_proc.kill = lambda: None
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: None)

    read_calls = 0
    def mock_read(fd, n):
        nonlocal read_calls
        if read_calls == 0:
            read_calls += 1
            return b"token1\n"
        return b""

    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read)

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 2
    assert json.loads(lines[0]) == {"type": "token", "content": "token1\n"}
    assert json.loads(lines[1]) == {"type": "status", "returncode": -1, "conversation_id": None}

def test_log_message_silenced():
    # Instantiate the class, bypassing BaseHTTPRequestHandler.__init__
    handler = host_agy_daemon.AgyDaemonHandler.__new__(host_agy_daemon.AgyDaemonHandler)
    # Shouldn't raise any error
    handler.log_message("format %s", "arg")

def test_run_server_interrupt(monkeypatch):
    monkeypatch.setattr(host_agy_daemon, "PORT", find_free_port())
    # Mock serve_forever to raise KeyboardInterrupt
    def mock_serve_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(host_agy_daemon.ThreadingHTTPServer, "serve_forever", mock_serve_forever)

    # Track if server_close was called
    close_called = False
    real_close = host_agy_daemon.ThreadingHTTPServer.server_close
    def mock_server_close(self):
        nonlocal close_called
        close_called = True
        real_close(self)

    monkeypatch.setattr(host_agy_daemon.ThreadingHTTPServer, "server_close", mock_server_close)

    # Should not raise exception
    host_agy_daemon.run_server()
    assert close_called

def test_daemon_post_stream_false_no_model_override(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False})

    captured = {}
    async def mock_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon.os.environ, "copy", lambda: {"CASCADE_DEFAULT_MODEL_OVERRIDE": "old-model"})

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert "CASCADE_DEFAULT_MODEL_OVERRIDE" not in captured.get("env", {})
    assert data["returncode"] == 0

def test_daemon_post_stream_true_read_oserror(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    def mock_read(fd, n):
        raise OSError("read error")

    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read)

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "status"

def test_daemon_post_stream_true_timeout_kill_fail(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True, "timeout": 0.1})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def slow_wait():
            await asyncio.sleep(0.5)
        mock_proc.wait = slow_wait
        def mock_kill():
            raise Exception("kill failed")
        mock_proc.kill = mock_kill
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon.os, "read", lambda fd, n: b"")
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: None)

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": -1, "conversation_id": None}

def test_daemon_post_stream_true_wait_exception(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def mock_wait():
            raise Exception("wait failed")
        mock_proc.wait = mock_wait
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon.os, "read", lambda fd, n: b"")
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: None)

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": -1, "conversation_id": None}

def test_daemon_post_stream_false_timeout_kill_fail(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False, "timeout": 0.1})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def slow_wait():
            await asyncio.sleep(0.5)
        mock_proc.wait = slow_wait
        def mock_kill():
            raise Exception("kill failed")
        mock_proc.kill = mock_kill
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == -1
    assert data["stderr"] == "TIMEOUT"

def test_daemon_post_stream_false_wait_exception(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def mock_wait():
            raise Exception("wait failed")
        mock_proc.wait = mock_wait
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == -1

def test_daemon_post_stream_false_file_read_error(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        # Corrupt the temp files to cause read exceptions
        os.unlink(kwargs["stdout"].name)
        os.unlink(kwargs["stderr"].name)

        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == 0
    assert data["stdout"] == ""
    assert data["stderr"] == ""

def test_daemon_post_stream_false_unlink_error(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": False})

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    def mock_unlink(path):
        raise Exception("unlink failed")

    monkeypatch.setattr(host_agy_daemon.os, "unlink", mock_unlink)

    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == 0

def test_daemon_post_stream_true_with_conversation(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True, "conversation_id": "conv_789"})

    captured = {}
    async def mock_exec(*args, **kwargs):
        captured["args"] = args
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: "conv_789")
    monkeypatch.setattr(host_agy_daemon.os, "read", lambda fd, n: b"")

    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert captured.get("args") == (host_agy_daemon.AGY_BINARY, "--conversation", "conv_789", "--print", "test prompt")
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": 0, "conversation_id": "conv_789"}

def test_daemon_post_stream_true_finally_cleanup(daemon_server, monkeypatch):
    req = make_run_request(daemon_server, {"prompt": "test prompt", "stream": True})

    from unittest.mock import MagicMock
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock()
    killed = False
    def mock_kill():
        nonlocal killed
        killed = True
    mock_proc.kill = mock_kill

    async def mock_exec(*args, **kwargs):
        return mock_proc

    closed_fds = []
    real_close = host_agy_daemon.os.close
    def mock_close(fd):
        closed_fds.append(fd)
        try:
            real_close(fd)
        except OSError:
            pass

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon.os, "close", mock_close)
    monkeypatch.setattr(host_agy_daemon.os, "read", lambda fd, n: b"")

    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()

    assert killed is True
    assert len(closed_fds) > 0

def test_parse_usage_output():
    text = """Quota:
Gemini Models          Weekly Limit Remaining     96%   2026-08-20T16:58:06Z
Gemini Models          Five Hour Limit Remaining  98%   2026-08-14T17:58:03Z
Claude and GPT models  Weekly Limit Remaining     100%  2026-08-21T13:37:53Z
Claude and GPT models  Five Hour Limit Remaining  100%  2026-08-14T18:37:53Z"""
    result = host_agy_daemon.parse_usage_output(text)
    assert len(result["quotas"]) == 4
    assert result["quotas"][0]["category"] == "Gemini Models"
    assert result["quotas"][0]["remaining"] == "96%"
    assert result["quotas"][2]["category"] == "Claude and GPT models"
    assert result["quotas"][2]["remaining"] == "100%"

def test_parse_models_output():
    text = """⠋ Fetching available models...
gemini-3.7-flash-high     Gemini 3.7 Flash (High)
claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)
gpt-oss-120b-medium       GPT-OSS 120B (Medium)"""
    models = host_agy_daemon.parse_models_output(text)
    assert len(models) == 3
    assert models[0]["id"] == "gemini-3.7-flash-high"
    assert models[1]["id"] == "claude-sonnet-4-6"
    assert models[2]["id"] == "gpt-oss-120b-medium"
    assert models[2]["name"] == "GPT-OSS 120B (Medium)"

def test_get_auth_status(tmp_path, monkeypatch):
    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text(json.dumps({
        "auth_method": "consumer",
        "token": {
            "access_token": "mock_tok",
            "refresh_token": "mock_ref",
            "expiry": "2026-08-14T15:45:24.092546+02:00"
        }
    }))
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(token_file))

    status = host_agy_daemon.get_auth_status()
    assert status["authenticated"] is True
    assert status["source"] == "cli_token"

    # Test missing token file
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(tmp_path / "nonexistent.json"))
    missing_status = host_agy_daemon.get_auth_status()
    assert missing_status["authenticated"] is False
    assert missing_status["status"] == "missing"

def test_daemon_get_health(daemon_server):
    req = urllib.request.Request(f"{daemon_server}/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert data["status"] == "ok"
    assert "agy_binary" in data
    assert "auth" in data

def test_daemon_get_run_probe(daemon_server):
    req = urllib.request.Request(f"{daemon_server}/run")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert data["status"] == "ok"

def test_daemon_get_usage(daemon_server, monkeypatch):
    async def mock_print(prompt, model_override="", conversation_id=None, timeout=120.0):
        return {
            "returncode": 0,
            "stdout": "Quota:\nGemini Models  Weekly Limit Remaining  95%  2026-08-20Z",
            "stderr": "",
            "conversation_id": None
        }
    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print)

    req = urllib.request.Request(f"{daemon_server}/usage")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert "quotas" in data

def test_daemon_get_models(daemon_server, monkeypatch):
    from unittest.mock import MagicMock
    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "gpt-oss-120b-medium  GPT-OSS 120B (Medium)\n"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_run)

    req = urllib.request.Request(f"{daemon_server}/models")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert data["status"] == "ok"
    assert len(data["models"]) == 1
    assert data["models"][0]["id"] == "gpt-oss-120b-medium"

def test_daemon_get_status(daemon_server):
    req = urllib.request.Request(f"{daemon_server}/status")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert data["status"] == "ok"
    assert "auth" in data

def test_extract_prompt_from_messages():
    assert host_agy_daemon.extract_prompt_from_messages([]) == ""
    assert host_agy_daemon.extract_prompt_from_messages(None) == ""

    msgs = [
        {"role": "system", "content": "You are a helpful bot"},
        {"role": "user", "content": [{"type": "text", "text": "Hello world"}]},
        {"role": "assistant", "content": "Hi there!", "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]},
        {"role": "tool", "content": "Sunny 25C"},
        {"role": "user", "content": "Great!"},
    ]
    prompt = host_agy_daemon.extract_prompt_from_messages(msgs)
    assert "System: You are a helpful bot" in prompt
    assert "User: Hello world" in prompt
    assert "Assistant: Hi there!" in prompt
    assert "[Tool Call: get_weather" in prompt
    assert "Tool Output: Sunny 25C" in prompt
    assert "User: Great!" in prompt

def test_daemon_get_v1_models(daemon_server):
    req = urllib.request.Request(f"{daemon_server}/v1/models")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
    assert data["object"] == "list"
    model_ids = [m["id"] for m in data["data"]]
    assert "gemini-3.8-flash" in model_ids
    assert "claude-opus-4.6" in model_ids

def test_daemon_chat_completions_non_streaming(daemon_server, monkeypatch):
    captured = {}
    async def mock_print(prompt, model_override="", conversation_id=None, timeout=120.0):
        captured["prompt"] = prompt
        captured["model_override"] = model_override
        captured["conversation_id"] = conversation_id
        return {
            "returncode": 0,
            "stdout": "Hello from Gemini 3.8 Flash",
            "stderr": "",
            "conversation_id": "conv_999"
        }
    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print)

    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
        "conversation_id": "test_conv"
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Session-ID": "sess_123"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())

    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello from Gemini 3.8 Flash"
    assert captured["model_override"] == "gemini-3.8-flash-low"
    assert captured["conversation_id"] == "test_conv"
    assert "User: Hi" in captured["prompt"]

def test_daemon_chat_completions_opus_override(daemon_server, monkeypatch):
    captured = {}
    async def mock_print(prompt, model_override="", conversation_id=None, timeout=120.0):
        captured["model_override"] = model_override
        return {"returncode": 0, "stdout": "Opus reply", "stderr": "", "conversation_id": None}
    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print)

    payload = {
        "model": "claude-opus-4.6",
        "messages": [{"role": "user", "content": "Think deeply"}],
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())

    assert data["choices"][0]["message"]["content"] == "Opus reply"
    assert captured["model_override"] == "claude-opus-4-6-thinking"

def test_daemon_chat_completions_quota_error(daemon_server, monkeypatch):
    async def mock_print(prompt, model_override="", conversation_id=None, timeout=120.0):
        return {"returncode": 1, "stdout": "", "stderr": "Resource exhausted: quota limit reached (429)", "conversation_id": None}
    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print)

    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 429
    err_data = json.loads(exc.value.read().decode())
    assert err_data["error"]["type"] == "rate_limit_error"

def test_daemon_chat_completions_generic_error(daemon_server, monkeypatch):
    async def mock_print(prompt, model_override="", conversation_id=None, timeout=120.0):
        return {"returncode": 2, "stdout": "", "stderr": "Crash or socket error", "conversation_id": None}
    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print)

    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 502

def test_daemon_chat_completions_streaming(daemon_server, monkeypatch):
    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    read_count = 0
    def mock_read(fd, n):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return b"Hello world"
        return b""
    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read)

    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "Stream me"}],
        "stream": True,
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        lines = resp.read().decode("utf-8").split("\n\n")

    sse_data = [l for l in lines if l.startswith("data: ") and not l.startswith("data: [DONE]")]
    assert len(sse_data) >= 1
    parsed_chunk = json.loads(sse_data[0].replace("data: ", ""))
    assert parsed_chunk["object"] == "chat.completion.chunk"
    assert parsed_chunk["choices"][0]["delta"]["content"] == "Hello world"

def test_format_tools_instruction():
    assert host_agy_daemon.format_tools_instruction([]) == ""
    assert host_agy_daemon.format_tools_instruction(None) == ""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        }
    ]
    instr = host_agy_daemon.format_tools_instruction(tools)
    assert "# Available Tools" in instr
    assert "<tools>" in instr
    assert "terminal" in instr
    assert "<tool_call>" in instr

def test_parse_tool_calls_from_text():
    # Empty
    assert host_agy_daemon.parse_tool_calls_from_text("") == ("", [])
    assert host_agy_daemon.parse_tool_calls_from_text(None) == ("", [])

    # Standard XML tool call tag
    text1 = "I will check uptime:\n<tool_call>\n{\"name\": \"terminal\", \"arguments\": {\"command\": \"uptime\"}}\n</tool_call>"
    cleaned1, calls1 = host_agy_daemon.parse_tool_calls_from_text(text1)
    assert cleaned1 == "I will check uptime:"
    assert len(calls1) == 1
    assert calls1[0]["function"]["name"] == "terminal"
    assert json.loads(calls1[0]["function"]["arguments"]) == {"command": "uptime"}
    assert calls1[0]["id"].startswith("call_")

    # Markdown fence tool call
    text2 = "```tool_call\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"test.txt\"}}\n```"
    cleaned2, calls2 = host_agy_daemon.parse_tool_calls_from_text(text2)
    assert cleaned2 == ""
    assert len(calls2) == 1
    assert calls2[0]["function"]["name"] == "read_file"

    # Multiple tool calls
    text3 = "<tool_call>{\"name\": \"tool_a\", \"arguments\": {}}</tool_call>\n<tool_call>{\"name\": \"tool_b\", \"arguments\": {\"x\": 1}}</tool_call>"
    cleaned3, calls3 = host_agy_daemon.parse_tool_calls_from_text(text3)
    assert len(calls3) == 2
    assert calls3[0]["function"]["name"] == "tool_a"
    assert calls3[1]["function"]["name"] == "tool_b"

    # Fallback raw JSON
    text4 = '{"name": "terminal", "arguments": {"command": "df -h"}}'
    cleaned4, calls4 = host_agy_daemon.parse_tool_calls_from_text(text4)
    assert len(calls4) == 1
    assert calls4[0]["function"]["name"] == "terminal"

    # Conversational text only
    text5 = "Hello, I am an AI assistant."
    cleaned5, calls5 = host_agy_daemon.parse_tool_calls_from_text(text5)
    assert cleaned5 == "Hello, I am an AI assistant."
    assert calls5 == []

def test_extract_prompt_with_tools():
    msgs = [{"role": "user", "content": "What is the date?"}]
    tools = [{"type": "function", "function": {"name": "get_date", "parameters": {}}}]
    prompt = host_agy_daemon.extract_prompt_from_messages(msgs, tools=tools)
    assert "System: # Available Tools" in prompt
    assert "get_date" in prompt
    assert "User: What is the date?" in prompt

def test_daemon_chat_completions_with_tools_non_streaming(daemon_server, monkeypatch):
    async def mock_print(prompt, model_override="", conversation_id=None, timeout=120.0):
        return {
            "returncode": 0,
            "stdout": "<tool_call>\n{\"name\": \"terminal\", \"arguments\": {\"command\": \"uptime\"}}\n</tool_call>",
            "stderr": "",
            "conversation_id": "conv_tool_1",
        }
    monkeypatch.setattr(host_agy_daemon, "execute_agy_print", mock_print)

    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "Check uptime"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            }
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())

    assert data["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = data["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "terminal"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"command": "uptime"}

def test_daemon_chat_completions_with_tools_streaming(daemon_server, monkeypatch):
    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    read_count = 0
    def mock_read(fd, n):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return b"<tool_call>{\"name\": \"terminal\", \"arguments\": {\"command\": \"uname -a\"}}</tool_call>"
        return b""
    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read)

    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "uname"}],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
        "stream": True,
    }
    req = urllib.request.Request(
        f"{daemon_server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        lines = resp.read().decode("utf-8").split("\n\n")

    sse_data = [json.loads(l.replace("data: ", "")) for l in lines if l.startswith("data: ") and not l.startswith("data: [DONE]")]
    assert len(sse_data) >= 2
    # First chunk has tool_calls delta
    assert sse_data[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "terminal"
    # Second chunk has finish_reason = tool_calls
    assert sse_data[1]["choices"][0]["finish_reason"] == "tool_calls"



