# -*- coding: utf-8 -*-
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

昵称规则（全部满足才输出 nickname，否则一律 null）：
- 只看标记为【最新消息】的那一条；历史消息、已记忆事件里的称呼一律忽略
- 用户必须在最新消息中明确要求改变对自己的称呼（例如"叫我小明""以后叫我XX""我的名字是XX"）
- nickname 必须是最新消息里逐字出现的词，严禁编造或联想任何没出现过的名字
- 喜欢吃/喝/玩某东西不是昵称（"我喜欢吃芒果"≠"叫我芒果"）；谈论食物、水果、动物、人物都不是改称呼
- 只取用户给自己起的称呼；给助手或第三方起的名字不算
- 如果提示了"用户当前昵称"，且最新消息没有要求换成一个不同的称呼，则 nickname 为 null
- 昵称设置本身不要写入 memories（"用户希望被叫X"这类不算生活事件）

bearnickname 规则（全部满足才输出 bearnickname，否则一律 null）：
- 只看标记为【最新消息】的那一条；历史消息、已记忆事件里的称呼一律忽略
- 用户必须在最新消息中明确要求改变对【助手】的称呼（例如"你叫XX""以后叫你XX""给你取名叫XX""你的名字是XX"）
- bearnickname 必须是最新消息里逐字出现的词，严禁编造或联想任何没出现过的名字
- "我喜欢吃草莓"≠"你叫草莓"；谈论食物、水果、动物、人物都不是改助手名字
- 只取用户给【助手】起的名字；给用户自己起的名字归 nickname，给第三方起的名字不算
- 如果提示了"助手当前昵称"，且最新消息没有要求换成一个不同的称呼，则 bearnickname 为 null
- 改名设置本身不要写入 memories

只输出 JSON，不要其他文字：
{
  "nickname": "用户希望被叫的名字，没有则为 null",
  "bearnickname": "用户给助手起的名字，没有则为 null",
  "memories": [
    {
      "time": "YYYY-MM-DD",
      "category": "book | music | movie | drink | food | game | place | activity | preference | other",
      "event": "用户……"
    }
  ]
}"""

RENAME_JUDGE_SYSTEM = """你是一个"改名结果判定器"。

输入：
- 改名请求句：用户发送的完整改名句（例如"以后叫我荔枝吧"或"以后叫你草莓吧"）
- 助手回复：助手对该改名请求的回答
- 用户当前昵称
- 助手当前昵称（满仓）

你的任务：判断这次改名是否成功，以及改的是谁的名字。

规则：
- 请求句包含"叫我/以后叫我/我的名字/称呼我"等（改用户自己的称呼）→ 本次改的是【用户昵称】
- 请求句包含"叫你/以后叫你/给你取名叫/你的名字"等（改对助手的称呼）→ 本次改的是【助手昵称】
- 从请求句中提取新名字填入 new_name；新名字必须逐字出现在请求句里，严禁编造或联想
- 助手回复明确确认接受新名字（如"好呀，以后就叫你荔枝啦"）→ rename_result = "user"（改用户）或 "bear"（改助手）
- 助手回复明确拒绝或否定（如"这个名字不好""我不这样叫你""换个名字吧"）→ rename_result = "denied"
- 无法判定、回复与改名无关、或没有明显的接受/拒绝 → rename_result = null
- 一次只判定一个名字；请求句只包含一个改名意图

