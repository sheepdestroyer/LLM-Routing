import os
os.environ["CONFIG_PATH"] = "router/config.yaml"

import pytest
from router.main import detect_active_tool, map_tool_to_category

@pytest.mark.parametrize(
    "tool_name,expected_category",
    [
        ("tree", "tree"),
        ("list_dir", "tree"),
        ("list-dir", "tree"),
        ("shell", "shell"),
        ("command", "shell"),
        ("run", "shell"),
        ("execute", "shell"),
        ("cmd", "shell"),
        ("write", "write"),
        ("create", "write"),
        ("save", "write"),
        ("edit", "write"),
        ("patch", "write"),
        ("replace", "write"),
        ("view", "view"),
        ("cat", "view"),
        ("grep", "view"),
        ("read", "view"),
        ("search", "view"),
        ("find", "view"),
        ("something_else", "other"),
        ("unknown__list_dir", "tree"),
    ],
)
def test_map_tool_to_category(tool_name, expected_category):
    """Verify tool names are mapped correctly to high-level categories."""
    assert map_tool_to_category(tool_name) == expected_category

def test_detect_active_tool_tool_role_with_name():
    """Verify detect_active_tool correctly identifies a tool when role is 'tool' and name is provided."""
    payload = {
        "messages": [
            {"role": "tool", "name": "run_command", "content": "file1.txt\nfile2.txt"}
        ]
    }
    assert detect_active_tool(payload) == "shell"

def test_detect_active_tool_tool_role_without_name():
    """Verify detect_active_tool looks backward for the assistant tool request when name is absent."""
    payload = {
        "messages": [
            {"role": "user", "content": "run this cmd"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "run_command"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "output"}
        ]
    }
    assert detect_active_tool(payload) == "shell"

def test_detect_active_tool_tool_role_without_name_no_match():
    """Verify detect_active_tool handles the case where it can't find the backward assistant tool request."""
    payload = {
        "messages": [
            {"role": "user", "content": "run this cmd"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_2", "function": {"name": "run_command"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "output"}
        ]
    }
    assert detect_active_tool(payload) == "other"

def test_detect_active_tool_assistant_role():
    """Verify detect_active_tool correctly identifies a tool when role is 'assistant' with tool_calls."""
    payload = {
        "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "write_file"}}
            ]}
        ]
    }
    assert detect_active_tool(payload) == "write"

@pytest.mark.parametrize(
    "user_message,expected_category",
    [
        ("tree", "tree"),
        ("files", "tree"),
        ("shell", "shell"),
        ("run", "shell"),
        ("cmd", "shell"),
        ("write", "write"),
        ("create file", "write"),
        ("view", "view"),
        ("read", "view"),
        ("cat", "view"),
    ],
)
def test_detect_active_tool_user_fallback(user_message, expected_category):
    """Verify detect_active_tool falls back to keyword matching in user messages."""
    assert detect_active_tool({"messages": [{"role": "user", "content": user_message}]}) == expected_category

def test_detect_active_tool_empty_and_malformed():
    """Verify detect_active_tool handles empty or malformed inputs correctly."""
    assert detect_active_tool({}) == "none"
    assert detect_active_tool({"messages": []}) == "none"
    assert detect_active_tool({"messages": ["not a dict"]}) == "none"
    assert detect_active_tool({"messages": [{"role": "user", "content": "hello"}]}) == "none"
    # test nested malformed objects
    assert detect_active_tool({"messages": [{"role": "assistant", "tool_calls": ["not a dict"]}]}) == "none"
    assert detect_active_tool({"messages": [{"role": "assistant", "tool_calls": [{"id": "call_1", "function": "not a dict"}]}]}) == "other"
