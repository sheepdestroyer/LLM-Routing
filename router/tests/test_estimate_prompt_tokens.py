import pytest
from router.main import estimate_prompt_tokens

@pytest.mark.parametrize("body, expected_tokens, description", [
    ({}, 50, "Empty dictionary"),
    ({"other_key": "value"}, 50, "Missing 'messages' key"),
    ({"messages": []}, 50, "Empty 'messages' list"),
    (
        {
            "messages": [
                {"content": "word " * 4},  # 4 * 1.2 = 4.8
                {"content": "word " * 8}   # 8 * 1.2 = 9.6
            ]
        },
        50 + 14, # int(round(4.8 + 9.6)) + 50 = 64
        "String content with predictable word counts"
    ),
    (
        {
            "messages": [
                {
                    "content": [
                        {"type": "text", "text": "word " * 4},  # 4.8 tokens
                        {"type": "image_url", "url": "ignored"},  # 0 tokens
                        {"type": "text", "text": "word " * 8}  # 9.6 tokens
                    ]
                }
            ]
        },
        50 + 14, # Total is int(round(4.8 + 9.6)) + 50 = 64
        "List content ignoring non-text blocks"
    ),
    (
        {
            "messages": [
                "invalid_message_type",  # Should be skipped
                {"content": None},  # Empty/None content
                {"content": [
                    "invalid_block_type",  # Should be skipped
                    {"type": "text", "text": None}  # None text, handled as empty string
                ]},
                {"content": "word " * 4}  # 4.8 tokens
            ]
        },
        50 + 5, # Total is int(round(4.8)) + 50 = 5 + 50 = 55
        "Mixed and invalid message formats handled gracefully"
    ),
    (
        {
            "messages": [
                {"role": "user"}  # No content key
            ]
        },
        50,
        "Message with no 'content' key"
    ),
    (
        {
            "messages": [
                {"content": {"key": "value"}}
            ]
        },
        50,
        "Unsupported content type ignored"
    )
])
def test_estimate_prompt_tokens(body, expected_tokens, description):
    """
    Test estimate_prompt_tokens logic.
    Using parametrize to reduce boilerplate while maintaining documentation.
    """
    assert estimate_prompt_tokens(body) == expected_tokens
