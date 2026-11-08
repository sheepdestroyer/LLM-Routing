"""Path A never returns a truncated native tool argument.

agy 1.5 stream-json tool summaries (``tool_info.parameters``) cut every string to 512 characters
plus "…" and drop keys; the PreToolUse hook gets the exact tool input. The tool-guard records it
for the daemon, which returns those arguments, or refuses the call when the summary is cut and no
record exists.
"""

import asyncio
import io
import json
import os
import shlex
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, MagicMock

import pytest

import host_agy_daemon as d

CUT = "x" * 512 + "\u2026"  # what agy's summary leaves of a long string: 513 characters
LONG_COMMAND = "printf '%s\\n' " + " ".join(f"'line {n:03d} of the probe'" for n in range(40)) + " | wc -c"
assert len(LONG_COMMAND) > 600
TERMINAL = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        },
    }
]


def cut(text: str) -> str:
    """agy's summary of a string longer than 512 characters."""
    return text[:512] + "\u2026"


def hook_payload(name: str, args, step: int = 2) -> str:
    return json.dumps({"stepIdx": step, "conversationId": "conv-1", "toolCall": {"name": name, "args": args}})


def write_record(directory: str, entry: str, record) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, entry), "w", encoding="utf-8") as f:
        f.write(record if isinstance(record, str) else json.dumps(record))


# ── truncation detection ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"CommandLine": CUT}, "CommandLine"),
        ({"CommandLine": cut(LONG_COMMAND), "Cwd": "/tmp"}, "CommandLine"),
        # nested: a list of edits inside the arguments
        ({"Path": "/f", "Edits": [{"Content": "short"}, {"Content": CUT}]}, "Edits[1].Content"),
        ({"Options": {"Inner": {"Text": CUT}}}, "Options.Inner.Text"),
        # the whole Arguments value became one cut string
        ({"Arguments": CUT}, "Arguments"),
        (CUT, "<arguments>"),
        ([CUT], "[0]"),
        # not cut
        ({"CommandLine": "x" * 511 + "\u2026"}, None),  # 512 characters: under the cut length
        ({"Content": "y" * 4000}, None),  # long, but no ellipsis
        ({"Content": "z" * 600 + "\u2026 more"}, None),  # ellipsis not at the end
        ({"Count": 3, "Flag": True, "Nothing": None, "Ratio": 0.5}, None),
        ({}, None),
        ("short", None),
    ],
)
def test_find_truncated_argument(params, expected):
    assert d.find_truncated_argument(params) == expected


# ── the tool-guard hook records the exact call ──────────────────────────────


def test_tool_guard_records_the_exact_call_then_denies(tmp_path):
    records = tmp_path / "run" / "tmp" / d.TOOL_RECORDS_SUBDIR
    environ = {"AGY_DAEMON_TOOL_POLICY": "deny", d.TOOL_RECORDS_ENV: str(records)}
    out = io.StringIO()
    args = {"CommandLine": LONG_COMMAND, "WaitMsBeforeAsync": 5000}
    assert d.tool_guard_main(io.StringIO(hook_payload("run_command", args)), out, environ) == 0
    assert json.loads(out.getvalue())["decision"] == "deny"
    (entry,) = os.listdir(records)
    assert entry.startswith("call-") and entry.endswith(".json")
    assert json.load(open(records / entry)) == {"step": 2, "name": "run_command", "args": args}
    assert stat.S_IMODE((records / entry).stat().st_mode) == 0o600
    assert stat.S_IMODE(records.stat().st_mode) == 0o700


def test_tool_guard_records_native_calls_too(tmp_path):
    out = io.StringIO()
    environ = {"AGY_DAEMON_TOOL_POLICY": "native", d.TOOL_RECORDS_ENV: str(tmp_path)}
    assert d.tool_guard_main(io.StringIO(hook_payload("view_file", {"Path": "/a"})), out, environ) == 0
    assert out.getvalue() == ""
    assert len(os.listdir(tmp_path)) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"stepIdx": 1}),
        json.dumps({"toolCall": "run_command"}),
        json.dumps({"toolCall": {"name": "run_command"}}),
        json.dumps({"toolCall": {"name": "run_command", "args": "CommandLine=ls"}}),
    ],
)
def test_record_tool_call_ignores_unusable_payloads(tmp_path, raw):
    records = tmp_path / "records"
    d.record_tool_call(raw, str(records))
    assert not records.exists()


