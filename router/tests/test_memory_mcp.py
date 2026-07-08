import json
import re
import time
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from memory_mcp import (
    PREFIX,
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    _is_memory_key,
    _make_key,
    _memory_entry,
    _memory_value,
    _parse_key,
    _parse_memory_value,
)


# =====================================================================
# Tests from router/test_memory_mcp.py
# =====================================================================

def test_make_key_global():
    """Test generating a key for global scope."""
    category = "test_cat"
    data = "test_data"

    before_ts = int(time.time() * 1000)
    key = _make_key(category, True, data)
    after_ts = int(time.time() * 1000)

    # Expected format: f"{PREFIX}:v2:{scope}:{category}::{ts}:{h}"
    assert key.startswith(f"{PREFIX}:v2:{SCOPE_GLOBAL}:{category}::")

    # Extract timestamp and hash part
    match = re.match(rf"^{PREFIX}:v2:{SCOPE_GLOBAL}:{category}::(\d+):([a-f0-9]+)$", key)
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

    assert key.startswith(f"{PREFIX}:v2:{SCOPE_LOCAL}:{category}::")

    match = re.match(rf"^{PREFIX}:v2:{SCOPE_LOCAL}:{category}::(\d+):([a-f0-9]+)$", key)
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
    assert key1 == f"{PREFIX}:v2:{SCOPE_GLOBAL}:cat1::1620000000123:5e5dad075ca7764bc51f"

    key2 = _make_key("cat2", False, "data")
    assert key2 == f"{PREFIX}:v2:{SCOPE_LOCAL}:cat2::1620000000123:5e5dad075ca7764bc51f"


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
    assert result == {"data": "", "tags": []}


def test_parse_memory_value_invalid_json_string():
    """Test _parse_memory_value with invalid JSON string."""
    result = _parse_memory_value("this is not a valid json string")
    assert result == {"data": "this is not a valid json string", "tags": []}


# =====================================================================
# Tests from test_memory_mcp.py (root)
# =====================================================================

def test_memory_entry_happy_path():
    """Test correctly formatted and complete memory entry."""
    valid_key = "memory:global:project_standards::1689201948123:a1b2c3d4e5f6"
    valid_value = json.dumps({"data": "Use pytest for all tests", "tags": ["testing", "python"]})
    lmem = {
        "key": valid_key,
        "value": valid_value,
        "memory_id": "test_id_123"
    }

    result = _memory_entry(lmem)

    assert result is not None
    assert result["key"] == valid_key
    assert result["category"] == "project_standards"
    assert result["data"] == "Use pytest for all tests"
    assert result["tags"] == ["testing", "python"]
    assert result["scope"] == "global"
    assert result["timestamp"] == "1689201948123"
    assert result["memory_id"] == "test_id_123"


def test_memory_entry_invalid_key():
    """Test with a key that does not start with 'memory:'."""
    lmem = {
        "key": "notamemory:global:cat::123:hash",
        "value": json.dumps({"data": "test", "tags": []})
    }

    result = _memory_entry(lmem)
    assert result is None


def test_memory_entry_malformed_json_value():
    """Test with malformed/string value where JSON parsing fails."""
    valid_key = "memory:local:notes::1689201948123:a1b2c3d4e5f6"
    # value is just a raw string, not JSON
    lmem = {
        "key": valid_key,
        "value": "This is just a raw string without tags"
    }

    result = _memory_entry(lmem)

    assert result is not None
    assert result["data"] == "This is just a raw string without tags"
    assert result["tags"] == [] # Falls back to empty tags list
    assert result["category"] == "notes"
    assert result["scope"] == "local"


def test_memory_entry_missing_fields():
    """Test gracefully handling dictionaries with missing keys."""
    # Missing 'value' and 'memory_id'
    lmem1 = {
        "key": "memory:global:ideas::123:hash"
    }
    result1 = _memory_entry(lmem1)
    assert result1 is not None
    assert result1["data"] == ""
    assert result1["tags"] == []
    assert result1["memory_id"] == ""

    # Missing 'key'
    lmem2 = {
        "value": json.dumps({"data": "test", "tags": []})
    }
    result2 = _memory_entry(lmem2)
    assert result2 is None

    # Empty dict
    result3 = _memory_entry({})
    assert result3 is None

def test_is_memory_key_types():
    """Test _is_memory_key works with both string and non-string inputs."""
    assert _is_memory_key("memory:local:test") is True
    assert _is_memory_key("other:prefix") is False
    assert _is_memory_key(None) is False
    assert _is_memory_key(12345) is False
    assert _is_memory_key([]) is False


@pytest.mark.parametrize(
    "key, expected",
    [
        (
            "memory:local:code::20240101T120000Z:abc123hash",
            {"scope": "local", "category": "code", "timestamp": "20240101T120000Z"},
        ),
        (
            "memory:global:general",
            {"scope": "global", "category": "general", "timestamp": ""},
        ),
        (
            "memory:local::20240101T120000Z:abc123hash",
            {"scope": "local", "category": "", "timestamp": "20240101T120000Z"},
        ),
        (
            "memory",
            {"scope": "", "category": "", "timestamp": ""},
        ),
        (
            "",
            {"scope": "", "category": "", "timestamp": ""},
        ),
        (
            None,
            {"scope": "", "category": "", "timestamp": ""},
        ),
        (
            "memory:global:category:with:colons::20240101T120000Z:abc123hash",
            {"scope": "global", "category": "category", "timestamp": "20240101T120000Z"},
        ),
        (
            "memory:global:general::20240101T120000Z",
            {"scope": "global", "category": "general", "timestamp": "20240101T120000Z"},
        ),
        (
            "memory:v2:local:proj%3Aalpha%2F100%25%20ready::20240101T120000Z:abc123hash",
            {"scope": "local", "category": "proj:alpha/100% ready", "timestamp": "20240101T120000Z"},
        ),
    ],
    ids=[
        "happy_path",
        "missing_timestamp_hash",
        "missing_category",
        "missing_scope_and_category",
        "empty_string",
        "invalid_type",
        "extra_colons_in_category",
        "missing_hash_but_has_timestamp",
        "v2_escaped_category",
    ]
)
def test_parse_key(key, expected):
    """Test _parse_key with various valid and invalid formats."""
    result = _parse_key(key)
    assert result == expected

