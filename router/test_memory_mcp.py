import time
import re
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_mcp import _is_memory_key, _make_key, SCOPE_GLOBAL, SCOPE_LOCAL, PREFIX, _memory_value, _parse_memory_value

def test_make_key_global():
    """Test generating a key for global scope."""
    category = "test_cat"
    data = "test_data"

    before_ts = int(time.time() * 1000)
    key = _make_key(category, True, data)
    after_ts = int(time.time() * 1000)

    # Expected format: f"{PREFIX}:{scope}:{category}::{ts}:{h}"
    parts = key.split(":")

    assert key.startswith(f"{PREFIX}:{SCOPE_GLOBAL}:{category}::")

    # Extract timestamp and hash part
    # Format is memory:global:test_cat::1717612345:a1b2c3d4...
    match = re.match(rf"^{PREFIX}:{SCOPE_GLOBAL}:{category}::(\d+):([a-f0-9]+)$", key)
    assert match is not None, f"Key {key} does not match expected format"

    ts = int(match.group(1))
    h = match.group(2)

    assert before_ts <= ts <= after_ts
    assert len(h) == 20

def test_make_key_local():
    """Test generating a key for local scope."""
    category = "another_cat"
    data = "more_data"

    before_ts = int(time.time() * 1000)
    key = _make_key(category, False, data)
    after_ts = int(time.time() * 1000)

    assert key.startswith(f"{PREFIX}:{SCOPE_LOCAL}:{category}::")

    match = re.match(rf"^{PREFIX}:{SCOPE_LOCAL}:{category}::(\d+):([a-f0-9]+)$", key)
    assert match is not None, f"Key {key} does not match expected format"

    ts = int(match.group(1))
    h = match.group(2)

    assert before_ts <= ts <= after_ts
    assert len(h) == 20


def test_make_key_formatting_details(monkeypatch):
    """Test the exact output formatting of _make_key using deterministic BLAKE2b."""
    # Mock time.time to return a predictable float so ts = 1620000000123
    monkeypatch.setattr(time, "time", lambda: 1620000000.123)

    # data="data", ts=1620000000123 -> blake2b("data1620000000123", digest_size=10) -> 5e5dad075ca7764bc51f
    key1 = _make_key("cat1", True, "data")
    assert key1 == f"{PREFIX}:{SCOPE_GLOBAL}:cat1::1620000000123:5e5dad075ca7764bc51f"

    key2 = _make_key("cat2", False, "data")
    assert key2 == f"{PREFIX}:{SCOPE_LOCAL}:cat2::1620000000123:5e5dad075ca7764bc51f"


def test_make_key_determinism_and_uniqueness():
    """Test determinism for same inputs within same timestamp, and uniqueness across timestamps/data."""
    category = "test_cat"
    data1 = "data1"
    data2 = "data2"

    key1 = _make_key(category, True, data1)
    time.sleep(0.002)
    key2 = _make_key(category, True, data1)
    key3 = _make_key(category, True, data2)

    # Uniqueness across data
    assert key1 != key3

    # Check determinism: if the timestamp parts are the same, the keys should be identical
    ts1 = key1.split("::")[1].split(":")[0]
    ts2 = key2.split("::")[1].split(":")[0]
    if ts1 == ts2:
        assert key1 == key2
    else:
        # If timestamp is different, keys should be different
        assert key1 != key2

def test_memory_value_happy_path():
    """Test _memory_value with standard data and tags."""
    result = _memory_value("some data", ["tag1", "tag2"])
    parsed = json.loads(result)
    assert parsed == {"data": "some data", "tags": ["tag1", "tag2"]}

def test_memory_value_missing_tags():
    """Test _memory_value when tags is None."""
    result = _memory_value("some data", None)
    parsed = json.loads(result)
    assert parsed == {"data": "some data", "tags": []}

def test_memory_value_unicode():
    """Test _memory_value properly handles unicode and ensure_ascii=False."""
    result = _memory_value("こんにちは", ["世界"])
    # If ensure_ascii=False, the unicode characters shouldn't be escaped (no \uXXXX)
    assert "こんにちは" in result
    assert "世界" in result
    parsed = json.loads(result)
    assert parsed == {"data": "こんにちは", "tags": ["世界"]}

def test_parse_memory_value_success():
    """Test _parse_memory_value successfully decodes valid JSON."""
    raw = '{"data": "info", "tags": ["a"]}'
    result = _parse_memory_value(raw)
    assert result == {"data": "info", "tags": ["a"]}

def test_parse_memory_value_invalid_json():
    """Test _parse_memory_value with invalid JSON."""
    result = _parse_memory_value("{invalid_json:")
    assert result == {"data": "{invalid_json:", "tags": []}

def test_parse_memory_value_type_error():
    """Test _parse_memory_value with TypeError (e.g. passing None)."""
    result = _parse_memory_value(None)
    assert result == {"data": None, "tags": []}

def test_parse_memory_value_invalid_json_string():
    """Test _parse_memory_value with invalid JSON string."""
    result = _parse_memory_value("this is not a valid json string")
    assert result == {"data": "this is not a valid json string", "tags": []}

def test_is_memory_key_true():
    """Test _is_memory_key with a valid memory key prefix."""
    assert _is_memory_key(f"{PREFIX}:global:test_cat::123:abc") is True

def test_is_memory_key_false_wrong_prefix():
    """Test _is_memory_key with an incorrect prefix."""
    assert _is_memory_key("not_memory:global:test_cat::123:abc") is False

def test_is_memory_key_empty():
    """Test _is_memory_key with an empty string."""
    assert _is_memory_key("") is False

def test_is_memory_key_none_or_non_string():
    """Test _is_memory_key with None or non-string inputs."""
    assert _is_memory_key(None) is False
    assert _is_memory_key(123) is False