def test_record_tool_call_without_directory_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d.record_tool_call(hook_payload("run_command", {"CommandLine": "ls"}), None)
    d.record_tool_call(hook_payload("run_command", {"CommandLine": "ls"}), "")
    assert os.listdir(tmp_path) == []


def test_tool_guard_still_denies_when_the_record_cannot_be_written(tmp_path, monkeypatch):
    def full_disk(path, text):
        raise OSError("no space left on device")

    monkeypatch.setattr(d, "write_private_atomic", full_disk)
    out = io.StringIO()
    environ = {"AGY_DAEMON_TOOL_POLICY": "deny", d.TOOL_RECORDS_ENV: str(tmp_path)}
    assert d.tool_guard_main(io.StringIO(hook_payload("run_command", {"CommandLine": "ls"})), out, environ) == 0
    assert json.loads(out.getvalue())["decision"] == "deny"


def test_spawn_env_record_directory_only_for_path_a():
    assert d.TOOL_RECORDS_ENV not in d.spawn_env("", "deny", "/t")
    env = d.spawn_env("", "deny", "/t", record_tools=True)
    assert env[d.TOOL_RECORDS_ENV] == os.path.join("/t", d.TOOL_RECORDS_SUBDIR)


# ── reading the record ───────────────────────────────────────────────────────


def test_read_tool_record_picks_the_matching_call(tmp_path):
    records = str(tmp_path)
    write_record(records, ".tmp-partial", '{"step": 2, "name": "run_command", "args": {"CommandLine": "partial"}}')
    write_record(records, "call-0.json", {"step": 2, "name": "view_file", "args": {"Path": "/other"}})
    write_record(records, "call-1.json", {"step": 3, "name": "run_command", "args": {"CommandLine": "later step"}})
    write_record(records, "call-2.json", "not json")
    write_record(records, "call-3.json", {"step": 2, "name": "run_command", "args": "not an object"})
    write_record(records, "call-4.json", {"step": 2, "name": "run_command", "args": {"CommandLine": LONG_COMMAND}})
    assert d.read_tool_record(records, 2, "run_command") == {"CommandLine": LONG_COMMAND}
    assert d.read_tool_record(records, 3, "run_command") == {"CommandLine": "later step"}
    # An event without step_index matches on the tool name alone (the first record, by file name).
    assert d.read_tool_record(records, None, "view_file") == {"Path": "/other"}
    assert d.read_tool_record(records, 2, "write_file") is None


def test_read_tool_record_missing_directory(tmp_path):
    assert d.read_tool_record(str(tmp_path / "absent"), 2, "run_command") is None


@pytest.mark.asyncio
async def test_wait_for_tool_record_sees_a_late_record(tmp_path):
    records = str(tmp_path / "records")

    async def hook_runs_late():
        await asyncio.sleep(0.1)
        d.record_tool_call(hook_payload("run_command", {"CommandLine": LONG_COMMAND}), records)

    writer = asyncio.create_task(hook_runs_late())
    args = await d.wait_for_tool_record(records, 2, "run_command", 5.0)
    await writer
    assert args == {"CommandLine": LONG_COMMAND}


@pytest.mark.asyncio
async def test_wait_for_tool_record_gives_up(tmp_path):
    started = time.monotonic()
    assert await d.wait_for_tool_record(str(tmp_path), 2, "run_command", 0.1) is None
    assert time.monotonic() - started >= 0.1
    # Without a record directory (not a Path A spawn) it does not wait at all.
    started = time.monotonic()
    assert await d.wait_for_tool_record(None, 2, "run_command", 5.0) is None
    assert time.monotonic() - started < 1.0


# ── resolving the intercepted call ───────────────────────────────────────────


def tool_step(name: str, parameters, step: int | None = 2) -> dict:
    su = {
        "state": "ACTIVE",
        "step_type": "tool",
        "tool_name": name,
        "tool_info": {"name": name, "parameters": parameters},
    }
    if step is not None:
        su["step_index"] = step
    return su