只输出 JSON，不要其他文字：
{
  "rename_result": "user | bear | denied | null",
  "new_name": "新名字（必须逐字取自请求句，没有则 null）"
}"""

VALID_CATEGORIES = {"book", "music", "movie", "drink", "food", "game", "place", "activity", "preference", "other"}

NAMING_INTENT_PATTERN = re.compile(
    r"叫我|喊我|称呼我|我的名字|我名字|名字是|我的昵称|昵称是|称我为|叫俺"
    r"|我叫(?!什么|啥|谁|不|你|他|她)|名叫"
)

BEAR_INTENT_PATTERN = re.compile(
    r"你叫|叫你|以后叫你|称呼你|你的名字|你的昵称|给你取名叫|你想叫|名为你|你叫俺"
    r"|你就叫|以后就叫你|以后你是|以后你叫|以后你就"
)

# 确定性守卫：仅匹配「针对名字/称呼」的拒绝短语，避免「心情不好」等日常用语误杀
_REFUSE_PATTERN = re.compile(
    r"不想叫|不愿意叫|不同意叫|名字不好|不好听|换个名字|换一个名字|才不要这个|哪有人叫"
)
# 回复含明确确认语义
_CONFIRM_PATTERN = re.compile(r"好|行|可以|没问题|记住|记得|就叫|是呀|嗯|当然|ok|OK|好的|就这样|定啦|定咯")


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _current_date() -> str:
    return datetime.date.today().isoformat()


def _parse(raw: str) -> tuple[list[dict], Optional[str], Optional[str]]:
    """Returns (memories, nickname, bearnickname). nickname/bearnickname is None if not detected."""
    cleaned = _strip_thinking(raw)
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return [], None, None
    try:
        parsed = json.loads(m.group())
    except json.JSONDecodeError:
        return [], None, None

    # nickname
    nickname = parsed.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        nickname = None
    else:
        nickname = nickname.strip()

    # bearnickname
    bearnickname = parsed.get("bearnickname")
    if not isinstance(bearnickname, str) or not bearnickname.strip():
        bearnickname = None
    else:
        bearnickname = bearnickname.strip()

    # memories
    memories = parsed.get("memories", [])
    if not isinstance(memories, list):
        return [], nickname, bearnickname
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
    return result, nickname, bearnickname


def _validate_nickname(
    nickname: Optional[str],
    latest_message: str,
    current_nickname: Optional[str],
    context_messages: Optional[list] = None,
) -> Optional[str]:
    """Deterministic guard for the extracted nickname — 不依赖模型听话：

    1. nickname 必须逐字出现在最新消息里（防止编造式幻觉）
    2. 与当前昵称不同（防止重复覆盖）
    3. 条件式意图检查：仅当该昵称也出现在窗口的旧消息里（陈旧回声嫌疑，
       如旧消息"以后叫我芒果"+最新消息"我想吃芒果蛋糕"的字面巧合）时，
       才要求最新消息含改称呼意图词；昵称只出现在最新消息时完全信任抽取器的
       语义判断，不误拦"我叫小李""以后我就是XX了"等自然表达。
    """
    if not nickname:
        return None
    nick = nickname.strip().strip("\"'“”‘’")
    if not nick or len(nick) > 24:
        return None
    if nick not in latest_message:
        return None
    if current_nickname and nick == current_nickname:
        return None
    stale_echo = any(nick in m for m in (context_messages or []) if isinstance(m, str))
    if stale_echo and not NAMING_INTENT_PATTERN.search(latest_message):
        return None
    return nick


def _validate_bearnickname(
    bearnickname: Optional[str],
    latest_message: str,
    current_bearnickname: Optional[str],
    context_messages: Optional[list] = None,
) -> Optional[str]:
    """Deterministic guard for the extracted bearnickname（助手昵称）— 不依赖模型听话：
    1. bearnickname 必须逐字出现在最新消息里（防止编造式幻觉）
    2. 与当前助手昵称不同（防止重复覆盖）
    3. 陈旧回声检查同 _validate_nickname。
    """
    if not bearnickname:
        return None
    nick = bearnickname.strip().strip("\"'\u201c\u201d\u2018\u2019")
    if not nick or len(nick) > 24:
        return None
    if nick not in latest_message:
        return None
    if current_bearnickname and nick == current_bearnickname:
        return None
    stale_echo = any(nick in m for m in (context_messages or []) if isinstance(m, str))
    if stale_echo and not BEAR_INTENT_PATTERN.search(latest_message):
        return None
    return nick


def _parse_rename_result(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Parse {'rename_result':..., 'new_name':...}. Returns (result, new_name)."""
    cleaned = _strip_thinking(raw)
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return None, None
    try:
        parsed = json.loads(m.group())
    except json.JSONDecodeError:
        return None, None
    result = parsed.get("rename_result")
    if result not in ("user", "bear", "denied"):
        result = None
    new_name = parsed.get("new_name")
    if not isinstance(new_name, str) or not new_name.strip():
        new_name = None
    else:
        new_name = new_name.strip()
    return result, new_name


def _validate_rename_result(result: Optional[str], model_reply: str) -> Optional[str]:
    """Deterministic guard for the rename verdict — 拒绝语义优先，防 LLM 乱报。

    - 'user'/'bear'：回复含拒绝词时降级为 'denied'（拒绝优先）；否则采信。
    - 'denied'：回复必须含明确拒绝词才采信，否则视为无法判定（null）。
    """
    if not result or not isinstance(model_reply, str) or not model_reply.strip():
        return None
    if result == "denied":
        return "denied"  # 以 LLM 语义为准：模型明确拒绝即采信
    if result in ("user", "bear"):
        if _REFUSE_PATTERN.search(model_reply):
            return "denied"
        return result
    return None


