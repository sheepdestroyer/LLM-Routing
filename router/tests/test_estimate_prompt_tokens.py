import pytest
import sys
import os
from pathlib import Path

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parent.parent / "config.yaml")

# Add the parent directory to the path so we can import from router
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import estimate_prompt_tokens

def test_estimate_prompt_tokens_empty_body():
    assert estimate_prompt_tokens({}) == 50

def test_estimate_prompt_tokens_no_messages():
    assert estimate_prompt_tokens({"other_key": "value"}) == 50

def test_estimate_prompt_tokens_empty_messages():
    assert estimate_prompt_tokens({"messages": []}) == 50

def test_estimate_prompt_tokens_string_content():
    # 20 chars // 4 = 5 tokens + 50 overhead = 55
    body = {
        "messages": [
            {"content": "12345678901234567890"}
        ]
    }
    assert estimate_prompt_tokens(body) == 55

def test_estimate_prompt_tokens_list_content():
    # block 1: 12 chars // 4 = 3 tokens
    # block 2: not text, ignored
    # block 3: 8 chars // 4 = 2 tokens
    # total = 5 + 50 = 55
    body = {
        "messages": [
            {
                "content": [
                    {"type": "text", "text": "123456789012"},
                    {"type": "image_url", "image_url": {"url": "http://example.com"}},
                    {"type": "text", "text": "12345678"}
                ]
            }
        ]
    }
    assert estimate_prompt_tokens(body) == 55

def test_estimate_prompt_tokens_invalid_message_type():
    # invalid message should be ignored
    body = {
        "messages": [
            "this is not a dictionary",
            {"content": "1234"} # 4 chars // 4 = 1 token
        ]
    }
    assert estimate_prompt_tokens(body) == 51

def test_estimate_prompt_tokens_invalid_content_type():
    # unsupported content type should be ignored
    body = {
        "messages": [
            {"content": {"key": "value"}}
        ]
    }
    assert estimate_prompt_tokens(body) == 50

def test_estimate_prompt_tokens_multiple_messages():
    # msg 1: 8 chars // 4 = 2 tokens
    # msg 2: 12 chars // 4 = 3 tokens
    # total = 5 + 50 = 55
    body = {
        "messages": [
            {"content": "12345678"},
            {"content": "123456789012"}
        ]
    }
    assert estimate_prompt_tokens(body) == 55

def test_estimate_prompt_tokens_none_content():
    # None content should be treated as empty string
    body = {
        "messages": [
            {"content": None}
        ]
    }
    assert estimate_prompt_tokens(body) == 50