@pytest.mark.asyncio
async def test_resolve_uses_the_hook_record_over_a_cut_summary(tmp_path, capsys):
    d.record_tool_call(
        hook_payload("run_command", {"CommandLine": LONG_COMMAND, "WaitMsBeforeAsync": 5000}), str(tmp_path)
    )
    call = await d.resolve_native_tool_call(
        tool_step("run_command", {"CommandLine": cut(LONG_COMMAND)}), TERMINAL, str(tmp_path)
    )
    assert call["function"]["name"] == "terminal"
    assert json.loads(call["function"]["arguments"]) == {"command": LONG_COMMAND}
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_resolve_passes_full_arguments_through(tmp_path):
    content = "\n".join(f"row {n}" for n in range(500))
    args = {"Path": "/w/notes.txt", "Content": content, "Overwrite": True}
    d.record_tool_call(hook_payload("write_file", args), str(tmp_path))
    # The summary of a write_file step has no Content at all.
    write_tools = [{"type": "function", "function": {"name": "write_file"}}, *TERMINAL]
    call = await d.resolve_native_tool_call(
        tool_step("write_file", {"Path": "/w/notes.txt"}), write_tools, str(tmp_path)
    )
    assert call["function"]["name"] == "write_file"
    assert json.loads(call["function"]["arguments"]) == args


@pytest.mark.parametrize(
    ("parameters", "key"),
    [
        ({"CommandLine": cut(LONG_COMMAND)}, "CommandLine"),
        ({"Path": "/f", "Edits": [{"Content": CUT}]}, "Edits[0].Content"),
        ({"Arguments": CUT}, "Arguments"),
    ],
)
@pytest.mark.asyncio
async def test_resolve_refuses_a_cut_summary_without_record(tmp_path, capsys, parameters, key):
    with pytest.raises(d.TruncatedToolCallError, match="refusing to return a partial tool call"):
        await d.resolve_native_tool_call(tool_step("run_command", parameters), TERMINAL, str(tmp_path))
    err = capsys.readouterr().err
    assert err.startswith(f"<{d.LOG_WARNING}>agy-daemon: WARNING: refused native tool call 'run_command'")
    assert f"agy truncated argument {key!r}" in err
    assert "xxxxxxxxxx" not in err and "line 000" not in err  # never the value


@pytest.mark.asyncio
async def test_resolve_falls_back_to_an_uncut_summary(tmp_path, capsys):
    call = await d.resolve_native_tool_call(
        tool_step("run_command", {"CommandLine": "uptime"}, step=None), TERMINAL, None
    )
    assert json.loads(call["function"]["arguments"]) == {"command": "uptime"}
    assert "no hook record, using agy's event summary" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_refusal_message_is_not_taken_for_a_quota_error(tmp_path):
    # The daemon maps errors mentioning "rate", "quota", ... to 429; generate_image contains "rate".
    with pytest.raises(d.TruncatedToolCallError) as exc:
        await d.resolve_native_tool_call(tool_step("generate_image", {"Prompt": CUT}), TERMINAL, str(tmp_path))
    message = d.refused_tool_call_result(exc.value, None)["stderr"].lower()
    assert not any(x in message for x in ["quota", "rate", "429", "exhaust", "resource"])


# ── non-streaming, communicate() branch ──────────────────────────────────────


def communicate_proc(lines: list[dict]) -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=("\n".join(json.dumps(x) for x in lines).encode(), b""))
    return proc


@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.asyncio
async def test_communicate_branch(monkeypatch, recorded):
    summary_event = {
        "event": "step_update",
        "step_update": tool_step("run_command", {"CommandLine": cut(LONG_COMMAND)}),
    }

    async def fake_exec(*cmd, **kwargs):
        if recorded:
            d.record_tool_call(
                hook_payload("run_command", {"CommandLine": LONG_COMMAND}), kwargs["env"][d.TOOL_RECORDS_ENV]
            )
        return communicate_proc([{"event": "init", "conversation_id": "conv-1"}, summary_event])

    monkeypatch.setattr(d.asyncio, "create_subprocess_exec", fake_exec)
    res = await d.execute_agy_stream_json("run it", tools=TERMINAL, intercept_tools=True)
    if recorded:
        assert res["returncode"] == 0
        assert json.loads(res["tool_calls"][0]["function"]["arguments"]) == {"command": LONG_COMMAND}
    else:
        assert res["returncode"] == 1 and res["tool_calls"] == []
        assert "refusing to return a partial tool call" in res["stderr"]
        assert res["conversation_id"] == "conv-1"


