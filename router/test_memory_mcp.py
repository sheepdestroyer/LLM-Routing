import pytest
import time
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_mcp import _make_key, SCOPE_GLOBAL, SCOPE_LOCAL, PREFIX

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
    # Format is memory:global:test_cat::1717612345:a1b2c3d4
    match = re.match(rf"^{PREFIX}:{SCOPE_GLOBAL}:{category}::(\d+):([a-z0-9x]+)$", key)
    assert match is not None, f"Key {key} does not match expected format"

    ts = int(match.group(1))
    h = match.group(2)

    assert before_ts <= ts <= after_ts
    assert len(h) <= 12

def test_make_key_local():
    """Test generating a key for local scope."""
    category = "another_cat"
    data = "more_data"

    before_ts = int(time.time() * 1000)
    key = _make_key(category, False, data)
    after_ts = int(time.time() * 1000)

    assert key.startswith(f"{PREFIX}:{SCOPE_LOCAL}:{category}::")

    match = re.match(rf"^{PREFIX}:{SCOPE_LOCAL}:{category}::(\d+):([a-z0-9x]+)$", key)
    assert match is not None, f"Key {key} does not match expected format"

    ts = int(match.group(1))
    h = match.group(2)

    assert before_ts <= ts <= after_ts
    assert len(h) <= 12

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
