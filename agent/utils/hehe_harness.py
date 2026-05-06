from __future__ import annotations

import re
from typing import Any, Dict, List, MutableSequence, Optional, Sequence, Tuple

HEHE = "嘿嘿"

# Punctuation commonly adjacent to short Chinese particles/emotes.
_ADJACENT_PUNCTUATION = (
    "，。！？、；：,.!?;:~～…—-·"
    "（）()[]【】{}《》<>“”\"‘’'「」『』"
)

_HEHE_WITH_ADJACENT_PUNCT_RE = re.compile(
    rf"[ \t]*[{re.escape(_ADJACENT_PUNCTUATION)}]*[ \t]*"
    rf"{re.escape(HEHE)}"
    rf"[ \t]*[{re.escape(_ADJACENT_PUNCTUATION)}]*[ \t]*"
)


def contains_hehe(text: Optional[str]) -> bool:
    return bool(text and HEHE in text)


def strip_hehe_with_adjacent_punctuation(text: str) -> str:
    """Remove every '嘿嘿' plus punctuation directly around it."""
    if not text:
        return text

    cleaned = _HEHE_WITH_ADJACENT_PUNCT_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    return cleaned.strip()


def apply_consecutive_hehe_harness(
    response: str,
    previous_response: Optional[str],
) -> Tuple[str, bool]:
    """Suppress '嘿嘿' only when the previous assistant turn also used it."""
    if not contains_hehe(previous_response) or not contains_hehe(response):
        return response, False

    cleaned = strip_hehe_with_adjacent_punctuation(response)
    return cleaned, cleaned != response


def assistant_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def last_assistant_text(messages: Sequence[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        text = assistant_text(message)
        if text:
            return text
    return ""


def replace_last_assistant_text(
    messages: MutableSequence[Dict[str, Any]],
    new_text: str,
) -> bool:
    """Replace the last assistant text block so persisted history matches output."""
    for message_index in range(len(messages) - 1, -1, -1):
        message = messages[message_index]
        if message.get("role") != "assistant":
            continue

        content = message.get("content")
        if isinstance(content, str):
            message["content"] = new_text
            return True

        if not isinstance(content, list):
            continue

        for block_index in range(len(content) - 1, -1, -1):
            block = content[block_index]
            if not isinstance(block, dict) or block.get("type") != "text":
                continue

            if new_text:
                block["text"] = new_text
            else:
                content.pop(block_index)
                if not content:
                    messages.pop(message_index)
            return True

    return False
