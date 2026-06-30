import sys
import os
from pathlib import Path

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parent.parent / "config.yaml")

# Add the parent directory to the path so we can import from router
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import estimate_prompt_tokens

def test_estimate_prompt_tokens_empty():
    assert estimate_prompt_tokens({}) == 50

def test_estimate_prompt_tokens_no_messages():
    assert estimate_prompt_tokens({"other_key": "value"}) == 50

def test_estimate_prompt_tokens_empty_messages():
    assert estimate_prompt_tokens({"messages": []}) == 50

def test_estimate_prompt_tokens_string_content():
    body = {
        "messages": [
            {"content": "word " * 4},  # 4 * 1.2 = 4.8
            {"content": "word " * 8}   # 8 * 1.2 = 9.6
        ]
    }
    # Total is int(round(4.8 + 9.6)) + 50 = int(round(14.4)) + 50 = 14 + 50 = 64
    assert estimate_prompt_tokens(body) == 50 + 14

def test_estimate_prompt_tokens_list_content():
    body = {
        "messages": [
            {
                "content": [
                    {"type": "text", "text": "word " * 4},  # 4.8 tokens
                    {"type": "image_url", "url": "ignored"},  # 0 tokens
                    {"type": "text", "text": "word " * 8}  # 9.6 tokens
                ]
            }
        ]
    }
    # Total is int(round(4.8 + 9.6)) + 50 = 64
    assert estimate_prompt_tokens(body) == 50 + 14

def test_estimate_prompt_tokens_mixed_and_invalid_msgs():
    body = {
        "messages": [
            "invalid_message_type",  # Should be skipped
            {"content": None},  # Empty/None content
            {"content": [
                "invalid_block_type",  # Should be skipped
                {"type": "text", "text": None}  # None text, handled as empty string
            ]},
            {"content": "word " * 4}  # 4.8 tokens
        ]
    }
    # Total is int(round(4.8)) + 50 = 5 + 50 = 55
    assert estimate_prompt_tokens(body) == 50 + 5

def test_estimate_prompt_tokens_missing_content():
    body = {
        "messages": [
            {"role": "user"}  # No content key
        ]
    }
    assert estimate_prompt_tokens(body) == 50

def test_estimate_prompt_tokens_invalid_content_type():
    # unsupported content type should be ignored
    body = {
        "messages": [
            {"content": {"key": "value"}}
        ]
    }
    assert estimate_prompt_tokens(body) == 50
