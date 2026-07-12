"""Shared helpers for defensive chat completion response parsing.

Used by the canonical endpoint verification script and the classifier scripts
to safely extract content and reasoning_content from OpenAI-compatible API responses.
"""

from typing import Any


def _normalize_chat_content(value: Any) -> str:
    """Normalize structured content payloads into a plain string.

    Handles plain strings, lists of strings, lists of dicts with
    ``text`` / ``content`` keys, and dicts with ``text`` / ``content`` keys.
    Returns an empty string for None or unrecognised shapes.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif "content" in item:
                    nested = _normalize_chat_content(item.get("content"))
                    if nested:
                        parts.append(nested)
        return "".join(parts).strip()
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text.strip()
        if "content" in value:
            return _normalize_chat_content(value.get("content"))
    return ""


def parse_chat_response(data: Any) -> tuple[str, str]:
    """Safely extract content and reasoning_content from a chat completion response.

    Args:
        data: Parsed JSON response from a chat completion endpoint (expected to be a dict).

    Returns:
        (content, reasoning_content) — both may be empty strings.
    """
    if not isinstance(data, dict):
        return "", ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return "", ""
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return "", ""
    content = _normalize_chat_content(message.get("content"))
    reasoning = _normalize_chat_content(message.get("reasoning_content"))
    return content, reasoning
