import pytest
from router.main import detect_active_tool


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, "none"),
        ({"messages": []}, "none"),
        ({"messages": [{"role": "system", "content": "hello"}]}, "none"),
    ],
)
def test_detect_active_tool_empty_or_no_tool(body, expected):
    assert detect_active_tool(body) == expected


@pytest.mark.parametrize(
    "tool_name,expected_category",
    [
        ("edit_file", "write"),
        ("cat_file", "view"),
    ],
)
def test_detect_active_tool_role_tool_with_name(tool_name, expected_category):
    body = {"messages": [{"role": "tool", "name": tool_name, "content": "..."}]}
    assert detect_active_tool(body) == expected_category


@pytest.mark.parametrize(
    "messages,expected",
    [
        (
            [
                {"role": "assistant", "tool_calls": [{"id": "call_123", "function": {"name": "read_file"}}]},
                {"role": "tool", "tool_call_id": "call_123", "content": "success"},
            ],
            "view",
        ),
        (
            [
                {"role": "assistant", "tool_calls": [{"id": "call_999", "function": {"name": "read_file"}}]},
                {"role": "tool", "tool_call_id": "call_123", "content": "success"},
            ],
            "other",
        ),
        (
            [
                "not a dict",
                {"role": "tool", "tool_call_id": "call_123", "content": "success"},
            ],
            "other",
        ),
        (
            [
                {"role": "assistant", "tool_calls": "not a list"},
                {"role": "tool", "tool_call_id": "call_123", "content": "success"},
            ],
            "other",
        ),
        (
            [
                {"role": "assistant", "tool_calls": ["not a dict"]},
                {"role": "tool", "tool_call_id": "call_123", "content": "success"},
            ],
            "other",
        ),
        (
            [
                {"role": "assistant", "tool_calls": [{"id": "call_123", "function": "not a dict"}]},
                {"role": "tool", "tool_call_id": "call_123", "content": "success"},
            ],
            "other",
        ),
    ],
)
def test_detect_active_tool_role_tool_tool_call_id_resolution(messages, expected):
    assert detect_active_tool({"messages": messages}) == expected


@pytest.mark.parametrize(
    "messages,expected",
    [
        (
            [
                {"role": "user", "content": "do something"},
                {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "write_to_file"}}]},
            ],
            "write",
        ),
        (
            [
                {"role": "assistant", "tool_calls": ["not a dict", {"function": {"name": "read_file"}}]},
            ],
            "view",
        ),
        (
            [
                {"role": "assistant", "tool_calls": [{"function": "not a dict"}]},
            ],
            "other",
        ),
        (
            [
                {"role": "assistant", "tool_calls": "not a list"},
            ],
            "none",
        ),
    ],
)
def test_detect_active_tool_role_assistant_tool_calls(messages, expected):
    assert detect_active_tool({"messages": messages}) == expected


@pytest.mark.parametrize(
    "user_content,expected_category",
    [
        ("show me the tree", "tree"),
        ("run this in shell", "shell"),
        ("create file test.py", "write"),
        ("cat main.py", "view"),
    ],
)
def test_detect_active_tool_fallback_user_keyword(user_content, expected_category):
    body = {"messages": [{"role": "user", "content": user_content}]}
    assert detect_active_tool(body) == expected_category


def test_detect_active_tool_ignores_non_dict_messages_in_history():
    body = {
        "messages": [
            "this is not a dict",
            {"role": "user", "content": "read this"},
        ]
    }
    assert detect_active_tool(body) == "view"


def test_detect_active_tool_user_fallback_skips_non_dict_messages():
    body = {
        "messages": [
            {"role": "user", "content": "read this"},
            "not a dict",
        ]
    }
    assert detect_active_tool(body) == "view"


def test_detect_active_tool_precedence_evaluates_most_recent_tool_message_first():
    body = {
        "messages": [
            {"role": "tool", "name": "edit_file", "content": "done"},
            {"role": "tool", "name": "cat_file", "content": "..."},
        ]
    }
    assert detect_active_tool(body) == "view"
