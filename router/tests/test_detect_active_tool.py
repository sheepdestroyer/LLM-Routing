import pytest
from router.main import detect_active_tool

def test_detect_active_tool_empty_body():
    """Test with empty body or empty messages."""
    assert detect_active_tool({}) == "none"
    assert detect_active_tool({"messages": []}) == "none"

def test_detect_active_tool_invalid_messages():
    """Test with messages that are not dictionaries."""
    assert detect_active_tool({"messages": ["not a dict", 123, None]}) == "none"

def test_detect_active_tool_explicit_name():
    """Test when the tool/function role has an explicit name."""
    body = {
        "messages": [
            {"role": "tool", "name": "run_command"}
        ]
    }
    assert detect_active_tool(body) == "shell"

    body = {
        "messages": [
            {"role": "function", "name": "list_dir"}
        ]
    }
    assert detect_active_tool(body) == "tree"

def test_detect_active_tool_matched_by_id():
    """Test when the tool role is missing a name but has a matching tool_call_id."""
    body = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_123", "function": {"name": "read_file"}},
                    {"id": "call_456", "function": {"name": "write_file"}}
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_456"
            }
        ]
    }
    # It scans backwards, matches call_456 to write_file -> "write"
    assert detect_active_tool(body) == "write"

def test_detect_active_tool_unmatched_by_id():
    """Test when the tool role lacks a name and cannot be matched."""
    body = {
        "messages": [
            {
                "role": "tool",
                "tool_call_id": "call_999"
            }
        ]
    }
    assert detect_active_tool(body) == "other"

def test_detect_active_tool_assistant_tool_calls():
    """Test when the last relevant message is an assistant calling a tool."""
    body = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "patch_file"}}
                ]
            }
        ]
    }
    assert detect_active_tool(body) == "write"

    # Also check when it's an invalid tool_calls format
    body_invalid = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": "not a list"
            }
        ]
    }
    assert detect_active_tool(body_invalid) == "none"

def test_detect_active_tool_user_fallback():
    """Test fallback keyphrase scanning in user messages."""
    body = {
        "messages": [
            {"role": "user", "content": "can you run a cmd for me?"}
        ]
    }
    assert detect_active_tool(body) == "shell"

    body = {
        "messages": [
            {"role": "user", "content": "please view this log"}
        ]
    }
    assert detect_active_tool(body) == "view"

    body = {
        "messages": [
            {"role": "user", "content": "let's do a tree command"}
        ]
    }
    assert detect_active_tool(body) == "tree"

    body = {
        "messages": [
            {"role": "user", "content": "create file test.txt"}
        ]
    }
    assert detect_active_tool(body) == "write"

def test_detect_active_tool_user_fallback_priority():
    """Test that the most recent user message takes priority."""
    body = {
        "messages": [
            {"role": "user", "content": "view the file"},
            {"role": "user", "content": "no wait, tree the directory instead"}
        ]
    }
    assert detect_active_tool(body) == "tree"

def test_detect_active_tool_user_fallback_no_match():
    """Test fallback when user message has no matching keyphrases."""
    body = {
        "messages": [
            {"role": "user", "content": "hello how are you"}
        ]
    }
    assert detect_active_tool(body) == "none"

def test_detect_active_tool_assistant_tool_call_without_function():
    """Test assistant tool call where function dict is missing."""
    body = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1"}
                ]
            }
        ]
    }
    assert detect_active_tool(body) == "other"

def test_detect_active_tool_assistant_matching_invalid_prev_msg():
    """Test matching tool_call_id but prev_msg is invalid."""
    body = {
        "messages": [
            "invalid message",
            {
                "role": "tool",
                "tool_call_id": "call_1"
            }
        ]
    }
    assert detect_active_tool(body) == "other"


def test_detect_active_tool_with_underscores():
    """Test map_tool_to_category handling of '__' prefix/suffix."""
    body = {
        "messages": [
            {"role": "tool", "name": "module__submodule__search_files"}
        ]
    }
    assert detect_active_tool(body) == "view"

def test_detect_active_tool_view_explicit():
    """Test map_tool_to_category handling view specifically."""
    body = {
        "messages": [
            {"role": "tool", "name": "grep_logs"}
        ]
    }
    assert detect_active_tool(body) == "view"

if __name__ == '__main__':
    pytest.main(['-v', __file__])
