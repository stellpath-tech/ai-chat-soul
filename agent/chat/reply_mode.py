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
REPLY_MODE_LABELS = {
    "voice": "语音",
    "text": "文字",
}

_SYSTEM_PROMPT = """你只负责判断是否要切换助手下一条回复的呈现方式。

输入包含当前回复模式 parent_reply_mode 和用户最新消息 message。
只能输出以下 JSON 之一：
{"reply_mode":"voice"}
{"reply_mode":"text"}
{"reply_mode":null}

规则按优先级执行：
1. 用户明确要求用语音、声音、音频回复，输出 voice。用户明确要求用文字、打字回复或不要语音，输出 text。
2. 用户没有明确说“语音”时，只在当前模式为 text 且听到回复本身明显是体验的一部分时，主动输出 voice。例如：
   - 询问助手能不能说话、想听助手说话或声音；
   - 要求朗读、唱歌、哄睡、讲睡前故事；
   - 明确需要安慰、哄一哄、温柔陪伴，并且语音明显比文字更合适。
3. 普通聊天、事实问答、天气、闲聊、写作、讲普通笑话等，不主动切换，输出 null。
4. 当前模式为 voice 时，除非用户明确要求文字，否则不要主动切回 text，输出 null。
5. 仅仅提到语音或声音、描述自己发过语音、讨论声音特点，不代表要求切换。
6. 如果不确定，输出 null。

不要回答用户的问题，不要解释。
"""

VOICE_REPLY_CAPABILITY_INSTRUCTION = (
    "客户端会将本轮回复合成为可播放的语音；"
    "不得声称自己不能发语音、没有声带或只能使用文字。"
)


def normalize_parent_reply_mode(parent_reply_mode: Optional[str]) -> str:
    """Normalize the client-reported current mode, defaulting to text."""
    value = str(parent_reply_mode or "").strip().lower()
    return value if value in REPLY_MODES else "text"


def reply_mode_system_instruction(
    reply_mode: Optional[str],
    parent_reply_mode: Optional[str],
) -> str:
    """Build one of the four current-mode state sentences."""
    parent_mode = normalize_parent_reply_mode(parent_reply_mode)
    next_mode = reply_mode if reply_mode in REPLY_MODES else parent_mode
    changed = reply_mode in REPLY_MODES and next_mode != parent_mode
    action = "已经切换为" if changed else "保持为"
    return f"当前的回复模式{action}{REPLY_MODE_LABELS[next_mode]}。"


def append_reply_mode_instruction(
    system_prompt: Optional[str],
    reply_mode: Optional[str],
    parent_reply_mode: Optional[str],
) -> str:
    """Append voice capability and the resolved state to the system prompt."""
    instruction = reply_mode_system_instruction(reply_mode, parent_reply_mode)
    parent_mode = normalize_parent_reply_mode(parent_reply_mode)
    next_mode = reply_mode if reply_mode in REPLY_MODES else parent_mode
    base = str(system_prompt or "").strip()
    blocks = [base]
    if next_mode == "voice":
        blocks.append(VOICE_REPLY_CAPABILITY_INSTRUCTION)
    blocks.append(instruction)
    return "\n\n".join(block for block in blocks if block)


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
    parent_reply_mode: Optional[str] = None,
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

    normalized_parent_mode = normalize_parent_reply_mode(parent_reply_mode)
    classifier_input = json.dumps(
        {
            "parent_reply_mode": normalized_parent_mode,
            "message": user_message.strip(),
        },
        ensure_ascii=False,
    )
    endpoint = f"{str(api_base).rstrip('/')}/chat/completions"
    payload = {
        "model": REPLY_MODE_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": classifier_input},
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
            f"parent_mode={normalized_parent_mode!r} mode={reply_mode!r} "
            f"model={REPLY_MODE_MODEL} latency_ms={latency_ms}"
        )
        event_log.log(
            "reply_mode_classified",
            request_id=request_id,
            parent_reply_mode=normalized_parent_mode,
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
            parent_reply_mode=normalized_parent_mode,
            reply_mode=None,
            model=REPLY_MODE_MODEL,
            latency_ms=latency_ms,
            success=False,
            error_type=type(exc).__name__,
        )
        return None
