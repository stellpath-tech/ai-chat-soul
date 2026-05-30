"""LLM-based extraction of thing-memory events from user messages."""
from __future__ import annotations

import json
import re
import datetime
from typing import Optional


EXTRACT_SYSTEM = """你是一个"用户事件记忆抽取器"。

你的任务是从用户消息中抽取适合长期保存的具体生活事件或普通偏好。

只抽取用户明确表达过的事实或偏好，例如：
- 用户看过/正在看什么书（必须含书名）、电影/电视剧/动漫（必须含片名）
- 用户听过/喜欢什么音乐（必须含歌名或艺人名）
- 用户喜欢/不喜欢什么饮料、食物、游戏（必须含具体名称）
- 用户去过/计划去哪里（必须含地名）
- 用户做过什么具体活动
- 用户提到的普通生活偏好

不要抽取：
- 你推测出来的信息
- 只是当前情绪，例如"我好烦""我好累"
- 没有长期复用价值的闲聊，例如"哈哈哈""嗯嗯""继续"
- 对助手的临时要求
- 系统指令
- 医疗健康、心理疾病、政治立场、宗教信仰
- 身份证、手机号、详细住址
- 性生活、犯罪记录、精确财务信息
- 任何明显高敏隐私

原则：
1. 只基于用户原话
2. 宁可漏记，不要误记
3. 不推断、不总结人格
4. 不抽取 assistant 内容
5. 每条记忆是原子事件
6. event 用第三人称，以"用户"开头
7. 涉及书/影视/音乐/食物/游戏/地点的事件，必须在 event 中包含具体名称；若用户没说具体名称，则不记录该条
8. time 用绝对日期（YYYY-MM-DD），相对时间（昨天/上周等）结合当前日期转换
9. 无法准确转换则 time 用当前日期，event 保留相对表达
10. 没有值得记忆的事件，memories 输出空数组
11. 【去重】下方"已记忆事件"列出用户此前已记录的事件。不要再次抽取与其相同或语义重复的事件（即使本轮用户重复提起或换了说法）；只抽取其中尚未出现的新事件

昵称规则：
- 如果用户明确表达希望被叫某个名字（例如"叫我小明""我的名字是XX""你可以叫我XX"），提取该名字填入 nickname
- 只取用户自己给自己起的称呼，不要提取对第三方的称呼
- 没有昵称信息则 nickname 为 null

只输出 JSON，不要其他文字：
{
  "nickname": "用户希望被叫的名字，没有则为 null",
  "memories": [
    {
      "time": "YYYY-MM-DD",
      "category": "book | music | movie | drink | food | game | place | activity | preference | other",
      "event": "用户……"
    }
  ]
}"""

VALID_CATEGORIES = {"book", "music", "movie", "drink", "food", "game", "place", "activity", "preference", "other"}


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _current_date() -> str:
    return datetime.date.today().isoformat()


def _parse(raw: str) -> tuple[list[dict], Optional[str]]:
    """Returns (memories, nickname). nickname is None if not detected."""
    cleaned = _strip_thinking(raw)
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return [], None
    try:
        parsed = json.loads(m.group())
    except json.JSONDecodeError:
        return [], None

    # nickname
    nickname = parsed.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        nickname = None
    else:
        nickname = nickname.strip()

    # memories
    memories = parsed.get("memories", [])
    if not isinstance(memories, list):
        return [], nickname
    today = _current_date()
    result = []
    for item in memories:
        event = item.get("event", "")
        if not isinstance(event, str) or len(event) < 4:
            continue
        t = item.get("time", today)
        if not isinstance(t, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", t):
            t = today
        cat = item.get("category", "other")
        if cat not in VALID_CATEGORIES:
            cat = "other"
        result.append({"time": t, "category": cat, "event": event.strip()})
    return result, nickname


def extract_memories(
    user_messages: list[str],
    api_key: str,
    api_base: str,
    model: str,
    existing_events: Optional[list[str]] = None,
) -> tuple[list[dict], Optional[str]]:
    """Call LLM to extract memory events and optional nickname.
    Returns (memories, nickname). nickname is None if not mentioned.

    existing_events: 用户此前已记录的事件描述列表，注入提示词让抽取器避免重复抽取。
    """
    if not api_key or not user_messages:
        return [], None

    try:
        import requests
    except ImportError:
        return [], None

    today = _current_date()
    msgs_text = "\n\n".join(f"用户消息 {i+1}：{m}" for i, m in enumerate(user_messages))

    existing_block = ""
    if existing_events:
        existing_lines = "\n".join(f"- {e}" for e in existing_events if isinstance(e, str) and e.strip())
        if existing_lines:
            existing_block = f"\n\n已记忆事件（请勿重复抽取）：\n{existing_lines}"

    user_prompt = f"当前日期：{today}{existing_block}\n\n本轮待抽取的新消息：\n{msgs_text}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 600,
        "temperature": 0,
        "enable_thinking": False,
    }

    try:
        resp = requests.post(
            f"{api_base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"] or "{}"
        return _parse(raw)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[ThingMemory] extractor error: {e}")
        return [], None
