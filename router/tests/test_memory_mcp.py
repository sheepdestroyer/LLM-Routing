import json
import re
import time
import io

import httpx
import urllib.parse
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import router.memory_mcp as _router_memory_mcp

sys.modules["memory_mcp"] = _router_memory_mcp
from router.memory_mcp import (
    PREFIX,
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    _is_memory_key,
    _make_key,
    _memory_entry,
    _memory_value,
    _parse_key,
    _parse_memory_value,
    _list_all_memories,
    handle_remember_memory,
    handle_retrieve_memories,
    handle_remove_specific_memory,
    handle_remove_memory_category,
    handle_request,
    main_loop,
    log,
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
    lmem = {"key": valid_key, "value": valid_value, "memory_id": "test_id_123"}

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
    lmem = {"key": "notamemory:global:cat::123:hash", "value": json.dumps({"data": "test", "tags": []})}

    result = _memory_entry(lmem)
    assert result is None


def test_memory_entry_malformed_json_value():
    """Test with malformed/string value where JSON parsing fails."""
    valid_key = "memory:local:notes::1689201948123:a1b2c3d4e5f6"
    # value is just a raw string, not JSON
    lmem = {"key": valid_key, "value": "This is just a raw string without tags"}

    result = _memory_entry(lmem)

    assert result is not None
    assert result["data"] == "This is just a raw string without tags"
    assert result["tags"] == []  # Falls back to empty tags list
    assert result["category"] == "notes"
    assert result["scope"] == "local"


def test_memory_entry_missing_fields():
    """Test gracefully handling dictionaries with missing keys."""
    # Missing 'value' and 'memory_id'
    lmem1 = {"key": "memory:global:ideas::123:hash"}
    result1 = _memory_entry(lmem1)
    assert result1 is not None
    assert result1["data"] == ""
    assert result1["tags"] == []
    assert result1["memory_id"] == ""

    # Missing 'key'
    lmem2 = {"value": json.dumps({"data": "test", "tags": []})}
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
    ],
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
    result = _parse_memory_value(raw_data)
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
    mock_list_response.json.return_value = {"memories": [{"key": key, "value": _memory_value("some-value", ["tag1"])}]}

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
    mock_list_response.json.return_value = {"memories": [{"key": key, "value": _memory_value("some-value", ["tag1"])}]}

    mock_delete_response = MagicMock()
    mock_delete_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_list_response
    mock_client.delete.return_value = mock_delete_response

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await handle_remove_specific_memory(
            {"category": category, "memory_content": "some-value", "is_global": False}
        )

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
            {"key": key2, "value": _memory_value("val2", [])},
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


@pytest.mark.asyncio
async def test_list_all_memories_success():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"memories": [{"key": "test_key", "value": "test_value"}]}
    mock_client.get.return_value = mock_response

    result = await _list_all_memories(mock_client)
    assert result == [{"key": "test_key", "value": "test_value"}]


@pytest.mark.asyncio
async def test_list_all_memories_error():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.get.return_value = mock_response

    result = await _list_all_memories(mock_client)
    assert result == []


@pytest.mark.asyncio
async def test_handle_remember_memory_success():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}
    mock_client.post.return_value = mock_response

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        args = {"category": "test_cat", "data": "test_data", "tags": ["tag1"], "is_global": True}
        result = await handle_remember_memory(args)

        assert "Stored in:" in result
        assert "test_cat" in result
        assert "tag1" in result
        assert "global" in result


@pytest.mark.asyncio
async def test_handle_remember_memory_error():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Error"
    mock_client.post.return_value = mock_response

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.__aenter__.return_value = mock_client

        args = {"category": "test_cat", "data": "test_data"}
        result = await handle_remember_memory(args)

        assert "Error saving memory" in result
        assert "Internal Error" in result