def _validate_rename_name(new_name: Optional[str], rename_request: str) -> Optional[str]:
    """Deterministic guard for the extracted new name:
    逐字出现在请求句 + 字符/长度规则与 PUT 一致（validate_nickname）。
    """
    from common.utils import validate_nickname
    if not new_name or not isinstance(rename_request, str):
        return None
    name = str(new_name).strip().strip("\"'“”‘’")
    if not name:
        return None
    if name not in rename_request:
        return None
    if validate_nickname(name):
        return None
    return name


def judge_rename(
    rename_request: str,
    model_reply: str,
    current_nickname: Optional[str],
    current_bearnickname: Optional[str],
    api_key: str,
    api_base: str,
    model: str,
) -> tuple[Optional[str], Optional[str]]:
    """Judge the outcome of one rename request via the LLM extractor.

    Inputs (all fed): the rename sentence + a copy of the model's reply + the
    current user/bear nicknames. Returns (rename_result, new_name):
    rename_result: 'user' | 'bear' | 'denied' | None.
    """
    if not api_key or not model_reply:
        return None, None
    try:
        import requests
    except ImportError:
        return None, None

    user_prompt = (
        f"改名请求句：{rename_request}\n\n"
        f"助手回复：{model_reply}\n\n"
        f"用户当前昵称：{current_nickname or '（无）'}\n"
        f"助手当前昵称：{current_bearnickname or '（无）'}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": RENAME_JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 40,
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
        llm_result, llm_name = _parse_rename_result(raw)
        result = _validate_rename_result(llm_result, model_reply)
        new_name = _validate_rename_name(llm_name, rename_request)
        if result in ("user", "bear") and not new_name:
            result = "denied"
        if result == "user" and new_name and current_nickname and new_name == current_nickname:
            result = "denied"
        if result == "bear" and new_name and current_bearnickname and new_name == current_bearnickname:
            result = "denied"
        if result is None:
            return None, None
        return result, new_name
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[ThingMemory] judge_rename error: {e}")
        return None, None


def extract_memories(
    user_messages: list[str],
    api_key: str,
    api_base: str,
    model: str,
    existing_events: Optional[list[str]] = None,
    current_nickname: Optional[str] = None,
    current_bearnickname: Optional[str] = None,
) -> tuple[list[dict], Optional[str], Optional[str]]:
    """Call LLM to extract memory events and optional nicknames.
    Returns (memories, nickname, bearnickname). Each is None if not mentioned.

    existing_events: 用户此前已记录的事件描述列表，注入提示词让抽取器避免重复抽取。
    current_nickname: 用户当前昵称；昵称只会从最新一条消息中抽取，且必须与当前昵称不同。
    current_bearnickname: 助手当前昵称；同样逐字+去重校验。
    """
    if not api_key or not user_messages:
        return [], None, None

    try:
        import requests
    except ImportError:
        return [], None

    today = _current_date()
    latest_message = user_messages[-1]
    context_msgs = user_messages[:-1]
    msgs_lines = [f"用户消息 {i+1}：{m}" for i, m in enumerate(context_msgs)]
    msgs_lines.append(f"【最新消息】用户消息 {len(user_messages)}：{latest_message}")
    msgs_text = "\n\n".join(msgs_lines)

    existing_block = ""
    if existing_events:
        existing_lines = "\n".join(f"- {e}" for e in existing_events if isinstance(e, str) and e.strip())
        if existing_lines:
            existing_block = f"\n\n已记忆事件（请勿重复抽取）：\n{existing_lines}"

    nickname_block = f"\n\n用户当前昵称：{current_nickname}" if current_nickname else ""
    bear_block = f"\n\n助手当前昵称：{current_bearnickname}" if current_bearnickname else ""

    user_prompt = f"当前日期：{today}{nickname_block}{bear_block}{existing_block}\n\n本轮待抽取的新消息：\n{msgs_text}"

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
        memories, nickname, bearnickname = _parse(raw)
        nickname = _validate_nickname(nickname, latest_message, current_nickname, context_msgs)
        bearnickname = _validate_bearnickname(bearnickname, latest_message, current_bearnickname, context_msgs)
        return memories, nickname, bearnickname
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[ThingMemory] extractor error: {e}")
        return [], None, None
