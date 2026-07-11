"""Shared helpers for defensive chat completion response parsing.

Used by the canonical endpoint verification script and the classifier scripts
to safely extract content and reasoning_content from OpenAI-compatible API responses.
"""

from typing import Any


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
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()
    return content, reasoning
