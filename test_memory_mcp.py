#!/usr/bin/env python3
"""
Tests for memory_mcp.py
"""

import sys
from pathlib import Path

# Need to import memory_mcp from the router module
sys.path.insert(0, str(Path(__file__).resolve().parent))

from router.memory_mcp import _parse_key
import pytest

def test_parse_key_happy_path():
    """Test full standard key structure"""
    key = "memory:local:code::20240101T120000Z:abc123hash"
    result = _parse_key(key)
    assert result == {
        "scope": "local",
        "category": "code",
        "timestamp": "20240101T120000Z"
    }

def test_parse_key_missing_timestamp_hash():
    """Test key without the :: delimiter section"""
    key = "memory:global:general"
    result = _parse_key(key)
    assert result == {
        "scope": "global",
        "category": "general",
        "timestamp": ""
    }

def test_parse_key_missing_category():
    """Test key with missing category"""
    key = "memory:local::20240101T120000Z:abc123hash"
    result = _parse_key(key)
    # The split(":") on "memory:local" results in ["memory", "local"] length 2
    # So category should be ""
    assert result == {
        "scope": "local",
        "category": "",
        "timestamp": "20240101T120000Z"
    }

def test_parse_key_missing_scope_and_category():
    """Test minimal key prefix"""
    key = "memory"
    result = _parse_key(key)
    assert result == {
        "scope": "",
        "category": "",
        "timestamp": ""
    }

def test_parse_key_empty_string():
    """Test completely empty string"""
    key = ""
    result = _parse_key(key)
    assert result == {
        "scope": "",
        "category": "",
        "timestamp": ""
    }

def test_parse_key_invalid_type():
    """Test handling of an invalid type that triggers the exception branch"""
    key = None
    result = _parse_key(key)
    assert result == {
        "scope": "",
        "category": "",
        "timestamp": ""
    }

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