# ── end to end: fake agy running the real tool-guard hook ────────────────────

FAKE_AGY = """#!{python}
import json, os, shlex, subprocess, sys, time

scenario = json.load(open({scenario!r}))
with open({pid_file!r}, "w") as f:
    f.write(str(os.getpid()))
sys.stdin.readline()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


emit({{"event": "init", "conversation_id": "conv-fake"}})
step = {{
    "conversation_id": "conv-fake",
    "step_index": 2,
    "state": "ACTIVE",
    "step_type": "tool",
    "tool_name": scenario["name"],
    "tool_info": {{"name": scenario["name"], "parameters": scenario["summary"]}},
}}
emit({{"event": "step_update", "step_update": step}})
if scenario["args"] is not None:
    # Like agy: run the PreToolUse hook from the HOME's hooks.json with the exact call on stdin.
    hooks = json.load(open(os.path.join(os.environ["HOME"], ".gemini", "config", "hooks.json")))
    command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = {{"stepIdx": 2, "conversationId": "conv-fake", "toolCall": {{"name": scenario["name"], "args": scenario["args"]}}}}
    done = subprocess.run(shlex.split(command), input=json.dumps(payload), capture_output=True, text=True)
    denied = json.loads(done.stdout or "{{}}").get("decision") == "deny"
    emit({{"event": "step_update", "step_update": dict(step, state="ERROR" if denied else "DONE")}})
# The model's next turn: the daemon must have stopped agy long before this.
time.sleep(20)
emit({{"event": "result", "result": {{"status": "SUCCESS", "response": "agy was not stopped"}}}})
"""


@pytest.fixture
def agy_server(tmp_path, monkeypatch):
    """The daemon on a free port, with its HOME prepared (hooks.json runs the real tool-guard)."""
    d.prepare_home()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = d.ThreadingHTTPServer(("127.0.0.1", port), d.AgyDaemonHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def install(name: str, summary, args) -> str:
        scenario, pid_file, script = tmp_path / "scenario.json", tmp_path / "agy.pid", tmp_path / "fake-agy"
        scenario.write_text(json.dumps({"name": name, "summary": summary, "args": args}))
        script.write_text(FAKE_AGY.format(python=sys.executable, scenario=str(scenario), pid_file=str(pid_file)))
        script.chmod(0o755)
        monkeypatch.setattr(d, "AGY_BINARY", str(script))
        return str(pid_file)

    yield f"http://127.0.0.1:{port}", install
    srv.shutdown()
    srv.server_close()


def chat(url: str, stream: bool):
    body = {
        "model": "agy-gemini",
        "stream": stream,
        "messages": [{"role": "user", "content": "count the bytes"}],
        "tools": TERMINAL,
    }
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=json.dumps(body).encode())
    return urllib.request.urlopen(req, timeout=15)


def sse_events(raw: str) -> list:
    return [json.loads(c[len("data: ") :]) for c in raw.split("\n\n") if c.startswith("data: {")]


def assert_stopped(pid_file: str) -> None:
    pid = int(open(pid_file).read())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


SCENARIOS_OK = [
    # (summary agy prints, exact arguments the hook gets or None, command the client must get)
    pytest.param(
        {"CommandLine": cut(LONG_COMMAND)},
        {"CommandLine": LONG_COMMAND, "WaitMsBeforeAsync": 5000},
        LONG_COMMAND,
        id="cut-recorded",
    ),
    pytest.param({"CommandLine": "uptime"}, {"CommandLine": "uptime"}, "uptime", id="short-recorded"),
    pytest.param({"CommandLine": "uptime"}, None, "uptime", id="short-no-record"),
]
SCENARIOS_REFUSED = [
    # (native tool, summary agy prints with no hook record, key named in the warning)
    pytest.param("run_command", {"CommandLine": cut(LONG_COMMAND)}, "CommandLine", id="cut-flat"),
    pytest.param("write_file", {"Path": "/f", "Edits": [{"Content": CUT}]}, "Edits[0].Content", id="cut-nested"),
    pytest.param("call_mcp_tool", {"Arguments": CUT}, "Arguments", id="cut-whole-arguments"),
    pytest.param("generate_image", {"Prompt": CUT}, "Prompt", id="cut-name-with-rate"),
]