def test_parse_memory_value_valid_json():
    raw_data = json.dumps({"data": "some data", "tags": ["tag1", "tag2"]})
    result = _parse_memory_value(raw_data)
    assert result == {"data": "some data", "tags": ["tag1", "tag2"]}


def test_parse_memory_value_invalid_json_fallback():
    raw_data = "this is not json"
    result = _parse_memory_value(raw_data)
    assert result == {"data": "this is not json", "tags": []}


def test_parse_memory_value_type_error_fallback():
    raw_data = 12345
    result = _parse_memory_value(raw_data)  # type: ignore[arg-type]
    assert result == {"data": "12345", "tags": []}


def test_parse_memory_value_null_data():
    raw_data = '{"data": null, "tags": ["tag1"]}'
    result = _parse_memory_value(raw_data)
    assert result == {"data": "", "tags": ["tag1"]}


def test_parse_memory_value_non_dict_json():
    raw_data = '"just a string"'
    result = _parse_memory_value(raw_data)
    assert result == {"data": "just a string", "tags": []}


def test_parse_memory_value_drops_extra_fields():
    raw_data = json.dumps({"data": "some data", "tags": ["tag1"], "extra": {"nested": True}})
    result = _parse_memory_value(raw_data)
    assert result == {"data": "some data", "tags": ["tag1"]}


def test_make_key_and_parse_key_round_trip():
    """Verify that _make_key and _parse_key correctly quote and unquote complex categories."""
    category = "proj:alpha/100% ready"
    key = _make_key(category, is_global=False, data="test-data")
    
    # Assert that the category in the key is URL-encoded
    assert "proj%3Aalpha%2F100%25%20ready" in key
    
    # Assert that the parsed key returns the original unencoded category
    parsed = _parse_key(key)
    assert parsed["scope"] == "local"
    assert parsed["category"] == category


@pytest.mark.asyncio
async def test_handle_remove_memory_category_url_encoding():
    from memory_mcp import handle_remove_memory_category
    
    category = "test:cat/100%_done"
    key = _make_key(category, is_global=False, data="some-value")
    
    mock_list_response = MagicMock()
    mock_list_response.status_code = 200
    mock_list_response.json.return_value = {
        "memories": [
            {
                "key": key,
                "value": _memory_value("some-value", ["tag1"])
            }
        ]
    }
    
    mock_delete_response = MagicMock()
    mock_delete_response.status_code = 200
    
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_list_response
    mock_client.delete.return_value = mock_delete_response
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        result = await handle_remove_memory_category({"category": category, "is_global": False})
        
        assert "Removed 1 memory" in result
        
        expected_quoted_key = urllib.parse.quote(key, safe="")
        mock_client.delete.assert_called_once()
        called_url = mock_client.delete.call_args[0][0]
        assert called_url.endswith(expected_quoted_key)


@pytest.mark.asyncio
async def test_handle_remove_specific_memory_url_encoding():
    from memory_mcp import handle_remove_specific_memory
    
    category = "test:cat/100%_done"
    key = _make_key(category, is_global=False, data="some-value")
    
    mock_list_response = MagicMock()
    mock_list_response.status_code = 200
    mock_list_response.json.return_value = {
        "memories": [
            {
                "key": key,
                "value": _memory_value("some-value", ["tag1"])
            }
        ]
    }
    
    mock_delete_response = MagicMock()
    mock_delete_response.status_code = 200
    
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_list_response
    mock_client.delete.return_value = mock_delete_response
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        result = await handle_remove_specific_memory({
            "category": category,
            "memory_content": "some-value",
            "is_global": False
        })
        
        assert "Removed memory" in result
        
        expected_quoted_key = urllib.parse.quote(key, safe="")
        mock_client.delete.assert_called_once()
        called_url = mock_client.delete.call_args[0][0]
        assert called_url.endswith(expected_quoted_key)


@pytest.mark.asyncio
async def test_handle_remove_memory_category_failure():
    from memory_mcp import handle_remove_memory_category
    
    key1 = _make_key("cat", is_global=False, data="val1")
    key2 = _make_key("cat", is_global=False, data="val2")
    
    mock_list_response = MagicMock()
    mock_list_response.status_code = 200
    mock_list_response.json.return_value = {
        "memories": [
            {"key": key1, "value": _memory_value("val1", [])},
            {"key": key2, "value": _memory_value("val2", [])}
        ]
    }
    
    mock_response_200 = MagicMock()
    mock_response_200.status_code = 200
    mock_response_500 = MagicMock()
    mock_response_500.status_code = 500
    mock_response_500.text = "Internal Server Error"
    
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_list_response
    mock_client.delete.side_effect = [mock_response_200, mock_response_500]
    
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        result = await handle_remove_memory_category({"category": "cat", "is_global": False})
        
        assert "Error removing memory" in result
        assert "deleted 1 of 2" in result
        assert "Internal Server Error" in result
