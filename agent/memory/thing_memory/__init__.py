"""ThingMemory harness — 用户生活事件记忆系统.

集成点:
  1. 每轮对话前: get_memory_block(workspace_root, user_id, session_id)
     → 返回注入字符串，追加进 system prompt
  2. 每轮对话后: fire_extract(workspace_root, user_id, session_id, user_messages, cfg)
     → 后台线程，每隔 EXTRACT_INTERVAL 轮提取一次新记忆
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .store import (
    get_recent_memories, add_memory, is_duplicate,
    archive_messages, is_nickname_intent_event,
)
from .extractor import extract_memories

logger = logging.getLogger(__name__)

BLOCK_HEADER = (
    "[用户过往记忆]\n"
    "以下是用户此前聊天中明确提到过的具体事件或偏好，可在合适时自然参考。"
    "不要生硬复述，不要过度联想。"
)
MAX_INJECTED = 15
MAX_BLOCK_CHARS = 800
EXTRACT_INTERVAL = 1  # 每轮都触发提取，靠 is_duplicate 去重


def get_memory_block(workspace_root: str, user_id: str, session_id: str = "") -> str:
    """Build memory injection block. Returns '' if no memories."""
    try:
        memories = get_recent_memories(workspace_root, user_id, session_id, limit=MAX_INJECTED)
        if not memories:
            return ""
        lines = [f"- {m['time']}：{m['event']}" for m in memories]
        block = f"{BLOCK_HEADER}\n\n" + "\n".join(lines)
        while lines and len(block) > MAX_BLOCK_CHARS:
            lines.pop()
            block = f"{BLOCK_HEADER}\n\n" + "\n".join(lines)
        return block
    except Exception as e:
        logger.warning(f"[ThingMemory] get_memory_block error: {e}")
        return ""


def fire_extract(
    workspace_root: str,
    user_id: str,
    session_id: str,
    user_messages: list[str],
    api_key: str,
    api_base: str,
    model: str,
    assistant_reply: str = "",
) -> None:
    """Launch background extraction (fire-and-forget)."""
    threading.Thread(
        target=_extract_worker,
        args=(workspace_root, user_id, session_id, user_messages, api_key, api_base, model, assistant_reply),
        daemon=True,
    ).start()


def _extract_worker(
    workspace_root: str,
    user_id: str,
    session_id: str,
    user_messages: list[str],
    api_key: str,
    api_base: str,
    model: str,
    assistant_reply: str = "",
) -> None:
    try:
        msgs_to_extract = [m for m in user_messages if isinstance(m, str) and m.strip()]
        if not msgs_to_extract:
            return

        # 注入已有记忆，让抽取器在生成阶段就避开重复
        existing = get_recent_memories(workspace_root, user_id, session_id, limit=40)
        existing_events = [m["event"] for m in existing]

        # 当前昵称：昵称只从【最新消息】里抽取，且必须与当前昵称不同（防陈旧窗口覆盖）
        current_nick = None
        try:
            from agent.memory.user_cache import get as _cache_get
            _cached = _cache_get(workspace_root, user_id)
            current_nick = _cached.get("nickname") if _cached else None
        except Exception:
            pass

        # 助手当前昵称（bear_nickname）：从 soul.db.user 表读取
        current_bear = None
        try:
            if user_id.startswith("user_"):
                import json as _json
                from agent.memory.thing_memory.store import _conn, db_path
                _uid = int(user_id[5:])
                _db = db_path(workspace_root)
                with _conn(_db) as conn:
                    _brow = conn.execute("SELECT bear_nickname FROM user WHERE id=?", (_uid,)).fetchone()
                    if _brow and _brow["bear_nickname"]:
                        current_bear = _brow["bear_nickname"]
        except Exception:
            pass

        extracted, nickname, bearnickname = extract_memories(
            msgs_to_extract, api_key, api_base, model,
            existing_events=existing_events, current_nickname=current_nick,
            current_bearnickname=current_bear,
        )
        logger.info(f"[ThingMemory] user={user_id} extracted={len(extracted)} nickname={nickname!r} bearnickname={bearnickname!r}")

        # 写入新记忆（昵称设置类描述不入事件记忆，昵称走独立通道）
        saved = 0
        for mem in extracted[:5]:
            if is_nickname_intent_event(mem["event"]):
                logger.info(f"[ThingMemory] skip nickname-like event: {mem['event']}")
                continue
            if not is_duplicate(workspace_root, user_id, mem["event"]):
                src = " | ".join(msgs_to_extract)[:200]
                add_memory(workspace_root, user_id, session_id, mem, source_text=src)
                saved += 1
        logger.info(f"[ThingMemory] user={user_id} saved={saved}")

        # 改名落库前走模型判断：不明确拒绝才落库（以 LLM 语义为准）
        if (nickname or bearnickname) and assistant_reply:
            try:
                from agent.memory.thing_memory.extractor import judge_rename
                _judge_request = msgs_to_extract[-1] if msgs_to_extract else ""
                _judge_result, _ = judge_rename(
                    _judge_request, assistant_reply, current_nick, current_bear,
                    api_key, api_base, model,
                )
                if _judge_result == "denied":
                    logger.info(f"[ThingMemory] rename denied by model judge: user={user_id} nickname={nickname!r} bearnickname={bearnickname!r}")
                    return
            except Exception as _je:
                logger.warning(f"[ThingMemory] rename judge failed, will apply: {_je}")

        # 写入昵称（文件缓存 + soul.db.user 表，旧昵称归档到 used_nickname）
        if nickname:
            try:
                import json as _json
                from agent.memory.user_cache import update_nickname
                from agent.memory.thing_memory.store import _conn, db_path

                update_nickname(workspace_root, user_id, nickname)

                if user_id.startswith("user_"):
                    uid_int = int(user_id[5:])
                    db = db_path(workspace_root)
                    with _conn(db) as conn:
                        row = conn.execute(
                            "SELECT nickname, used_nickname FROM user WHERE id=?", (uid_int,)
                        ).fetchone()
                        if row:
                            old_nick = row["nickname"]
                            used = _json.loads(row["used_nickname"] or "[]")
                            # 旧昵称非空且与新昵称不同时才归档
                            if old_nick and old_nick != nickname and old_nick not in used:
                                used.append(old_nick)
                            # 昵称变更时，把引用旧昵称的记忆改写为曾用昵称格式（保持 active，自消歧）
                            if old_nick and old_nick != nickname:
                                try:
                                    from agent.memory.thing_memory.store import tag_former_nickname_memories
                                    rewritten = tag_former_nickname_memories(
                                        workspace_root, user_id, [old_nick], new_nickname=nickname
                                    )
                                    if rewritten:
                                        logger.info(f"[ThingMemory] rewrote {rewritten} former-nickname memories: {old_nick!r}→{nickname!r}")
                                except Exception as _tag_e:
                                    logger.warning(f"[ThingMemory] tag former-nickname memories failed: {_tag_e}")
                            conn.execute(
                                "UPDATE user SET nickname=?, used_nickname=?, updated_at=datetime('now') WHERE id=?",
                                (nickname, _json.dumps(used, ensure_ascii=False), uid_int),
                            )
                            conn.commit()

                logger.info(f"[ThingMemory] nickname updated: user={user_id} nickname={nickname!r}")
            except Exception as _ne:
                logger.warning(f"[ThingMemory] nickname update failed: {_ne}")

        # 写入助手昵称（bear_nickname，旧名归档到 used_bear_nickname）
        if bearnickname:
            try:
                import json as _json
                from agent.memory.thing_memory.store import _conn, db_path

                old_bear = None
                if user_id.startswith("user_"):
                    uid_int = int(user_id[5:])
                    db = db_path(workspace_root)
                    with _conn(db) as conn:
                        brow = conn.execute(
                            "SELECT bear_nickname, used_bear_nickname FROM user WHERE id=?", (uid_int,)
                        ).fetchone()
                        if brow:
                            old_bear = brow["bear_nickname"]
                            used_bear = _json.loads(brow["used_bear_nickname"] or "[]")
                            if old_bear and old_bear != bearnickname and old_bear not in used_bear:
                                used_bear.append(old_bear)
                            conn.execute(
                                "UPDATE user SET bear_nickname=?, used_bear_nickname=?, updated_at=datetime('now') WHERE id=?",
                                (bearnickname, _json.dumps(used_bear, ensure_ascii=False), uid_int),
                            )
                            conn.commit()
                logger.info(f"[ThingMemory] bearnickname updated: user={user_id} {old_bear!r}→{bearnickname!r}")
            except Exception as _be:
                logger.warning(f"[ThingMemory] bearnickname update failed: {_be}")
    except Exception as e:
        logger.warning(f"[ThingMemory] extraction worker error: {e}", exc_info=True)
