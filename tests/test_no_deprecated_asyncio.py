"""Guard: no use of asyncio's deprecated ``iscoroutinefunction`` (removed in Python 3.16).

Python 3.14 deprecates it in favour of ``inspect.iscoroutinefunction``. The agy daemon called it on
every stream-json spawn, so the prod journal logged a DeprecationWarning for each call site.
pytest.ini ignores DeprecationWarning globally: the runtime tests below turn it into an error around
the real-process code path of the daemon, and a source scan keeps the call out of the repository.
"""

import ast
import json
import warnings
from pathlib import Path

import pytest

import host_agy_daemon as d

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("router", "scripts", "litellm", "tests")
TERMINAL = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        },
    }
]


def attribute_root(node: ast.expr) -> str | None:
    """The name an attribute chain starts from (``asyncio`` for ``asyncio.coroutines.x``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def asyncio_names(tree: ast.Module) -> set[str]:
    """Names an import binds to asyncio or to something imported from it (aliases included)."""
    names = {"asyncio"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio" or alias.name.startswith("asyncio."):
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "asyncio":
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def deprecated_calls(path: Path, root: Path = ROOT) -> list[str]:
    """Every use of asyncio's iscoroutinefunction in a Python file, as ``path:line``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = asyncio_names(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "iscoroutinefunction":
            if attribute_root(node.value) in names:
                hits.append(f"{path.relative_to(root)}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "asyncio":
            if any(alias.name == "iscoroutinefunction" for alias in node.names):
                hits.append(f"{path.relative_to(root)}:{node.lineno}")
    return sorted(hits, key=lambda hit: int(hit.rsplit(":", 1)[1]))


def test_no_source_uses_asyncio_iscoroutinefunction():
    files = sorted(ROOT.glob("*.py"))
    for top in SOURCE_DIRS:
        files += sorted(p for p in (ROOT / top).rglob("*.py") if "__pycache__" not in p.parts)
    assert ROOT / "scripts" / "host_agy_daemon.py" in files
    assert [hit for path in files for hit in deprecated_calls(path)] == []


def test_the_scan_finds_the_deprecated_forms(tmp_path):
    old = "asyncio" + ".iscoroutinefunction"  # split: a grep of the repository finds no call
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import asyncio\n"
        "from asyncio import iscoroutinefunction\n"
        f"{old}(print)\n"
        "asyncio.coroutines.iscoroutinefunction(print)\n"
        "import inspect\n"
        "inspect.iscoroutinefunction(print)\n"
        f"text = '{old}'\n"
        "import asyncio as aio\n"
        "aio.iscoroutinefunction(print)\n"
        "from asyncio import coroutines as co\n"
        "co.iscoroutinefunction(print)\n"
        "import asyncio.coroutines as aco\n"
        "aco.iscoroutinefunction(print)\n"
    )
    assert deprecated_calls(sample, root=tmp_path) == [
        "sample.py:2",
        "sample.py:3",
        "sample.py:4",
        "sample.py:9",
        "sample.py:11",
        "sample.py:13",
    ]


class FakeStdin:
    """The asyncio.StreamWriter surface the daemon uses: coroutine drain() and wait_closed()."""

    def __init__(self):
        self.data = b""
        self.awaited: list[str] = []

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        self.awaited.append("drain")

    def close(self) -> None:
        self.awaited.append("close")

    async def wait_closed(self) -> None:
        self.awaited.append("wait_closed")


class FakeStream:
    def __init__(self, events: list[dict]):
        self.lines = [json.dumps(event).encode() + b"\n" for event in events]

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    """An asyncio.subprocess.Process stand-in with real coroutine methods (no communicate())."""

    def __init__(self, events: list[dict]):
        self.stdin = FakeStdin()
        self.stdout = FakeStream(events)
        self.stderr = FakeStream([])
        self.returncode: int | None = None
        self.killed = False
        self.waits = 0

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waits += 1
        self.returncode = -9 if self.killed else 0
        return self.returncode


def fake_spawner(monkeypatch, events: list[dict], record: dict | None = None) -> list[FakeProcess]:
    procs: list[FakeProcess] = []

    async def fake_exec(*cmd, **kwargs):
        if record is not None:  # what the tool-guard hook records for the step
            d.record_tool_call(json.dumps(record), kwargs["env"][d.TOOL_RECORDS_ENV])
        procs.append(FakeProcess(events))
        return procs[-1]

    monkeypatch.setattr(d.asyncio, "create_subprocess_exec", fake_exec)
    return procs


@pytest.mark.asyncio
async def test_stream_json_run_emits_no_deprecation_warning(monkeypatch):
    procs = fake_spawner(
        monkeypatch,
        [
            {"event": "init", "conversation_id": "conv-1"},
            {"event": "step_update", "step_update": {"text_delta": "Hel"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "Hello"}},
        ],
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        res = await d.execute_agy_stream_json("say hello")
    assert res["returncode"] == 0, res["stderr"]
    assert res["stdout"] == "Hello"
    assert res["conversation_id"] == "conv-1"
    # The coroutine methods were recognised and awaited: prompt flushed, pipe closed, exit reaped.
    assert procs[0].stdin.awaited == ["drain", "close", "wait_closed"]
    assert procs[0].waits == 1


@pytest.mark.asyncio
async def test_intercepted_tool_call_emits_no_deprecation_warning(monkeypatch):
    procs = fake_spawner(
        monkeypatch,
        [
            {"event": "init", "conversation_id": "conv-2"},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 2,
                    "tool_name": "run_command",
                    "tool_info": {"name": "run_command", "parameters": {"CommandLine": "uptime"}},
                },
            },
        ],
        record={"stepIdx": 2, "toolCall": {"name": "run_command", "args": {"CommandLine": "uptime"}}},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        res = await d.execute_agy_stream_json("check uptime", tools=TERMINAL, intercept_tools=True)
    assert res["returncode"] == 0, res["stderr"]
    assert json.loads(res["tool_calls"][0]["function"]["arguments"]) == {"command": "uptime"}
    # agy is stopped at the intercepted step and its exit is awaited (no zombie left behind).
    assert procs[0].killed
    assert procs[0].waits == 1