@pytest.mark.asyncio
async def test_handle_retrieve_memories_found():
    key = _make_key("test_cat", is_global=False, data="test_data")
    value = _memory_value("test_data", ["tag1"])

    mock_list_all = AsyncMock()
    mock_list_all.return_value = [{"key": key, "value": value}]

    with patch("memory_mcp._list_all_memories", mock_list_all):
        args = {"category": "test_cat", "is_global": False}
        result = await handle_retrieve_memories(args)

        assert "test_cat" in result
        assert "test_data" in result
        assert "tag1" in result


@pytest.mark.asyncio
async def test_handle_retrieve_memories_not_found():
    mock_list_all = AsyncMock()
    mock_list_all.return_value = []

    with patch("memory_mcp._list_all_memories", mock_list_all):
        args = {"category": "test_cat", "is_global": False}
        result = await handle_retrieve_memories(args)

        assert "No memories found for category 'test_cat'" in result


@pytest.mark.asyncio
async def test_handle_remove_specific_memory_not_found():
    mock_list_all = AsyncMock()
    mock_list_all.return_value = []

    with patch("memory_mcp._list_all_memories", mock_list_all):
        args = {"category": "test_cat", "memory_content": "test_data", "is_global": False}
        result = await handle_remove_specific_memory(args)

        assert "No matching memory found" in result


@pytest.mark.asyncio
async def test_handle_remove_specific_memory_error():
    key = _make_key("test_cat", is_global=False, data="test_data")
    value = _memory_value("test_data", [])

    mock_list_all = AsyncMock()
    mock_list_all.return_value = [{"key": key, "value": value}]

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Error"
    mock_client.delete.return_value = mock_response

    with patch("memory_mcp._list_all_memories", mock_list_all):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client_class.return_value.__aenter__.return_value = mock_client

            args = {"category": "test_cat", "memory_content": "test_data", "is_global": False}
            result = await handle_remove_specific_memory(args)

            assert "Error removing memory" in result


@pytest.mark.asyncio
async def test_handle_request_initialize():
    req = {"method": "initialize"}
    result = await handle_request(req)
    assert result is not None
    assert "protocolVersion" in result
    assert "capabilities" in result
    assert "serverInfo" in result


@pytest.mark.asyncio
async def test_handle_request_tools_list():
    req = {"method": "tools/list"}
    result = await handle_request(req)
    assert result is not None
    assert "tools" in result
    tools = {t["name"] for t in result["tools"]}
    assert tools == {"remember_memory", "retrieve_memories", "remove_memory_category", "remove_specific_memory"}


@pytest.mark.asyncio
async def test_handle_request_tools_call():
    req = {
        "method": "tools/call",
        "params": {"name": "remember_memory", "arguments": {"category": "test", "data": "test_data"}},
    }

    mock_handler = AsyncMock()
    mock_handler.return_value = "Memory remembered"

    with patch("memory_mcp.handle_remember_memory", mock_handler):
        result = await handle_request(req)

        assert result is not None
        assert not result.get("isError")
        assert result["content"][0]["text"] == "Memory remembered"


@pytest.mark.asyncio
async def test_handle_request_tools_call_unknown():
    req = {"method": "tools/call", "params": {"name": "unknown_tool", "arguments": {}}}

    result = await handle_request(req)
    assert result is not None
    assert result["isError"] is True
    assert "Unknown tool" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_handle_request_tools_call_error():
    req = {"method": "tools/call", "params": {"name": "remember_memory", "arguments": {}}}

    mock_handler = AsyncMock()
    mock_handler.side_effect = Exception("Test Error")

    with patch("memory_mcp.handle_remember_memory", mock_handler):
        result = await handle_request(req)

        assert result is not None
        assert result["isError"] is True
        assert "Test Error" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_main_loop():
    # Simulate a stream of stdin JSON-RPC requests
    mock_stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
        + "invalid json\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notification_no_id"})
        + "\n"
    )

    mock_stdout = io.StringIO()

    with patch("sys.stdin", mock_stdin):
        with patch("sys.stdout", mock_stdout):
            await main_loop()

    output = mock_stdout.getvalue()
    responses = [json.loads(line) for line in output.strip().split("\n") if line]

    assert len(responses) == 2
    assert responses[0]["id"] == 1
    assert "protocolVersion" in responses[0]["result"]
    assert responses[1]["id"] == 2
    assert "tools" in responses[1]["result"]


