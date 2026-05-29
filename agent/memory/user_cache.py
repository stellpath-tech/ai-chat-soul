"""
User session disk cache.

Each active user gets a JSON file under {workspace}/cache/{user_id}.json.
The file holds their current conversation, nickname, and memory snapshot.

Lifecycle:
  touch(workspace, uid)          - called on ANY request; cold-loads from DB if needed
  update_*(workspace, uid, ...)  - update one field in the file (conversation / memory / nickname)
  flush(workspace, uid)          - write file back to current_setting table, delete file
  start_eviction_thread(ws)      - background thread; flushes files idle > TTL
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

_CACHE_TTL = 180        # seconds until idle user is flushed
_EVICT_INTERVAL = 60    # how often the background thread runs
MAX_TURNS = 40          # max user turns kept in current_setting


def _truncate(messages: list) -> tuple[list, list]:
    """Split messages into (keep_last_N_turns, evicted). Splits on user turn boundaries."""
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) <= MAX_TURNS:
        return messages, []
    cutoff = user_indices[-MAX_TURNS]
    return messages[cutoff:], messages[:cutoff]


# ── File paths ────────────────────────────────────────────────────────────────

def _cache_dir(workspace_root: str) -> str:
    return os.path.join(workspace_root, "cache")


def _file_path(workspace_root: str, user_id: str) -> str:
    safe = user_id.replace("/", "_").replace("\\", "_").replace(":", "_")
    return os.path.join(_cache_dir(workspace_root), f"{safe}.json")


# ── Internal file I/O ─────────────────────────────────────────────────────────

def _read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_from_db(workspace_root: str, user_id: str) -> dict:
    """Read current_setting row from SQLite. Returns empty skeleton if not found."""
    from agent.memory.thing_memory.store import _conn, db_path
    path = db_path(workspace_root)
    if os.path.exists(path):
        try:
            with _conn(path) as conn:
                row = conn.execute(
                    "SELECT conversation, nickname, memory FROM current_setting WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            if row:
                return {
                    "user_id": user_id,
                    "conversation": json.loads(row["conversation"] or "[]"),
                    "nickname": row["nickname"],
                    "memory": json.loads(row["memory"] or "[]"),
                    "last_active": time.time(),
                }
        except Exception:
            pass
    return {"user_id": user_id, "conversation": [], "nickname": None, "memory": [], "last_active": time.time()}


def _save_to_db(workspace_root: str, data: dict) -> None:
    """Upsert data dict into current_setting table."""
    import datetime as _dt
    from agent.memory.thing_memory.store import _conn, db_path
    now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    uid = data["user_id"]
    conv = json.dumps(data.get("conversation", []), ensure_ascii=False)
    mem = json.dumps(data.get("memory", []), ensure_ascii=False)
    nick = data.get("nickname")
    path = db_path(workspace_root)
    try:
        with _conn(path) as conn:
            conn.execute(
                "INSERT INTO current_setting (user_id, conversation, nickname, memory, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "conversation=excluded.conversation, nickname=excluded.nickname, "
                "memory=excluded.memory, updated_at=excluded.updated_at",
                (uid, conv, nick, mem, now),
            )
            conn.commit()
    except Exception as e:
        from common.log import logger
        logger.warning(f"[UserCache] DB flush failed for {uid}: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def touch(workspace_root: str, user_id: str) -> dict:
    """
    Called on every incoming request.
    If file cache exists: refresh last_active and return.
    If not: cold-load from DB, write to file.
    Returns the current cached data dict.
    """
    path = _file_path(workspace_root, user_id)
    data = _read(path)
    if data is None:
        data = _load_from_db(workspace_root, user_id)
    data["last_active"] = time.time()
    _write(path, data)
    return data


def get(workspace_root: str, user_id: str) -> Optional[dict]:
    """Read cached data without touching last_active."""
    path = _file_path(workspace_root, user_id)
    return _read(path)


def update_conversation(workspace_root: str, user_id: str, messages: list) -> None:
    path = _file_path(workspace_root, user_id)
    data = _read(path) or {"user_id": user_id, "nickname": None, "memory": []}
    data["conversation"] = messages
    data["last_active"] = time.time()
    _write(path, data)


def update_memory(workspace_root: str, user_id: str, memories: list) -> None:
    path = _file_path(workspace_root, user_id)
    data = _read(path) or {"user_id": user_id, "conversation": [], "nickname": None}
    data["memory"] = memories
    data["last_active"] = time.time()
    _write(path, data)


def update_nickname(workspace_root: str, user_id: str, nickname: str) -> None:
    path = _file_path(workspace_root, user_id)
    data = _read(path) or {"user_id": user_id, "conversation": [], "memory": []}
    data["nickname"] = nickname
    data["last_active"] = time.time()
    _write(path, data)


def flush(workspace_root: str, user_id: str) -> None:
    """
    Write file back to SQLite and delete the file.
    Truncates conversation to MAX_TURNS; archives the evicted portion.
    """
    path = _file_path(workspace_root, user_id)
    data = _read(path)
    if data is None:
        return

    messages = data.get("conversation", [])
    truncated, evicted = _truncate(messages)
    data["conversation"] = truncated

    _save_to_db(workspace_root, data)

    if evicted:
        try:
            from agent.memory.thing_memory.store import archive_messages
            archive_messages(workspace_root, user_id, evicted)
        except Exception as e:
            from common.log import logger
            logger.warning(f"[UserCache] archive_messages failed for {user_id}: {e}")

    try:
        os.remove(path)
    except OSError:
        pass


# ── Background eviction ───────────────────────────────────────────────────────

def _evict_once(workspace_root: str) -> None:
    cache_dir = _cache_dir(workspace_root)
    if not os.path.isdir(cache_dir):
        return
    now = time.time()
    for fname in os.listdir(cache_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(cache_dir, fname)
        data = _read(fpath)
        if data is None:
            continue
        if now - data.get("last_active", 0) > _CACHE_TTL:
            _save_to_db(workspace_root, data)
            try:
                os.remove(fpath)
            except OSError:
                pass


def start_eviction_thread(workspace_root: str) -> None:
    """Start a daemon thread that flushes idle users every _EVICT_INTERVAL seconds."""
    def _loop():
        while True:
            time.sleep(_EVICT_INTERVAL)
            try:
                _evict_once(workspace_root)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True, name="user-cache-evict").start()
