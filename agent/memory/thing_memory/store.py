"""SQLite-based storage for ThingMemory events."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from typing import Optional


def db_path(workspace_root: str) -> str:
    # EC2 生产环境：soul.db 在 data/ 子目录
    data_path = os.path.join(workspace_root, "data", "soul.db")
    if os.path.exists(data_path):
        return data_path
    return os.path.join(workspace_root, "soul.db")


def _conn(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    stmts = [
        """CREATE TABLE IF NOT EXISTS thing_memory (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            time TEXT NOT NULL,
            category TEXT NOT NULL,
            event TEXT NOT NULL,
            source_text TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tm_user ON thing_memory(user_id, status)",
        """CREATE TABLE IF NOT EXISTS conversation_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            archived_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ca_user ON conversation_archive(user_id, archived_at)",
        """CREATE TABLE IF NOT EXISTS current_setting (
            user_id      TEXT PRIMARY KEY,
            conversation TEXT,
            nickname     TEXT,
            memory       TEXT,
            updated_at   TEXT
        )""",
    ]
    for stmt in stmts:
        conn.execute(stmt)
    conn.commit()


# ── In-memory cache ───────────────────────────────────────────────────────────
# key: (workspace_root, user_id)
# value: list[dict]  — full active memory rows for this user
_mem_cache: dict[tuple[str, str], list[dict]] = {}


def _cache_key(workspace_root: str, user_id: str) -> tuple[str, str]:
    return (workspace_root, user_id)


def _invalidate(workspace_root: str, user_id: str) -> None:
    _mem_cache.pop(_cache_key(workspace_root, user_id), None)


def _load_all_memories(workspace_root: str, user_id: str) -> list[dict]:
    """Read all active memories from DB into cache."""
    path = db_path(workspace_root)
    if not os.path.exists(path):
        return []
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT id, time, category, event, created_at FROM thing_memory "
            "WHERE user_id=? AND status='active' ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_cached(workspace_root: str, user_id: str) -> list[dict]:
    key = _cache_key(workspace_root, user_id)
    if key not in _mem_cache:
        _mem_cache[key] = _load_all_memories(workspace_root, user_id)
    return _mem_cache[key]


# ── Public API ────────────────────────────────────────────────────────────────

def get_recent_memories(
    workspace_root: str,
    user_id: str,
    session_id: str = "",
    limit: int = 15,
) -> list[dict]:
    all_mems = _get_cached(workspace_root, user_id)
    return all_mems[-limit:]


def add_memory(
    workspace_root: str,
    user_id: str,
    session_id: str,
    mem: dict,
    source_text: Optional[str] = None,
) -> None:
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    path = db_path(workspace_root)
    mid = str(uuid.uuid4())
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO thing_memory "
            "(id, user_id, session_id, time, category, event, source_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, user_id, session_id, mem["time"], mem["category"], mem["event"], source_text, now),
        )
        conn.commit()
    # append to cache instead of invalidating, to avoid a round-trip
    key = _cache_key(workspace_root, user_id)
    if key in _mem_cache:
        _mem_cache[key].append({
            "id": mid, "time": mem["time"], "category": mem["category"],
            "event": mem["event"], "created_at": now,
        })


def is_duplicate(workspace_root: str, user_id: str, event: str) -> bool:
    normalized = _normalize(event)
    for row in _get_cached(workspace_root, user_id):
        if _normalize(row["event"]) == normalized:
            return True
    # 已打标的昵称偏好事件也应参与去重，防止同一事件被重新抽取入库
    path = db_path(workspace_root)
    if os.path.exists(path):
        with _conn(path) as conn:
            rows = conn.execute(
                "SELECT event FROM thing_memory WHERE user_id=? AND status='superseded'",
                (user_id,),
            ).fetchall()
        for row in rows:
            if _normalize(row["event"]) == normalized:
                return True
    return False


_NICK_INTENT = ("被叫", "希望叫", "想叫", "称呼", "昵称", "叫我", "改叫", "不要叫")

FORMER_NICKNAME_PREFIX = "用户曾用昵称"


def is_nickname_intent_event(event: str) -> bool:
    """事件是否是"用户对自己称呼的设置/更换"类描述（昵称走独立通道，不入事件记忆）。"""
    if event.startswith(FORMER_NICKNAME_PREFIX):
        return False
    return any(k in event for k in _NICK_INTENT)


def _is_nickname_pref_event(event: str, former_names: list) -> bool:
    """双条件判断：事件含昵称意图词 且 含某个曾用名，才视为过期昵称偏好事件。

    避免误伤主题词记忆（如"用户提到仿真青蛙树脂摆件"含"青蛙"但无昵称意图词）。
    已是曾用昵称格式的记录不再匹配（防止换名链上被反复改写）。
    """
    if event.startswith(FORMER_NICKNAME_PREFIX):
        return False
    if not any(k in event for k in _NICK_INTENT):
        return False
    return any(n in event for n in former_names)


def tag_former_nickname_memories(
    workspace_root: str,
    user_id: str,
    former_names: list,
    new_nickname: Optional[str] = None,
) -> int:
    """昵称更换后处理引用旧昵称的记忆：
    - 提供 new_nickname 时，改写为自消歧的曾用昵称格式并保持 active
      （如 用户曾用昵称"芒果"（2026-08-06 起改用"雪糕"）），历史可追问、单条不冲突；
    - 未提供时退化为原行为（标记 superseded 退出注入）。
    返回处理条数。"""
    if not former_names:
        return 0
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    path = db_path(workspace_root)
    count = 0
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT id, event FROM thing_memory WHERE user_id=? AND status='active'",
            (user_id,),
        ).fetchall()
        for r in rows:
            if not _is_nickname_pref_event(r["event"], former_names):
                continue
            if new_nickname:
                old = next((n for n in former_names if n in r["event"]), former_names[0])
                rewritten = f'{FORMER_NICKNAME_PREFIX}"{old}"（{today} 起改用"{new_nickname}"）'
                conn.execute(
                    "UPDATE thing_memory SET event=? WHERE id=? AND status='active'",
                    (rewritten, r["id"]),
                )
            else:
                conn.execute(
                    "UPDATE thing_memory SET status='superseded' WHERE id=? AND status='active'",
                    (r["id"],),
                )
            count += 1
        if count:
            conn.commit()
    # 失效内存缓存，否则进程内仍会注入旧记忆
    _invalidate(workspace_root, user_id)
    return count


def _normalize(text: str) -> str:
    return re.sub(r"[^一-鿿a-z0-9]", "", text.lower())


# ── Conversation archive ──────────────────────────────────────────────────────

def archive_messages(workspace_root: str, user_id: str, messages: list) -> None:
    if not messages:
        return
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    path = db_path(workspace_root)
    rows = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        rows.append((user_id, role, content, now))
    with _conn(path) as conn:
        conn.executemany(
            "INSERT INTO conversation_archive (user_id, role, content, archived_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
