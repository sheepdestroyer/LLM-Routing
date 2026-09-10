"""Unit tests for scripts/chat_helpers.py.

Tests coverage for defensive chat completion parsing and content normalization.
"""

from typing import Any
import pytest
from scripts.chat_helpers import _normalize_chat_content, parse_chat_response


class TestNormalizeChatContent:
    """Tests for _normalize_chat_content."""

    def test_none_returns_empty_string(self):
        assert _normalize_chat_content(None) == ""

    @pytest.mark.parametrize(
        ("raw_input", "expected"),
        [
            ("hello world", "hello world"),
            ("   leading and trailing whitespace   ", "leading and trailing whitespace"),
            ("", ""),
            ("   \n\t  ", ""),
        ],
    )
    def test_string_input(self, raw_input: str, expected: str):
        assert _normalize_chat_content(raw_input) == expected

    def test_list_of_strings(self):
        data = ["Hello ", "beautiful ", "world! "]
        assert _normalize_chat_content(data) == "Hello beautiful world!"

    def test_list_of_dicts_with_text(self):
        data = [
            {"type": "text", "text": "Step 1: "},
            {"type": "text", "text": " Do this. "},
        ]
        assert _normalize_chat_content(data) == "Step 1:  Do this."

    def test_list_of_dicts_with_content(self):
        data = [
            {"content": "First part. "},
            {"content": [{"text": "Nested part."}]},
        ]
        assert _normalize_chat_content(data) == "First part. Nested part."

    def test_list_of_dicts_with_empty_or_none_content(self):
        # Covers the False branch of `if nested:` in list processing
        data = [
            {"content": None},
            {"content": ""},
            {"content": "   "},
            {"content": []},
            {"content": {"other": 123}},
        ]
        assert _normalize_chat_content(data) == ""

    def test_list_mixed_items(self):
        data = [
            "Plain prefix: ",
            {"type": "text", "text": "Dict text. "},
            {"type": "custom", "content": "Nested content."},
            {"unrecognized_key": "ignored"},
            123,  # non-string, non-dict item ignored
            None,  # ignored
        ]
        assert _normalize_chat_content(data) == "Plain prefix: Dict text. Nested content."

    def test_list_with_unrecognized_dict_blocks(self):
        # Blocks without "text" or "content" keys are cleanly ignored
        data = [
            {"type": "thinking", "thinking": "Let me ponder this."},  # No text or content -> ignored
            {"type": "text", "text": "Here is the final answer."},
        ]
        assert _normalize_chat_content(data) == "Here is the final answer."

    def test_dict_with_text(self):
        data = {"text": "  Single text dict  "}
        assert _normalize_chat_content(data) == "Single text dict"

    def test_dict_with_content(self):
        data = {"content": "  Content value in dict  "}
        assert _normalize_chat_content(data) == "Content value in dict"

    def test_dict_with_nested_content(self):
        data = {"content": [{"text": "Nested within dict"}]}
        assert _normalize_chat_content(data) == "Nested within dict"

    def test_dict_with_non_string_text_falling_back_to_content(self):
        # text is not a str, so it checks "content" in value
        data = {"text": 12345, "content": "Fallback content"}
        assert _normalize_chat_content(data) == "Fallback content"

    def test_dict_with_non_string_text_and_no_content(self):
        data = {"text": 12345}
        assert _normalize_chat_content(data) == ""

    def test_dict_with_unrecognized_keys(self):
        data = {"role": "assistant", "name": "bot"}
        assert _normalize_chat_content(data) == ""

    @pytest.mark.parametrize(
        "invalid_value",
        [
            42,
            3.14159,
            True,
            False,
            object(),
            (1, 2, 3),
        ],
    )
    def test_unsupported_types_return_empty_string(self, invalid_value: Any):
        assert _normalize_chat_content(invalid_value) == ""


class TestParseChatResponse:
    """Tests for parse_chat_response."""

    @pytest.mark.parametrize(
        "invalid_data",
        [
            None,
            "not a dict",
            [{"choices": []}],
            123,
            True,
        ],
    )
    def test_non_dict_data_returns_empty_tuple(self, invalid_data: Any):
        assert parse_chat_response(invalid_data) == ("", "")

    @pytest.mark.parametrize(
        "invalid_choices",
        [
            {},
            {"choices": None},
            {"choices": []},
            {"choices": "not a list"},
            {"choices": 123},
            {"choices": {}},
            {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error", "code": 429}},
        ],
    )
    def test_invalid_or_missing_choices_returns_empty_tuple(self, invalid_choices: dict[str, Any]):
        assert parse_chat_response(invalid_choices) == ("", "")

    @pytest.mark.parametrize(
        "first_choice_invalid",
        [
            {"choices": [None]},
            {"choices": ["string instead of dict"]},
            {"choices": [123]},
            {"choices": [[]]},
        ],
    )
    def test_invalid_first_choice_returns_empty_tuple(self, first_choice_invalid: dict[str, Any]):
        assert parse_chat_response(first_choice_invalid) == ("", "")

    @pytest.mark.parametrize(
        "invalid_message",
        [
            {"choices": [{}]},
            {"choices": [{"message": None}]},
            {"choices": [{"message": "string"}]},
            {"choices": [{"message": 123}]},
            {"choices": [{"message": []}]},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"role": "assistant"}}]},
        ],
    )
    def test_invalid_or_missing_message_returns_empty_tuple(self, invalid_message: dict[str, Any]):
        assert parse_chat_response(invalid_message) == ("", "")

    def test_standard_dict_response(self):
        data = {
            "id": "chatcmpl-123",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello! How can I help you today?",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        assert parse_chat_response(data) == ("Hello! How can I help you today?", "")

    def test_multi_choice_response_extracts_first_choice_only(self):
        data = {
            "choices": [
                {"message": {"role": "assistant", "content": "First candidate"}},
                {"message": {"role": "assistant", "content": "Second candidate"}},
            ]
        }
        assert parse_chat_response(data) == ("First candidate", "")

    def test_response_with_reasoning_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The answer is 42.",
                        "reasoning_content": "Calculating deep thought...",
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("The answer is 42.", "Calculating deep thought...")

    def test_response_with_reasoning_only_and_none_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "Evaluating prompt with zero token budget left...",
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("", "Evaluating prompt with zero token budget left...")

    def test_response_with_reasoning_only_and_empty_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Thinking steps only...",
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("", "Thinking steps only...")

    def test_response_with_explicit_none_reasoning(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Standard answer",
                        "reasoning_content": None,
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("Standard answer", "")

    def test_response_with_structured_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Structured "},
                            {"type": "text", "text": "output"},
                        ],
                        "reasoning_content": [
                            {"type": "text", "text": "Thought step 1. "},
                            {"type": "text", "text": "Thought step 2."},
                        ],
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("Structured output", "Thought step 1. Thought step 2.")

    def test_response_with_tool_calls_and_none_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Paris"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("", "")

    def test_response_with_tool_calls_and_text_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking the weather for you...",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Paris"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("Checking the weather for you...", "")

    def test_response_with_whitespace_stripping(self):
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "   \n Answer with whitespace \t  \n ",
                        "reasoning_content": "  \n Reason with whitespace \t ",
                    }
                }
            ]
        }
        assert parse_chat_response(data) == ("Answer with whitespace", "Reason with whitespace")
