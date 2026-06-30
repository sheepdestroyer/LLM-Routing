import pytest
from router.main import detect_active_tool

def test_detect_active_tool_empty():
    assert detect_active_tool({}) == "none"
    assert detect_active_tool({"messages": []}) == "none"
    assert detect_active_tool({"messages": [{"role": "system", "content": "hello"}]}) == "none"

def test_detect_active_tool_role_tool_with_name():
    # Write tool name mapped to write
    body = {
        "messages": [
            {"role": "tool", "name": "edit_file", "content": "..."}
        ]
    }
    assert detect_active_tool(body) == "write"

    # View tool mapped to view
    body = {
        "messages": [
            {"role": "tool", "name": "cat_file", "content": "..."}
        ]
    }
    assert detect_active_tool(body) == "view"

def test_detect_active_tool_role_tool_without_name_but_matched_tool_call_id():
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "call_123", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "call_123", "content": "success"}
        ]
    }
    assert detect_active_tool(body) == "view"

def test_detect_active_tool_role_tool_without_name_unmatched_tool_call_id():
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "call_999", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "call_123", "content": "success"}
        ]
    }
    assert detect_active_tool(body) == "other"

def test_detect_active_tool_role_assistant_with_tool_calls():
    body = {
        "messages": [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "write_to_file"}}]}
        ]
    }
    assert detect_active_tool(body) == "write"

@pytest.mark.parametrize(
    "content,expected",
    [
        ("show me the tree", "tree"),
        ("run this in shell", "shell"),
        ("create file test.py", "write"),
        ("cat main.py", "view"),
    ],
)
def test_detect_active_tool_fallback_user_keyword(content, expected):
    body = {
        "messages": [
            {"role": "user", "content": content}
        ]
    }
    assert detect_active_tool(body) == expected

def test_detect_active_tool_ignores_invalid_message_formats():
    body = {
        "messages": [
            "this is not a dict",
            {"role": "user", "content": "read this"}
        ]
    }
    assert detect_active_tool(body) == "view"

def test_detect_active_tool_precedence():
    # If there are multiple tools, it processes from the last message backwards
    body = {
        "messages": [
            {"role": "tool", "name": "edit_file", "content": "done"},
            {"role": "tool", "name": "cat_file", "content": "..."}
        ]
    }
    # It starts from the end, so it sees "cat_file" first, which maps to "view"
    assert detect_active_tool(body) == "view"
