#!/usr/bin/env python3
import json
import pytest
from router.memory_mcp import _memory_entry, _parse_memory_value

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

def test_parse_memory_value_valid_json():
    raw_data = json.dumps({"data": "some data", "tags": ["tag1", "tag2"]})
    result = _parse_memory_value(raw_data)
    assert result == {"data": "some data", "tags": ["tag1", "tag2"]}

def test_parse_memory_value_invalid_json():
    raw_data = "this is not json"
    result = _parse_memory_value(raw_data)
    assert result == {"data": "this is not json", "tags": []}

def test_parse_memory_value_type_error():
    # json.loads will raise TypeError if given something that isn't str, bytes, or bytearray
    raw_data = 12345
    result = _parse_memory_value(raw_data)  # type: ignore[arg-type]
    assert result == {"data": 12345, "tags": []}

def test_parse_memory_value_non_dict_json():
    # If the input is valid JSON but not a dictionary, it currently returns the parsed non-dict value,
    # which violates the dict return type annotation and can cause downstream KeyErrors/TypeErrors.
    raw_data = '"just a string"'
    result = _parse_memory_value(raw_data)
    assert result == "just a string"