def test_log():
    mock_stderr = io.StringIO()
    with patch("sys.stderr", mock_stderr):
        log("Test message")

    assert "[memory-mcp] Test message\n" in mock_stderr.getvalue()


@pytest.mark.asyncio
async def test_main_loop_errors():
    mock_stdin = io.StringIO(
        "\n"  # Empty line to hit continue
        + "invalid json format\n"  # JSON parse error
        + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "test_unhandled_error"})
        + "\n"  # triggers Exception
    )

    mock_stdout = io.StringIO()
    mock_stderr = io.StringIO()

    # We want handle_request to raise an Exception for id 3
    async def mock_handle_request(req):
        if req.get("method") == "test_unhandled_error":
            raise RuntimeError("Fake unexpected error")
        return None

    with (
        patch("sys.stdin", mock_stdin),
        patch("sys.stdout", mock_stdout),
        patch("sys.stderr", mock_stderr),
        patch("memory_mcp.handle_request", side_effect=mock_handle_request),
    ):
        await main_loop()

    err_output = mock_stderr.getvalue()
    assert "JSON parse error" in err_output
    assert "Unexpected error" in err_output


@pytest.mark.asyncio
async def test_handle_request_tools_call_retrieve_memories():
    req = {"method": "tools/call", "params": {"name": "retrieve_memories", "arguments": {"category": "test"}}}

    mock_handler = AsyncMock()
    mock_handler.return_value = "Memories retrieved"

    with patch("memory_mcp.handle_retrieve_memories", mock_handler):
        result = await handle_request(req)

        assert result is not None
        assert not result.get("isError")
        assert result["content"][0]["text"] == "Memories retrieved"


@pytest.mark.asyncio
async def test_handle_request_tools_call_remove_memory_category():
    req = {"method": "tools/call", "params": {"name": "remove_memory_category", "arguments": {"category": "test"}}}

    mock_handler = AsyncMock()
    mock_handler.return_value = "Category removed"

    with patch("memory_mcp.handle_remove_memory_category", mock_handler):
        result = await handle_request(req)

        assert result is not None
        assert not result.get("isError")
        assert result["content"][0]["text"] == "Category removed"


@pytest.mark.asyncio
async def test_handle_request_tools_call_remove_specific_memory():
    req = {
        "method": "tools/call",
        "params": {"name": "remove_specific_memory", "arguments": {"category": "test", "memory_content": "data"}},
    }

    mock_handler = AsyncMock()
    mock_handler.return_value = "Memory removed"

    with patch("memory_mcp.handle_remove_specific_memory", mock_handler):
        result = await handle_request(req)

        assert result is not None
        assert not result.get("isError")
        assert result["content"][0]["text"] == "Memory removed"


@pytest.mark.asyncio
async def test_handle_retrieve_memories_filters():
    key_local_cat1 = _make_key("cat1", is_global=False, data="d1")
    key_global_cat1 = _make_key("cat1", is_global=True, data="d2")
    key_local_cat2 = _make_key("cat2", is_global=False, data="d3")
    key_invalid = "not_a_memory"

    memories = [
        {"key": key_local_cat1, "value": _memory_value("d1", [])},
        {"key": key_global_cat1, "value": _memory_value("d2", [])},
        {"key": key_local_cat2, "value": _memory_value("d3", [])},
        {"key": key_invalid, "value": "{}"},
    ]

    mock_list_all = AsyncMock()
    mock_list_all.return_value = memories

    with patch("memory_mcp._list_all_memories", mock_list_all):
        # Test scope mismatch (looking for local cat1, global should be skipped)
        res1 = await handle_retrieve_memories({"category": "cat1", "is_global": False})
        assert "d1" in res1
        assert "d2" not in res1

        # Test category mismatch (looking for cat1, cat2 should be skipped)
        res2 = await handle_retrieve_memories({"category": "cat1", "is_global": False})
        assert "d3" not in res2

        # Test category "*" (looking for all local)
        res3 = await handle_retrieve_memories({"category": "*", "is_global": False})
        assert "d1" in res3
        assert "d3" in res3
        assert "d2" not in res3


