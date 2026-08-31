from scripts.chat_helpers import _normalize_chat_content

def test_normalize_chat_content_none():
    assert _normalize_chat_content(None) == ""

def test_normalize_chat_content_str():
    assert _normalize_chat_content(" hello ") == "hello"
    assert _normalize_chat_content("world") == "world"

def test_normalize_chat_content_list():
    assert _normalize_chat_content([" hello ", " world "]) == "helloworld"

def test_normalize_chat_content_list_dicts():
    payload = [
        {"text": " hello "},
        {"content": " world "}
    ]
    assert _normalize_chat_content(payload) == "helloworld"

def test_normalize_chat_content_dict_text():
    assert _normalize_chat_content({"text": " text "}) == "text"

def test_normalize_chat_content_nested():
    payload = {
        "content": [
            {"text": " nested "}
        ]
    }
    assert _normalize_chat_content(payload) == "nested"

def test_normalize_chat_content_unrecognized():
    assert _normalize_chat_content(123) == ""
    assert _normalize_chat_content([123, 456]) == ""
    assert _normalize_chat_content({"unknown": "key"}) == ""
