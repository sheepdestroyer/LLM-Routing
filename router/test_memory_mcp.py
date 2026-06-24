import json
from router.memory_mcp import _memory_value, _parse_memory_value

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
    # Looking at _parse_memory_value, let's see what it returns when an error occurs
    # We should look at router/memory_mcp.py to verify its behavior for invalid JSON
    assert result == {"data": "{invalid_json:", "tags": []}

def test_parse_memory_value_type_error():
    """Test _parse_memory_value with TypeError (e.g. passing None)."""
    result = _parse_memory_value(None)
    assert result == {"data": None, "tags": []}
