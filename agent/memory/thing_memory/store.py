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
    now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
    return False


def _normalize(text: str) -> str:
    return re.sub(r"[^一-鿿a-z0-9]", "", text.lower())


# ── Conversation archive ──────────────────────────────────────────────────────

def archive_messages(workspace_root: str, user_id: str, messages: list) -> None:
    if not messages:
        return
    import datetime as _dt
    now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
