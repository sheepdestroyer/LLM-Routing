import pytest
from router.main import estimate_prompt_tokens

@pytest.mark.parametrize(
    "body, expected_tokens",
    [
        pytest.param({}, 50, id="empty_dictionary"),
        pytest.param({"other_key": "value"}, 50, id="missing_messages_key"),
        pytest.param({"messages": []}, 50, id="empty_messages_list"),
        pytest.param(
            {
                "messages": [
                    {"content": "word " * 4},  # 4 * 1.2 = 4.8
                    {"content": "word " * 8},  # 8 * 1.2 = 9.6
                ]
            },
            50 + 14,  # int(round(4.8 + 9.6)) + 50 = 64
            id="string_content_with_predictable_word_counts",
        ),
        pytest.param(
            {
                "messages": [
                    {
                        "content": [
                            {"type": "text", "text": "word " * 4},  # 4.8 tokens
                            {"type": "image_url", "url": "ignored"},  # 0 tokens
                            {"type": "text", "text": "word " * 8},  # 9.6 tokens
                        ]
                    }
                ]
            },
            50 + 14,  # Total is int(round(4.8 + 9.6)) + 50 = 64
            id="list_content_ignoring_non_text_blocks",
        ),
        pytest.param(
            {
                "messages": [
                    "invalid_message_type",  # Should be skipped
                    {"content": None},  # Empty/None content
                    {
                        "content": [
                            "invalid_block_type",  # Should be skipped
                            {"type": "text", "text": None},  # None text, handled as empty string
                        ]
                    },
                    {"content": "word " * 4},  # 4.8 tokens
                ]
            },
            50 + 5,  # Total is int(round(4.8)) + 50 = 5 + 50 = 55
            id="mixed_and_invalid_message_formats_handled_gracefully",
        ),
        pytest.param(
            {
                "messages": [
                    {"role": "user"}  # No content key
                ]
            },
            50,
            id="message_with_no_content_key",
        ),
        pytest.param(
            {
                "messages": [
                    {"content": {"key": "value"}}
                ]
            },
            50,
            id="unsupported_content_type_ignored",
        ),
    ],
)
def test_estimate_prompt_tokens(body, expected_tokens):
    """
    Test estimate_prompt_tokens logic across different message payload structures.
    """
    assert estimate_prompt_tokens(body) == expected_tokens