@pytest.mark.parametrize(("summary", "args", "command"), SCENARIOS_OK)
def test_path_a_non_streaming_returns_exact_arguments(agy_server, monkeypatch, summary, args, command):
    url, install = agy_server
    monkeypatch.setattr(d, "TOOL_RECORD_WAIT", 5.0 if args is not None else 0.3)
    pid_file = install("run_command", summary, args)
    started = time.monotonic()
    with chat(url, stream=False) as resp:
        data = json.loads(resp.read())
    assert time.monotonic() - started < 10
    choice = data["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    (call,) = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "terminal"
    assert json.loads(call["function"]["arguments"]) == {"command": command}
    assert_stopped(pid_file)


@pytest.mark.parametrize(("summary", "args", "command"), SCENARIOS_OK)
def test_path_a_streaming_returns_exact_arguments(agy_server, monkeypatch, summary, args, command):
    url, install = agy_server
    monkeypatch.setattr(d, "TOOL_RECORD_WAIT", 5.0 if args is not None else 0.3)
    pid_file = install("run_command", summary, args)
    with chat(url, stream=True) as resp:
        raw = resp.read().decode()
    events = sse_events(raw)
    (call,) = events[0]["choices"][0]["delta"]["tool_calls"]
    assert json.loads(call["function"]["arguments"]) == {"command": command}
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert raw.rstrip().endswith("data: [DONE]")
    assert_stopped(pid_file)


@pytest.mark.parametrize(("name", "summary", "key"), SCENARIOS_REFUSED)
def test_path_a_non_streaming_refuses_a_truncated_argument(agy_server, monkeypatch, capsys, name, summary, key):
    url, install = agy_server
    monkeypatch.setattr(d, "TOOL_RECORD_WAIT", 0.3)
    pid_file = install(name, summary, None)
    with pytest.raises(urllib.error.HTTPError) as exc:
        chat(url, stream=False)
    assert exc.value.code == 502
    error = json.loads(exc.value.read())["error"]
    assert error["type"] == "api_error"
    assert "refusing to return a partial tool call" in error["message"]
    assert_stopped(pid_file)
    err = capsys.readouterr().err
    assert f"<4>agy-daemon: WARNING: refused native tool call {name!r}: agy truncated argument {key!r}" in err
    assert "xxxxxxxxxx" not in err and "line 000" not in err


@pytest.mark.parametrize(("name", "summary", "key"), SCENARIOS_REFUSED)
def test_path_a_streaming_refuses_a_truncated_argument(agy_server, monkeypatch, capsys, name, summary, key):
    url, install = agy_server
    monkeypatch.setattr(d, "TOOL_RECORD_WAIT", 0.3)
    pid_file = install(name, summary, None)
    with chat(url, stream=True) as resp:
        raw = resp.read().decode()
    (event,) = sse_events(raw)
    assert event["error"]["code"] == 502 and event["error"]["type"] == "api_error"
    assert "refusing to return a partial tool call" in event["error"]["message"]
    assert "tool_calls" not in raw and "[DONE]" not in raw
    assert_stopped(pid_file)
    err = capsys.readouterr().err
    assert f"<4>agy-daemon: WARNING: refused native tool call {name!r}: agy truncated argument {key!r}" in err
    assert "xxxxxxxxxx" not in err and "line 000" not in err


def test_hook_command_is_the_daemon_tool_guard():
    d.prepare_home()
    hooks = json.load(open(os.path.join(d.DAEMON_HOME, ".gemini", "config", "hooks.json")))
    argv = shlex.split(hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
    assert argv[1:] == [os.path.abspath(d.__file__), "--tool-guard"]
