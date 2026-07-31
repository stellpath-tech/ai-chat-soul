"""Classify whether the latest user message requests voice or text output."""

import json
import re
import time
from typing import Callable, Optional

import requests

from common.log import logger
from config import conf

try:
    from common import event_log
except Exception:
    class _NoopEventLog:
        @staticmethod
        def log(*args, **kwargs):
            pass

    event_log = _NoopEventLog()


REPLY_MODE_MODEL = "qwen3.5-flash"
REPLY_MODES = {"voice", "text"}
CLASSIFIER_TIMEOUT = (3, 10)
REPLY_MODE_SYSTEM_INSTRUCTIONS = {
    "voice": "当前回复模式已经切换为语音模式。",
    "text": "当前回复模式已经切换为文字模式。",
}

_SYSTEM_PROMPT = """你是聊天回复形式的意图分类器。只判断用户最新一条消息是否明确要求助手改变回复形式。

输出规则：
- 明确要求助手/满仓用语音、声音、音频回复或发一段语音：voice
- 明确要求助手不要用语音、关闭语音、改用文字或打字回复：text
- 没有明确指定回复形式：null

注意：
- 仅仅提到“语音”、描述自己发过语音、讨论声音好不好听，不代表要求助手用语音回复。
- 疑问、引用、假设和转述中没有真实指令时输出 null。
- 只判断当前消息，不推断历史状态。

只能输出以下 JSON 之一，不能解释：
{"reply_mode":"voice"}
{"reply_mode":"text"}
{"reply_mode":null}
"""


def append_reply_mode_instruction(
    system_prompt: Optional[str],
    reply_mode: Optional[str],
) -> Optional[str]:
    """Append the per-turn reply mode state as the final system sentence."""
    instruction = REPLY_MODE_SYSTEM_INSTRUCTIONS.get(reply_mode)
    if not instruction:
        return system_prompt
    base = str(system_prompt or "").strip()
    return f"{base}\n\n{instruction}" if base else instruction


def parse_reply_mode(raw: str) -> Optional[str]:
    """Parse the classifier output into voice, text, or None."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except (TypeError, ValueError):
        value = cleaned.strip().strip('"').lower()
        return value if value in REPLY_MODES else None

    value = data.get("reply_mode") if isinstance(data, dict) else data
    if isinstance(value, str):
        value = value.strip().lower()
        if value in REPLY_MODES:
            return value
    return None


def classify_reply_mode(
    user_message: str,
    *,
    request_id: str = "",
    http_post: Optional[Callable] = None,
) -> Optional[str]:
    """Classify one user message with one qwen3.5-flash request.

    Classification failures are fail-open: normal chat continues with None.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return None

    api_key = conf().get("open_ai_api_key", "")
    api_base = conf().get(
        "open_ai_api_base",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    if not api_key or not api_base:
        logger.warning("[ReplyMode] classifier skipped: API key/base is missing")
        return None

    endpoint = f"{str(api_base).rstrip('/')}/chat/completions"
    payload = {
        "model": REPLY_MODE_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message.strip()},
        ],
        "temperature": 0,
        "max_tokens": 20,
        "enable_thinking": False,
    }
    post = http_post or requests.post
    started = time.monotonic()

    try:
        response = post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=CLASSIFIER_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"].get("content", "")
        reply_mode = parse_reply_mode(content)
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            f"[ReplyMode] request={request_id or '-'} "
            f"mode={reply_mode!r} model={REPLY_MODE_MODEL} latency_ms={latency_ms}"
        )
        event_log.log(
            "reply_mode_classified",
            request_id=request_id,
            reply_mode=reply_mode,
            model=REPLY_MODE_MODEL,
            latency_ms=latency_ms,
            success=True,
        )
        return reply_mode
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            f"[ReplyMode] classifier failed request={request_id or '-'} "
            f"latency_ms={latency_ms}: {exc}"
        )
        event_log.log(
            "reply_mode_classified",
            request_id=request_id,
            reply_mode=None,
            model=REPLY_MODE_MODEL,
            latency_ms=latency_ms,
            success=False,
            error_type=type(exc).__name__,
        )
        return None
