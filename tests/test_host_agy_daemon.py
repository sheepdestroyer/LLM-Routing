import asyncio
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from unittest.mock import AsyncMock

import pytest

import sys
from pathlib import Path

# Dynamic project root discovery
root = Path(__file__).resolve()
while root.parent != root and not (root / ".git").exists():
    root = root.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "scripts"))

import host_agy_daemon

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

@pytest.fixture
def daemon_server():
    port = find_free_port()
    host_agy_daemon.PORT = port

    server = host_agy_daemon.ThreadingHTTPServer(('127.0.0.1', port), host_agy_daemon.AgyDaemonHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    server.server_close()
    server_thread.join()

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
        urllib.request.urlopen(req)
    assert exc.value.code == 404

def test_daemon_post_stream_false(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False, "conversation_id": "conv_abc", "model_override": "gpt-4"}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        assert args == (host_agy_daemon.AGY_BINARY, "--conversation", "conv_abc", "--print", "test prompt")
        assert kwargs.get("env", {}).get("CASCADE_DEFAULT_MODEL_OVERRIDE") == "gpt-4"
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

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == 0
    assert data["stdout"] == "mocked stdout output"
    assert data["stderr"] == "mocked stderr output"
    assert data["conversation_id"] == "last_conv_456"

def test_daemon_post_stream_false_timeout(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False, "timeout": 0.1}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

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

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == -1
    assert data["stderr"] == "TIMEOUT"

def test_daemon_post_stream_true(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True, "model_override": "test-model"}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        assert args == (host_agy_daemon.AGY_BINARY, "--print", "test prompt")
        assert kwargs.get("env", {}).get("CASCADE_DEFAULT_MODEL_OVERRIDE") == "test-model"
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

    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 3
    assert json.loads(lines[0]) == {"type": "token", "content": "token1\n"}
    assert json.loads(lines[1]) == {"type": "token", "content": "token2\n"}
    assert json.loads(lines[2]) == {"type": "status", "returncode": 0, "conversation_id": "last_conv_456"}

def test_daemon_post_stream_true_exec_error(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        raise Exception("exec failed")

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": -1, "stderr": "exec failed"}

def test_daemon_post_stream_true_timeout(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True, "timeout": 0.1}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

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

    with urllib.request.urlopen(req) as resp:
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
    # Mock serve_forever to raise KeyboardInterrupt
    def mock_serve_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(host_agy_daemon.ThreadingHTTPServer, "serve_forever", mock_serve_forever)

    # Track if server_close was called
    close_called = False
    def mock_server_close(self):
        nonlocal close_called
        close_called = True

    monkeypatch.setattr(host_agy_daemon.ThreadingHTTPServer, "server_close", mock_server_close)

    # Should not raise exception
    host_agy_daemon.run_server()
    assert close_called

def test_daemon_post_stream_false_no_model_override(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        assert "CASCADE_DEFAULT_MODEL_OVERRIDE" not in kwargs.get("env", {})
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon.os.environ, "copy", lambda: {"CASCADE_DEFAULT_MODEL_OVERRIDE": "old-model"})

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == 0

def test_daemon_post_stream_true_read_oserror(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    def mock_read(fd, n):
        raise OSError("read error")

    monkeypatch.setattr(host_agy_daemon.os, "read", mock_read)

    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "status"

def test_daemon_post_stream_true_timeout_kill_fail(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True, "timeout": 0.1}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

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

    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": -1, "conversation_id": None}

def test_daemon_post_stream_true_wait_exception(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

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

    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": -1, "conversation_id": None}

def test_daemon_post_stream_false_timeout_kill_fail(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False, "timeout": 0.1}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

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

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == -1
    assert data["stderr"] == "TIMEOUT"

def test_daemon_post_stream_false_wait_exception(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        async def mock_wait():
            raise Exception("wait failed")
        mock_proc.wait = mock_wait
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == -1

def test_daemon_post_stream_false_file_read_error(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        # Corrupt the temp files to cause read exceptions
        os.unlink(kwargs["stdout"].name)
        os.unlink(kwargs["stderr"].name)

        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == 0
    assert data["stdout"] == ""
    assert data["stderr"] == ""

def test_daemon_post_stream_false_unlink_error(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)

    def mock_unlink(path):
        raise Exception("unlink failed")

    monkeypatch.setattr(host_agy_daemon.os, "unlink", mock_unlink)

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    assert data["returncode"] == 0

def test_daemon_post_stream_true_with_conversation(daemon_server, monkeypatch):
    req = urllib.request.Request(
        f"{daemon_server}/run",
        data=json.dumps({"prompt": "test prompt", "stream": True, "conversation_id": "conv_789"}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    async def mock_exec(*args, **kwargs):
        assert args == (host_agy_daemon.AGY_BINARY, "--conversation", "conv_789", "--print", "test prompt")
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        return mock_proc

    monkeypatch.setattr(host_agy_daemon.asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr(host_agy_daemon, "get_last_conversation_id", lambda: "conv_789")
    monkeypatch.setattr(host_agy_daemon.os, "read", lambda fd, n: b"")

    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode().strip()
        lines = content.split("\n")

    assert len(lines) == 1
    assert json.loads(lines[0]) == {"type": "status", "returncode": 0, "conversation_id": "conv_789"}
