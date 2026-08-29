"""Carry a quoted history message into the current turn.

Ported from the mancang test platform (manchang-agent-v0 harness): the quoted
message is prepended to the user message as a 【引用】 block, and the system
prompt gains the 【引用处理】 rules so the model answers the quote first.
"""

from typing import Any, Dict, List, Optional, Union

QUOTE_ROLES = {"user", "assistant"}
QUOTE_ROLE_LABELS = {
    "user": "用户",
    "assistant": "AI",
}
# Long quotes are pure prompt overhead — the model only needs enough of the
# original message to know what is being referred to.
MAX_QUOTE_CHARS = 500

QUOTE_HANDLING_INSTRUCTION = """【引用处理】
当输入中【引用】不为空时（用户引用了某条历史消息进行讨论）：
1. 必须先针对被引用的内容回应（认可、解释、澄清、承接或反驳等）。
2. 再回应本次新消息。
3. 不得忽略或回避被引用内容。
4. 若被引用内容包含提问，优先回答被引用内容里的提问。"""


def normalize_quote(raw: Any) -> Optional[Dict[str, str]]:
    """Normalize a client-supplied quote into {message_id, role, content}.

    Returns None when the payload is missing, malformed, or has empty content,
    so every caller can treat "no quote" and "bad quote" the same way.
    """
    if not isinstance(raw, dict):
        return None

    content = raw.get("content")
    if not isinstance(content, str):
        return None
    content = content.strip()
    if not content:
        return None
    if len(content) > MAX_QUOTE_CHARS:
        content = content[:MAX_QUOTE_CHARS] + "…"

    role = str(raw.get("role") or "").strip().lower()
    if role not in QUOTE_ROLES:
        role = "assistant"

    message_id = raw.get("message_id", raw.get("messageId", ""))
    message_id = str(message_id or "").strip()

    return {"message_id": message_id, "role": role, "content": content}


def quote_role_label(role: Optional[str]) -> str:
    """'用户' for a quoted user message, 'AI' otherwise."""
    return QUOTE_ROLE_LABELS.get(str(role or "").strip().lower(), "AI")


def format_quote_block(quote: Optional[Dict[str, str]]) -> str:
    """Render the 【引用】 prefix line, or '' when there is nothing to quote."""
    if not quote or not quote.get("content"):
        return ""
    return f"【引用】{quote_role_label(quote.get('role'))}: {quote['content']}\n"


def compose_with_quote(
    user_message: Union[str, List[dict], None],
    quote: Optional[Dict[str, str]],
) -> Union[str, List[dict], None]:
    """Prepend the 【引用】 block to the outgoing user message.

    Handles both plain text and multimodal content blocks; for the latter the
    quote goes into the first text block, or a new leading one when the message
    is image-only.
    """
    block = format_quote_block(quote)
    if not block:
        return user_message

    if isinstance(user_message, str):
        return block + user_message

    if isinstance(user_message, list):
        composed = list(user_message)
        for index, part in enumerate(composed):
            if isinstance(part, dict) and part.get("type") == "text":
                merged = dict(part)
                merged["text"] = block + str(part.get("text", ""))
                composed[index] = merged
                return composed
        composed.insert(0, {"type": "text", "text": block.rstrip("\n")})
        return composed

    return block.rstrip("\n") if user_message is None else user_message


def append_quote_instruction(
    system_prompt: Optional[str],
    quote: Optional[Dict[str, str]],
) -> Optional[str]:
    """Append the 【引用处理】 rules, only for turns that actually carry a quote."""
    if not quote or not quote.get("content"):
        return system_prompt
    base = str(system_prompt or "").strip()
    if not base:
        return QUOTE_HANDLING_INSTRUCTION
    return f"{base}\n\n{QUOTE_HANDLING_INSTRUCTION}"
