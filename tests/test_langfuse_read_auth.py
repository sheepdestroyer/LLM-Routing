"""Trace readers use LANGFUSE_READ_PUBLIC_KEY / LANGFUSE_READ_SECRET_KEY when both are set.

The stack's own pair is write-only in prod (Langfuse: 403 "traces:read" on reads). The four tools
that read traces fall back to it only when no read pair is configured.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

import langfuse_read_auth as lra

ROOT = Path(__file__).resolve().parent.parent
READ = {"LANGFUSE_READ_PUBLIC_KEY": "pk-lf-read-1111", "LANGFUSE_READ_SECRET_KEY": "sk-lf-read-2222"}
MAIN = {"LANGFUSE_PUBLIC_KEY": "pk-lf-ingest-3333", "LANGFUSE_SECRET_KEY": "sk-lf-ingest-4444"}
READERS = ("scripts/extract_prompts.py", "scripts/extract_complex.py", "scripts/extract_gapfill.py")


def test_the_read_pair_wins_when_both_are_set(capsys):
    assert lra.read_pair({**MAIN, **READ}) == ("pk-lf-read-1111", "sk-lf-read-2222")
    assert lra.read_pair(READ) == ("pk-lf-read-1111", "sk-lf-read-2222")
    assert capsys.readouterr().err == ""


def test_the_main_pair_is_the_fallback(capsys):
    assert lra.read_pair(MAIN) == ("pk-lf-ingest-3333", "sk-lf-ingest-4444")
    assert lra.read_pair({**MAIN, "LANGFUSE_READ_PUBLIC_KEY": "", "LANGFUSE_READ_SECRET_KEY": ""}) == (
        "pk-lf-ingest-3333",
        "sk-lf-ingest-4444",
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        ("LANGFUSE_READ_PUBLIC_KEY", "LANGFUSE_READ_SECRET_KEY"),
        ("LANGFUSE_READ_SECRET_KEY", "LANGFUSE_READ_PUBLIC_KEY"),
    ],
)
def test_a_half_read_pair_is_reported_and_ignored(capsys, present, missing):
    assert lra.read_pair({**MAIN, present: READ[present]}) == ("pk-lf-ingest-3333", "sk-lf-ingest-4444")
    err = capsys.readouterr().err
    assert err == f"⚠ {missing} is not set: reading traces with LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY\n"
    assert READ[present] not in err


def test_no_complete_pair():
    assert lra.read_pair({}) is None
    assert lra.read_pair({"LANGFUSE_PUBLIC_KEY": "pk-lf-only"}) is None


def test_env_file_parsing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nLANGFUSE_READ_PUBLIC_KEY=\"pk-lf-read-1111\"\nLANGFUSE_READ_SECRET_KEY='sk-lf-read-2222'\n"
        "  URL = http://h/?a=b  \nnot an assignment\n",
        encoding="utf-8",
    )
    assert lra.load_env_file(env_file) == {
        "LANGFUSE_READ_PUBLIC_KEY": "pk-lf-read-1111",
        "LANGFUSE_READ_SECRET_KEY": "sk-lf-read-2222",
        "URL": "http://h/?a=b",
    }


def test_trace_reader_auth_encodes_the_read_pair(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f'{k}="{v}"\n' for k, v in {**MAIN, **READ}.items()), encoding="utf-8")
    assert base64.b64decode(lra.trace_reader_auth(env_file)).decode() == "pk-lf-read-1111:sk-lf-read-2222"


def test_trace_reader_auth_names_the_variables_when_nothing_is_set(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        lra.trace_reader_auth(env_file)
    assert str(info.value) == (
        "Set LANGFUSE_READ_PUBLIC_KEY/LANGFUSE_READ_SECRET_KEY (a key that may read traces) or "
        f"LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY in {env_file}"
    )


@pytest.mark.parametrize("reader", READERS)
def test_extract_scripts_read_traces_through_the_helper(reader):
    # They run their extraction at import time against localhost:3001 (prod Langfuse), so they are
    # checked statically: one helper call, no direct use of the stack's own pair.
    text = (ROOT / reader).read_text(encoding="utf-8")
    assert "from langfuse_read_auth import trace_reader_auth" in text
    assert "from scripts.langfuse_read_auth import trace_reader_auth" in text
    assert 'auth = trace_reader_auth(Path(__file__).resolve().parent.parent / ".env")' in text
    assert "LANGFUSE_PUBLIC_KEY" not in text and "LANGFUSE_SECRET_KEY" not in text
    assert 'req.add_header("Authorization", f"Basic {auth}")' in text


def _verify_module():
    from scripts.verification import verify_canonical_endpoints

    return verify_canonical_endpoints


def _workdir(tmp_path, env_text, overlay_text=None):
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    if overlay_text is not None:
        (tmp_path / ".env.dev").write_text(overlay_text, encoding="utf-8")
    return tmp_path


def _clear(monkeypatch):
    for name in (*READ, *MAIN):
        monkeypatch.delenv(name, raising=False)


def test_verify_canonical_endpoints_reads_with_the_read_pair(tmp_path, monkeypatch, capsys):
    module = _verify_module()
    _clear(monkeypatch)
    env_text = "".join(f'{k}="{v}"\n' for k, v in {**MAIN, **READ}.items())
    monkeypatch.setattr(module, "WORKDIR", _workdir(tmp_path, env_text))
    cfg = module.load_env()
    assert (cfg["langfuse_public_key"], cfg["langfuse_secret_key"]) == ("pk-lf-read-1111", "sk-lf-read-2222")


def test_verify_canonical_endpoints_falls_back_and_honours_overrides(tmp_path, monkeypatch):
    module = _verify_module()
    _clear(monkeypatch)
    env_text = "".join(f"export {k}={v}\n" for k, v in MAIN.items())
    # The dev overlay adds only half a read pair: ignored, the main pair is used.
    monkeypatch.setattr(module, "WORKDIR", _workdir(tmp_path, env_text, "LANGFUSE_READ_PUBLIC_KEY=pk-lf-half\n"))
    cfg = module.load_env(dev=True)
    assert (cfg["langfuse_public_key"], cfg["langfuse_secret_key"]) == ("pk-lf-ingest-3333", "sk-lf-ingest-4444")
    # A shell variable completes the read pair and wins.
    monkeypatch.setenv("LANGFUSE_READ_SECRET_KEY", "sk-lf-from-shell")
    cfg = module.load_env(dev=True)
    assert (cfg["langfuse_public_key"], cfg["langfuse_secret_key"]) == ("pk-lf-half", "sk-lf-from-shell")


def test_verify_canonical_endpoints_skips_without_any_pair(tmp_path, monkeypatch, capsys):
    module = _verify_module()
    _clear(monkeypatch)
    monkeypatch.setattr(module, "WORKDIR", _workdir(tmp_path, "ROUTER_PORT=5010\n"))
    cfg = module.load_env()
    assert (cfg["langfuse_public_key"], cfg["langfuse_secret_key"]) == ("", "")
    assert module.test_langfuse_session_propagation(cfg) == (0, 0)
    assert "SKIPPED (neither LANGFUSE_READ_PUBLIC_KEY/SECRET_KEY nor LANGFUSE_PUBLIC_KEY/SECRET_KEY set)" in (
        capsys.readouterr().out
    )


def test_verify_canonical_endpoints_explains_a_write_only_key(monkeypatch, capsys):
    module = _verify_module()

    class Response:
        def __init__(self, status, payload=None, text=""):
            self.status_code, self._payload, self.text = status, payload or {}, text

        def json(self):
            return self._payload

    monkeypatch.setattr(module.httpx, "post", lambda *a, **k: Response(200, {"choices": []}))
    gets = []

    def get(url, **kwargs):
        gets.append(url)
        return Response(403, text='{"message":"Access denied. The \\"traces:read\\" permission is required"}')

    monkeypatch.setattr(module.httpx, "get", get)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    cfg = {
        "router_port": "5010",
        "langfuse_web_port": "3011",
        "langfuse_public_key": "pk-lf-ingest-3333",
        "langfuse_secret_key": "sk-lf-ingest-4444",
        "router_api_key": "router-key",
    }
    assert module.test_langfuse_session_propagation(cfg) == (1, 2)
    out = capsys.readouterr().out
    assert "Langfuse refused to list traces (HTTP 403): this key cannot read traces" in out
    assert "LANGFUSE_READ_PUBLIC_KEY/LANGFUSE_READ_SECRET_KEY" in out
    assert len(gets) == 1  # no 20 s of pointless polling
    assert not re.search(r"(pk|sk)-lf-ingest", out)