@pytest.mark.asyncio
async def test_delete_item_httpx_error():
    key = _make_key("cat1", is_global=False, data="d1")
    memories = [{"key": key, "value": _memory_value("d1", [])}]

    mock_list_all = AsyncMock()
    mock_list_all.return_value = memories

    mock_client = AsyncMock()
    # Making delete raise httpx.HTTPError
    mock_client.delete.side_effect = httpx.HTTPError("Network failure")

    with patch("memory_mcp._list_all_memories", mock_list_all):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client_class.return_value.__aenter__.return_value = mock_client

            res = await handle_remove_memory_category({"category": "cat1", "is_global": False})

            assert "Error removing memory" in res
            assert "Network failure" in res


@pytest.mark.asyncio
async def test_handle_remove_memory_category_filters():
    key_local_cat1 = _make_key("cat1", is_global=False, data="d1")
    key_global_cat1 = _make_key("cat1", is_global=True, data="d2")
    key_invalid = "not_a_memory"

    memories = [
        {"key": key_local_cat1, "value": _memory_value("d1", [])},
        {"key": key_global_cat1, "value": _memory_value("d2", [])},
        {"key": key_invalid, "value": "{}"},
    ]

    mock_list_all = AsyncMock()
    mock_list_all.return_value = memories

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.delete.return_value = mock_response

    # Test not finding any to delete (wrong scope)
    with patch("memory_mcp._list_all_memories", mock_list_all):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client_class.return_value.__aenter__.return_value = mock_client

            res1 = await handle_remove_memory_category({"category": "cat2", "is_global": False})
            assert "No memories found to remove" in res1

            # Test scope skipping coverage inside the loop
            res2 = await handle_remove_memory_category({"category": "cat1", "is_global": False})
            # Should only try to delete the local one, not the global one
            assert "Removed 1 memory" in res2


@pytest.mark.asyncio
async def test_handle_remove_specific_memory_filters():
    key_local_cat1 = _make_key("cat1", is_global=False, data="d1")
    key_global_cat1 = _make_key("cat1", is_global=True, data="d2")
    key_local_cat2 = _make_key("cat2", is_global=False, data="d3")
    key_invalid = "not_a_memory"

    memories = [
        {"key": key_local_cat1, "value": _memory_value("d1", [])},
        {"key": key_global_cat1, "value": _memory_value("d2", [])},
        {"key": key_local_cat2, "value": _memory_value("d3", [])},
        {"key": key_invalid, "value": "{}"},
    ]

    mock_list_all = AsyncMock()
    mock_list_all.return_value = memories

    with patch("memory_mcp._list_all_memories", mock_list_all):
        # Scope mismatch (looking for d2 in local, it's global)
        res1 = await handle_remove_specific_memory({"category": "cat1", "memory_content": "d2", "is_global": False})
        assert "No matching memory found" in res1

        # Category mismatch (looking for d3 in cat1, it's cat2)
        res2 = await handle_remove_specific_memory({"category": "cat1", "memory_content": "d3", "is_global": False})
        assert "No matching memory found" in res2


def test_parse_memory_value_none():
    res = _parse_memory_value(None)
    assert res == {"data": "", "tags": []}


@pytest.mark.asyncio
async def test_handle_request_none():
    req = {"method": "unknown_method"}
    res = await handle_request(req)
    assert res is None


def test_parse_memory_value_no_tags_field():
    val = json.dumps({"data": "test_data"})
    res = _parse_memory_value(val)
    assert res == {"data": "test_data", "tags": []}
