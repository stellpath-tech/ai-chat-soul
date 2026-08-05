import sqlite3
import os
import time
import json
import uuid
import re
import threading
from datetime import datetime, timedelta, timezone
from contextlib import closing
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# Store DB in workspace data dir
DB_DIR = os.path.expanduser('~/cow/data')
DB_PATH = os.path.join(DB_DIR, 'soul.db')
DEFAULT_USER_NICKNAME = "宝宝"
DEFAULT_BEAR_NICKNAME = "满仓"
MAX_CHAT_MESSAGES_PER_USER = 1000
APP_TIMEZONE = timezone(timedelta(hours=8))
DIARY_STATES = {"GENERATING", "DONE", "SKIPPED"}
_USER_TIMEZONE_CACHE = {}
_USER_TIMEZONE_CACHE_LOCK = threading.Lock()

def get_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _now_app_timezone():
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)

def _now_app_timezone_str():
    return _now_app_timezone().strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          phone_number VARCHAR(255) NOT NULL UNIQUE,
          invite_code VARCHAR(255) NOT NULL DEFAULT '',
          user_group TINYINT NOT NULL DEFAULT -1,
          auth_token VARCHAR(255) NOT NULL DEFAULT '',
          nickname VARCHAR(255) DEFAULT NULL,
          used_nickname TEXT NOT NULL DEFAULT '[]',
          account_status VARCHAR(32) NOT NULL DEFAULT 'active',
          tz_iana VARCHAR(255) NOT NULL DEFAULT 'Asia/Shanghai',
          tz_offset_min INTEGER NOT NULL DEFAULT 480,
          tz_updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          deletion_requested_at DATETIME DEFAULT NULL,
          deletion_deadline DATETIME DEFAULT NULL,
          deleted_at DATETIME DEFAULT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        _ensure_columns(cursor, "user", {
            "nickname": "VARCHAR(255) DEFAULT NULL",
            "used_nickname": "TEXT NOT NULL DEFAULT '[]'",
            "account_status": "VARCHAR(32) NOT NULL DEFAULT 'active'",
            "tz_iana": "VARCHAR(255) NOT NULL DEFAULT 'Asia/Shanghai'",
            "tz_offset_min": "INTEGER NOT NULL DEFAULT 480",
            "tz_updated_at": "DATETIME DEFAULT NULL",
            "deletion_requested_at": "DATETIME DEFAULT NULL",
            "deletion_deadline": "DATETIME DEFAULT NULL",
            "deleted_at": "DATETIME DEFAULT NULL",
        })

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_push_device (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id BIGINT NOT NULL UNIQUE,
          platform VARCHAR(32) NOT NULL,
          push_token TEXT NOT NULL,
          device_id VARCHAR(255) NOT NULL DEFAULT '',
          app_version VARCHAR(64) NOT NULL DEFAULT '',
          os_version VARCHAR(64) NOT NULL DEFAULT '',
          device_brand VARCHAR(64) NOT NULL DEFAULT '',
          device_model VARCHAR(128) NOT NULL DEFAULT '',
          push_provider VARCHAR(32) NOT NULL DEFAULT 'TIMPUSH',
          enabled TINYINT NOT NULL DEFAULT 1,
          last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_push_device_token "
            "ON user_push_device(push_token)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_push_device_enabled "
            "ON user_push_device(enabled, last_seen_at)"
        )
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS invite_code (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          invite_code VARCHAR(255) NOT NULL UNIQUE,
          expire_at DATETIME NOT NULL,
          user_group TINYINT NOT NULL DEFAULT -1,
          used_by_phone VARCHAR(255) DEFAULT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_behavior_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id BIGINT NOT NULL DEFAULT -1,
          event_name VARCHAR(255) NOT NULL DEFAULT '',
          properties TEXT,
          timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_feedback (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id BIGINT NOT NULL,
          feedback_type VARCHAR(32) DEFAULT NULL,
          description TEXT NOT NULL,
          images TEXT NOT NULL DEFAULT '[]',
          contact VARCHAR(255) DEFAULT NULL,
          repair_status VARCHAR(32) NOT NULL DEFAULT '未标记',
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_feedback_comment (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          feedback_id BIGINT NOT NULL,
          content TEXT NOT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_feedback_comment_feedback_id ON user_feedback_comment(feedback_id, id)")

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_chat_message (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id BIGINT NOT NULL,
          session_id VARCHAR(255) NOT NULL DEFAULT '',
          role VARCHAR(32) NOT NULL,
          content TEXT NOT NULL,
          image_url TEXT NOT NULL DEFAULT '',
          message_type VARCHAR(32) NOT NULL DEFAULT 'text',
          weather_text VARCHAR(255) NOT NULL DEFAULT '',
          diary_title VARCHAR(255) NOT NULL DEFAULT '',
          diary_summary TEXT NOT NULL DEFAULT '',
          source VARCHAR(32) NOT NULL DEFAULT 'APP',
          request_id VARCHAR(64) NOT NULL DEFAULT '',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        _ensure_columns(cursor, "user_chat_message", {
            "image_url": "TEXT NOT NULL DEFAULT ''",
            "weather_text": "VARCHAR(255) NOT NULL DEFAULT ''",
            "diary_title": "VARCHAR(255) NOT NULL DEFAULT ''",
            "diary_summary": "TEXT NOT NULL DEFAULT ''",
        })
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_chat_message_user_id_id ON user_chat_message(user_id, id)")

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_diary (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id BIGINT NOT NULL,
          diary_date VARCHAR(10) NOT NULL,
          state VARCHAR(32) NOT NULL DEFAULT 'GENERATING',
          title VARCHAR(255) NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          image_urls TEXT NOT NULL DEFAULT '[]',
          weather_text VARCHAR(255) NOT NULL DEFAULT '',
          mode VARCHAR(16) NOT NULL DEFAULT 'auto',
          source_message_ids TEXT NOT NULL DEFAULT '[]',
          source_transcript_hash VARCHAR(64) NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '',
          retry_count INTEGER NOT NULL DEFAULT 0,
          next_retry_at DATETIME DEFAULT NULL,
          generation_started_at DATETIME DEFAULT NULL,
          generated_at DATETIME DEFAULT NULL,
          push_state VARCHAR(32) NOT NULL DEFAULT 'NONE',
          push_retry_count INTEGER NOT NULL DEFAULT 0,
          push_next_retry_at DATETIME DEFAULT NULL,
          push_started_at DATETIME DEFAULT NULL,
          push_task_id VARCHAR(255) NOT NULL DEFAULT '',
          push_error TEXT NOT NULL DEFAULT '',
          push_sent_at DATETIME DEFAULT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(user_id, diary_date)
        )
        ''')
        _ensure_columns(cursor, "user_diary", {
            "title": "VARCHAR(255) NOT NULL DEFAULT ''",
            "content": "TEXT NOT NULL DEFAULT ''",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "image_urls": "TEXT NOT NULL DEFAULT '[]'",
            "weather_text": "VARCHAR(255) NOT NULL DEFAULT ''",
            "mode": "VARCHAR(16) NOT NULL DEFAULT 'auto'",
            "source_message_ids": "TEXT NOT NULL DEFAULT '[]'",
            "source_transcript_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
            "error": "TEXT NOT NULL DEFAULT ''",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "DATETIME DEFAULT NULL",
            "generation_started_at": "DATETIME DEFAULT NULL",
            "generated_at": "DATETIME DEFAULT NULL",
            "push_state": "VARCHAR(32) NOT NULL DEFAULT 'NONE'",
            "push_retry_count": "INTEGER NOT NULL DEFAULT 0",
            "push_next_retry_at": "DATETIME DEFAULT NULL",
            "push_started_at": "DATETIME DEFAULT NULL",
            "push_task_id": "VARCHAR(255) NOT NULL DEFAULT ''",
            "push_error": "TEXT NOT NULL DEFAULT ''",
            "push_sent_at": "DATETIME DEFAULT NULL",
        })
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_diary_user_state_date ON user_diary(user_id, state, diary_date)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_diary_push_pending "
            "ON user_diary(push_state, push_next_retry_at, push_started_at)"
        )
        
        conn.commit()

def _ensure_columns(cursor, table_name, columns):
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing = {row["name"] for row in cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")

def generate_token():
    return uuid.uuid4().hex

def check_and_use_invite_code(phone_number, code):
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM invite_code WHERE invite_code = ?", (code,))
        row = cursor.fetchone()
        if not row:
            return False, "无效的内测码", "invite_code_invalid"
            
        expire_at = datetime.strptime(row['expire_at'], "%Y-%m-%d %H:%M:%S")
        if _now_app_timezone() > expire_at:
            return False, "内测码已过期", "invite_code_expired"
            
        user_group = row['user_group']
        used_by_phone = row['used_by_phone']
        
        if used_by_phone and used_by_phone != phone_number:
            return False, "内测码已被其他手机号绑定", "invite_code_bound"
            
        return True, user_group, None

def register_or_login(phone_number, invite_code):
    if not phone_number or not re.match(r'^\+\d{1,4}\d{6,14}$', phone_number):
        return False, "手机号格式错误（需包含区号，如 +8613800000000）", None, "phone_format_error"

    existing_user = get_user_by_phone(phone_number)
    if existing_user:
        if existing_user["account_status"] == "pending_deletion":
            return False, "账号正在注销中", None, "ACCOUNT_PENDING_DELETION"
        if existing_user["account_status"] == "deleted":
            return False, "账号已注销，无法登录", None, "ACCOUNT_DELETED"

    success, msg_or_group, err_type = check_and_use_invite_code(phone_number, invite_code)
    if not success:
        return False, msg_or_group, None, err_type

    user_group = msg_or_group
    token = generate_token()
    now_str = _now_app_timezone_str()

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM user WHERE phone_number = ?", (phone_number,))
        user = cursor.fetchone()
        
        if user:
            if user['account_status'] == 'pending_deletion':
                return False, "账号正在注销中", None, "ACCOUNT_PENDING_DELETION"
            if user['account_status'] == 'deleted':
                return False, "账号已注销，无法登录", None, "ACCOUNT_DELETED"
            cursor.execute("""
                UPDATE user SET invite_code = ?, user_group = ?, auth_token = ?, account_status = 'active', updated_at = ?
                WHERE id = ?
            """, (invite_code, user_group, token, now_str, user['id']))
            action = "login"
        else:
            cursor.execute("""
                INSERT INTO user (phone_number, invite_code, user_group, auth_token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (phone_number, invite_code, user_group, token, now_str, now_str))
            action = "register"
            
        cursor.execute("""
            UPDATE invite_code SET used_by_phone = ?, updated_at = ? WHERE invite_code = ?
        """, (phone_number, now_str, invite_code))
        
        conn.commit()
        return True, "Success", token, action

def get_user_by_phone(phone_number):
    if not phone_number:
        return None
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user WHERE phone_number = ?", (phone_number,))
        user = cursor.fetchone()
        return dict(user) if user else None

def get_user_by_token(token):
    if not token:
        return None
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user WHERE auth_token = ?", (token,))
        user = cursor.fetchone()
        return dict(user) if user else None

def get_active_user_by_token(token):
    user = get_user_by_token(token)
    if not user or user.get("account_status") != "active":
        return None
    return user

def register_user_push_device(
    user_id,
    platform,
    push_token,
    device_id="",
    app_version="",
    os_version="",
    device_brand="",
    device_model="",
):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM user_push_device WHERE push_token = ? AND user_id != ?",
            (push_token, user_id),
        )
        conn.execute("""
            INSERT INTO user_push_device
            (user_id, platform, push_token, device_id, app_version, os_version,
             device_brand, device_model, push_provider, enabled, last_seen_at,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'TIMPUSH', 1, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                platform = excluded.platform,
                push_token = excluded.push_token,
                device_id = excluded.device_id,
                app_version = excluded.app_version,
                os_version = excluded.os_version,
                device_brand = excluded.device_brand,
                device_model = excluded.device_model,
                push_provider = 'TIMPUSH',
                enabled = 1,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
        """, (
            user_id, platform, push_token, device_id or "", app_version or "",
            os_version or "", device_brand or "", device_model or "",
            now_str, now_str, now_str,
        ))
        conn.commit()

def unregister_user_push_device(user_id, push_token):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        cursor = conn.execute("""
            UPDATE user_push_device
            SET enabled = 0, updated_at = ?
            WHERE user_id = ? AND push_token = ? AND enabled = 1
        """, (now_str, user_id, push_token))
        conn.commit()
        return cursor.rowcount == 1

def get_user_push_device(user_id):
    with closing(get_db()) as conn:
        row = conn.execute("""
            SELECT user_id, platform, push_token, device_id, app_version, os_version,
                   device_brand, device_model, enabled, last_seen_at
            FROM user_push_device
            WHERE user_id = ?
        """, (user_id,)).fetchone()
    return dict(row) if row else None

def record_user_timezone_async(user_id, timezone_payload):
    profile = _normalize_user_timezone(timezone_payload)
    if not user_id or user_id == -1 or not profile:
        return False

    cache_key = (profile["tz_iana"], profile["tz_offset_min"])
    with _USER_TIMEZONE_CACHE_LOCK:
        if _USER_TIMEZONE_CACHE.get(user_id) == cache_key:
            return False
        _USER_TIMEZONE_CACHE[user_id] = cache_key

    threading.Thread(
        target=_write_user_timezone_with_cache_guard,
        args=(user_id, profile, cache_key),
        daemon=True,
        name="user-timezone-update",
    ).start()
    return True

def _normalize_user_timezone(timezone_payload):
    if not isinstance(timezone_payload, dict):
        return None

    tz_iana = str(timezone_payload.get("tz_iana") or "").strip()
    tz_offset_min = timezone_payload.get("tz_offset_min")
    if not tz_iana or tz_offset_min is None:
        return None

    try:
        tz_offset_min = int(tz_offset_min)
    except (TypeError, ValueError):
        return None

    return {
        "tz_iana": tz_iana,
        "tz_offset_min": tz_offset_min,
    }

def _write_user_timezone_with_cache_guard(user_id, profile, cache_key):
    with _USER_TIMEZONE_CACHE_LOCK:
        if _USER_TIMEZONE_CACHE.get(user_id) != cache_key:
            return
        try:
            _write_user_timezone(user_id, profile)
        except Exception:
            if _USER_TIMEZONE_CACHE.get(user_id) == cache_key:
                _USER_TIMEZONE_CACHE.pop(user_id, None)

def _write_user_timezone(user_id, profile):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        conn.execute(
            """
            UPDATE user
            SET tz_iana = ?, tz_offset_min = ?, tz_updated_at = ?
            WHERE id = ?
            """,
            (profile["tz_iana"], profile["tz_offset_min"], now_str, user_id),
        )
        conn.commit()

def get_user_profile(user_id):
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nickname FROM user WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return {
                "userNickname": DEFAULT_USER_NICKNAME,
                "bearNickname": DEFAULT_BEAR_NICKNAME,
            }
        return {
            "userNickname": row["nickname"] or DEFAULT_USER_NICKNAME,
            "bearNickname": DEFAULT_BEAR_NICKNAME,
        }

def update_user_nickname(user_id, user_nickname):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nickname, used_nickname FROM user WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        used = []
        try:
            used = json.loads(row["used_nickname"] or "[]")
        except Exception:
            used = []
        old_nickname = row["nickname"]
        if old_nickname and old_nickname != user_nickname and old_nickname not in used:
            used.append(old_nickname)
        cursor.execute("""
            UPDATE user
            SET nickname = ?, used_nickname = ?, updated_at = ?
            WHERE id = ?
        """, (user_nickname, json.dumps(used, ensure_ascii=False), now_str, user_id))
        conn.commit()
        return old_nickname

def request_account_deletion(user_id):
    now = _now_app_timezone()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    deadline = _add_business_days(now, 15).strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("""
            UPDATE user
            SET account_status = 'pending_deletion',
                deletion_requested_at = ?,
                deletion_deadline = ?,
                auth_token = '',
                updated_at = ?
            WHERE id = ?
        """, (now_str, deadline, now_str, user_id))
        conn.execute("""
            UPDATE user_push_device
            SET enabled = 0, updated_at = ?
            WHERE user_id = ?
        """, (now_str, user_id))
        conn.commit()
    return deadline

def cleanup_expired_deleted_accounts(workspace_root=None, now=None):
    now = now or _now_app_timezone()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    cleaned_user_ids = []

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        rows = cursor.execute("""
            SELECT id
            FROM user
            WHERE account_status = 'pending_deletion'
              AND deletion_deadline IS NOT NULL
              AND deletion_deadline <= ?
        """, (now_str,)).fetchall()

        for row in rows:
            user_id = row["id"]
            _purge_deleted_user_data(cursor, user_id, now_str)
            cleaned_user_ids.append(user_id)

        conn.commit()

    if workspace_root:
        for user_id in cleaned_user_ids:
            _remove_user_cache_file(workspace_root, f"user_{user_id}")

    return len(cleaned_user_ids)

def _purge_deleted_user_data(cursor, user_id, deleted_at):
    user_key = f"user_{user_id}"

    _delete_from_existing_table(cursor, "user_chat_message", "user_id = ?", (user_id,))
    _delete_from_existing_table(cursor, "user_diary", "user_id = ?", (user_id,))
    _delete_from_existing_table(cursor, "user_push_device", "user_id = ?", (user_id,))
    _delete_from_existing_table(
        cursor,
        "user_feedback_comment",
        "feedback_id IN (SELECT id FROM user_feedback WHERE user_id = ?)",
        (user_id,),
    )
    _delete_from_existing_table(cursor, "user_feedback", "user_id = ?", (user_id,))
    _delete_from_existing_table(cursor, "user_behavior_log", "user_id = ?", (user_id,))
    _delete_from_existing_table(cursor, "thing_memory", "user_id = ?", (user_key,))
    _delete_from_existing_table(cursor, "conversation_archive", "user_id = ?", (user_key,))
    _delete_from_existing_table(cursor, "current_setting", "user_id = ?", (user_key,))

    cursor.execute("""
        UPDATE user
        SET invite_code = '',
            user_group = -1,
            auth_token = '',
            nickname = NULL,
            used_nickname = '[]',
            account_status = 'deleted',
            deleted_at = ?,
            updated_at = ?
        WHERE id = ?
    """, (deleted_at, deleted_at, user_id))

def _delete_from_existing_table(cursor, table_name, where_sql, params):
    if not _table_exists(cursor, table_name):
        return
    cursor.execute(f"DELETE FROM {table_name} WHERE {where_sql}", params)

def _table_exists(cursor, table_name):
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None

def _remove_user_cache_file(workspace_root, user_key):
    safe = user_key.replace("/", "_").replace("\\", "_").replace(":", "_")
    cache_path = os.path.join(workspace_root, "cache", f"{safe}.json")
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except OSError:
        pass

def _add_business_days(start, days):
    current = start
    added = 0
    while added < days:
        current = current + timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current

def create_feedback(user_id, feedback_type, description, images, contact):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        conn.execute("""
            INSERT INTO user_feedback (user_id, feedback_type, description, images, contact, repair_status, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, '未标记', ?, ?)
        """, (user_id, feedback_type, description, json.dumps(images or [], ensure_ascii=False), contact, now_str, now_str))
        conn.commit()

def list_feedbacks(keyword=None, repair_status=None, order="desc", limit=30, offset=0):
    safe_limit = max(1, min(int(limit or 30), 100))
    safe_offset = max(0, int(offset or 0))
    sort = "ASC" if str(order).lower() == "asc" else "DESC"
    where = []
    params = []

    if keyword:
        where.append("(f.description LIKE ? OR f.contact LIKE ?)")
        like = f"%{keyword.strip()}%"
        params.extend([like, like])
    if repair_status:
        where.append("f.repair_status = ?")
        params.append(repair_status)

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    with closing(get_db()) as conn:
        rows = conn.execute(f"""
            SELECT
                f.id,
                f.user_id,
                u.phone_number,
                f.feedback_type,
                f.description,
                f.images,
                f.contact,
                f.repair_status,
                f.updated_at,
                f.created_at,
                COUNT(c.id) AS comment_count
            FROM user_feedback f
            LEFT JOIN user u ON u.id = f.user_id
            LEFT JOIN user_feedback_comment c ON c.feedback_id = f.id
            {where_sql}
            GROUP BY f.id
            ORDER BY f.created_at {sort}, f.id {sort}
            LIMIT ? OFFSET ?
        """, params + [safe_limit, safe_offset]).fetchall()

        total_row = conn.execute(f"""
            SELECT COUNT(*) AS total
            FROM user_feedback f
            LEFT JOIN user u ON u.id = f.user_id
            {where_sql}
        """, params).fetchone()

        status_rows = conn.execute("""
            SELECT repair_status, COUNT(*) AS count
            FROM user_feedback
            GROUP BY repair_status
        """).fetchall()

        feedback_ids = [row["id"] for row in rows]
        comments_by_feedback = _load_feedback_comments(conn, feedback_ids)

        return {
            "items": [_format_feedback_row(row, comments_by_feedback.get(row["id"], [])) for row in rows],
            "total": total_row["total"] if total_row else 0,
            "limit": safe_limit,
            "offset": safe_offset,
            "statusCounts": {row["repair_status"]: row["count"] for row in status_rows},
        }

def update_feedback_repair_status(feedback_id, repair_status):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_feedback
            SET repair_status = ?, updated_at = ?
            WHERE id = ?
        """, (repair_status, now_str, feedback_id))
        conn.commit()
        return cursor.rowcount > 0

def add_feedback_comment(feedback_id, content):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        exists = cursor.execute("SELECT 1 FROM user_feedback WHERE id = ?", (feedback_id,)).fetchone()
        if not exists:
            return None
        cursor.execute("""
            INSERT INTO user_feedback_comment (feedback_id, content, created_at)
            VALUES (?, ?, ?)
        """, (feedback_id, content, now_str))
        conn.execute("UPDATE user_feedback SET updated_at = ? WHERE id = ?", (now_str, feedback_id))
        comment_id = cursor.lastrowid
        conn.commit()
        return {
            "id": comment_id,
            "feedbackId": feedback_id,
            "content": content,
            "createdAt": now_str,
        }

def _load_feedback_comments(conn, feedback_ids):
    if not feedback_ids:
        return {}
    placeholders = ",".join("?" for _ in feedback_ids)
    rows = conn.execute(f"""
        SELECT id, feedback_id, content, created_at
        FROM user_feedback_comment
        WHERE feedback_id IN ({placeholders})
        ORDER BY id ASC
    """, feedback_ids).fetchall()
    comments = {}
    for row in rows:
        comments.setdefault(row["feedback_id"], []).append({
            "id": row["id"],
            "feedbackId": row["feedback_id"],
            "content": row["content"],
            "createdAt": row["created_at"],
        })
    return comments

def _format_feedback_row(row, comments):
    try:
        images = json.loads(row["images"] or "[]")
    except Exception:
        images = []
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "phoneNumber": row["phone_number"] or "",
        "type": row["feedback_type"] or "",
        "description": row["description"],
        "images": images,
        "contact": row["contact"] or "",
        "repairStatus": row["repair_status"],
        "commentCount": row["comment_count"],
        "comments": comments,
        "updatedAt": row["updated_at"],
        "createdAt": row["created_at"],
    }

def append_chat_message(
    user_id,
    session_id,
    role,
    content,
    message_type="text",
    source="APP",
    request_id="",
    image_url="",
    weather_text="",
    diary_title="",
    diary_summary="",
):
    content_text = content or ""
    image_url_text = image_url or ""
    weather_text = weather_text or ""
    diary_title = diary_title or ""
    diary_summary = diary_summary or ""
    has_card_payload = message_type == "card" and (weather_text or diary_title or diary_summary)
    if not user_id or user_id == -1 or (not content_text and not image_url_text and not has_card_payload):
        return None
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_chat_message
            (user_id, session_id, role, content, image_url, message_type, weather_text, diary_title, diary_summary, source, request_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            session_id or "",
            role,
            content_text,
            image_url_text,
            message_type,
            weather_text,
            diary_title,
            diary_summary,
            source or "APP",
            request_id or "",
            now_str,
        ))
        message_id = cursor.lastrowid
        cursor.execute("""
            DELETE FROM user_chat_message
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM user_chat_message
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
              )
        """, (user_id, user_id, MAX_CHAT_MESSAGES_PER_USER))
        conn.commit()
        return message_id

def append_diary_card_message(user_id, diary_title, diary_summary, weather_text="", request_id=""):
    if request_id:
        with closing(get_db()) as conn:
            existing = conn.execute("""
                SELECT id FROM user_chat_message
                WHERE user_id = ? AND message_type = 'card' AND request_id = ?
            """, (user_id, request_id)).fetchone()
        if existing:
            with closing(get_db()) as conn:
                conn.execute("""
                    UPDATE user_chat_message
                    SET content = ?, diary_title = ?, diary_summary = ?, weather_text = ?
                    WHERE id = ?
                """, (
                    diary_summary or diary_title or "", diary_title or "",
                    diary_summary or "", weather_text or "", existing["id"],
                ))
                conn.commit()
            return existing["id"]
    return append_chat_message(
        user_id=user_id,
        session_id=f"user_{user_id}",
        role="assistant",
        content=diary_summary or diary_title or "",
        message_type="card",
        source="APP",
        request_id=request_id or "",
        weather_text=weather_text or "",
        diary_title=diary_title or "",
        diary_summary=diary_summary or "",
    )

def list_chat_messages(user_id, offset=None, limit=50):
    safe_limit = max(1, min(int(limit or 50), 100))
    params = [user_id]
    where = "user_id = ?"
    if offset:
        where += " AND id < ?"
        params.append(int(offset))
    params.append(safe_limit)
    with closing(get_db()) as conn:
        rows = conn.execute(f"""
            SELECT id, role, content, image_url, message_type, weather_text, diary_title, diary_summary, source, request_id, created_at
            FROM user_chat_message
            WHERE {where}
            ORDER BY id DESC
            LIMIT ?
        """, params).fetchall()
        desc_messages = [dict(row) for row in rows]
        messages = list(reversed(desc_messages))
        next_offset = desc_messages[-1]["id"] if desc_messages else None
        has_more = False
        if next_offset is not None:
            more = conn.execute(
                "SELECT 1 FROM user_chat_message WHERE user_id = ? AND id < ? LIMIT 1",
                (user_id, next_offset),
            ).fetchone()
            has_more = more is not None
        return {
            "messages": [
                {
                    "id": msg["id"],
                    "role": msg["role"],
                    "content": msg["content"],
                    "imageUrl": msg["image_url"] or "",
                    "messageType": msg["message_type"],
                    "weatherText": msg["weather_text"] or "",
                    "diaryTitle": msg["diary_title"] or "",
                    "diarySummary": msg["diary_summary"] or "",
                    "source": msg["source"],
                    "requestId": msg["request_id"],
                    "createdAt": msg["created_at"],
                }
                for msg in messages
            ],
            "nextOffset": next_offset,
            "hasMore": has_more,
            "limit": safe_limit,
        }

def get_user_diary_detail(user_id, ts_ms):
    diary_date = _diary_date_from_ts_ms(user_id, ts_ms)
    with closing(get_db()) as conn:
        row = conn.execute("""
            SELECT state, title, content, image_urls, generated_at, created_at
            FROM user_diary
            WHERE user_id = ? AND diary_date = ?
        """, (user_id, diary_date)).fetchone()

    if not row:
        return {
            "createdAt": 0,
            "state": "EMPTY",
            "imageUrls": [],
            "title": "",
            "content": "",
        }

    image_urls = _loads_json_array(row["image_urls"])
    created_at = row["generated_at"] or row["created_at"]
    return {
        "createdAt": _datetime_str_to_ms(created_at),
        "state": row["state"] if row["state"] in DIARY_STATES else "GENERATING",
        "imageUrls": image_urls,
        "title": row["title"] or "",
        "content": row["content"] or "",
    }

def list_active_users_for_diary():
    with closing(get_db()) as conn:
        rows = conn.execute("""
            SELECT id, tz_iana, tz_offset_min
            FROM user
            WHERE account_status = 'active'
            ORDER BY id
        """).fetchall()
    return [dict(row) for row in rows]

def create_or_reset_diary_job(user_id, diary_date, mode="auto", force=False):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        existing = cursor.execute("""
            SELECT id, state, generation_started_at FROM user_diary
            WHERE user_id = ? AND diary_date = ?
        """, (user_id, diary_date)).fetchone()
        if existing and not force:
            return dict(existing)
        if existing and existing["state"] == "GENERATING" and existing["generation_started_at"]:
            return dict(existing)
        if existing:
            cursor.execute("""
                UPDATE user_diary
                SET state = 'GENERATING', mode = ?, title = '', content = '', summary = '',
                    image_urls = '[]', weather_text = '', source_message_ids = '[]',
                    source_transcript_hash = '', error = '', retry_count = 0,
                    next_retry_at = NULL, generation_started_at = NULL,
                    generated_at = NULL, push_state = 'NONE', push_retry_count = 0,
                    push_next_retry_at = NULL, push_started_at = NULL,
                    push_task_id = '', push_error = '', push_sent_at = NULL,
                    updated_at = ?
                WHERE id = ?
            """, (mode or "auto", now_str, existing["id"]))
            diary_id = existing["id"]
        else:
            cursor.execute("""
                INSERT INTO user_diary
                (user_id, diary_date, state, mode, created_at, updated_at)
                VALUES (?, ?, 'GENERATING', ?, ?, ?)
            """, (user_id, diary_date, mode or "auto", now_str, now_str))
            diary_id = cursor.lastrowid
        conn.commit()
        return {"id": diary_id, "state": "GENERATING"}

def claim_diary_job(user_id, diary_date, stale_after_minutes=30):
    now = _now_app_timezone()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    stale_before = (now - timedelta(minutes=stale_after_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT * FROM user_diary
            WHERE user_id = ? AND diary_date = ? AND state = 'GENERATING'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
              AND (generation_started_at IS NULL OR generation_started_at <= ?)
        """, (user_id, diary_date, now_str, stale_before)).fetchone()
        if not row:
            conn.rollback()
            return None
        cursor = conn.execute("""
            UPDATE user_diary
            SET generation_started_at = ?, updated_at = ?
            WHERE id = ? AND state = 'GENERATING'
        """, (now_str, now_str, row["id"]))
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        result = dict(row)
        result["generation_started_at"] = now_str
        return result

def list_chat_messages_in_window(user_id, start_at, end_at):
    with closing(get_db()) as conn:
        rows = conn.execute("""
            SELECT id, role, content, image_url, message_type, weather_text, created_at
            FROM user_chat_message
            WHERE user_id = ? AND created_at >= ? AND created_at < ?
              AND message_type != 'card'
            ORDER BY id ASC
        """, (user_id, start_at, end_at)).fetchall()
    return [dict(row) for row in rows]

def complete_diary_job(
    diary_id,
    title,
    content,
    summary,
    image_urls,
    weather_text,
    mode,
    source_message_ids,
    source_transcript_hash,
):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        conn.execute("""
            UPDATE user_diary
            SET state = 'DONE', title = ?, content = ?, summary = ?, image_urls = ?,
                weather_text = ?, mode = ?, source_message_ids = ?, source_transcript_hash = ?,
                error = '', next_retry_at = NULL, generation_started_at = NULL,
                generated_at = ?, push_state = 'PENDING', push_retry_count = 0,
                push_next_retry_at = NULL, push_started_at = NULL,
                push_task_id = '', push_error = '', push_sent_at = NULL,
                updated_at = ?
            WHERE id = ?
        """, (
            title or "", content or "", summary or "",
            json.dumps(image_urls or [], ensure_ascii=False), weather_text or "", mode or "auto",
            json.dumps(source_message_ids or [], ensure_ascii=False), source_transcript_hash or "",
            now_str, now_str, diary_id,
        ))
        conn.commit()

def claim_diary_push_notification(user_id=None, diary_date=None, stale_after_minutes=10):
    now = _now_app_timezone()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    stale_before = (now - timedelta(minutes=stale_after_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    where = [
        "d.state = 'DONE'",
        "d.push_state = 'PENDING'",
        "(d.push_next_retry_at IS NULL OR d.push_next_retry_at <= ?)",
        "(d.push_started_at IS NULL OR d.push_started_at <= ?)",
        "u.account_status = 'active'",
    ]
    params = [now_str, stale_before]
    if user_id is not None:
        where.append("d.user_id = ?")
        params.append(int(user_id))
    if diary_date is not None:
        where.append("d.diary_date = ?")
        params.append(str(diary_date))

    with closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"""
            SELECT d.id AS diary_id, d.user_id, d.diary_date,
                   u.tz_iana, u.tz_offset_min,
                   p.push_token, p.platform
            FROM user_diary d
            JOIN user u ON u.id = d.user_id
            LEFT JOIN user_push_device p
              ON p.user_id = d.user_id AND p.enabled = 1
            WHERE {' AND '.join(where)}
            ORDER BY d.id
            LIMIT 1
        """, params).fetchone()
        if not row:
            conn.rollback()
            return None
        cursor = conn.execute("""
            UPDATE user_diary
            SET push_started_at = ?, updated_at = ?
            WHERE id = ? AND push_state = 'PENDING'
        """, (now_str, now_str, row["diary_id"]))
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return dict(row)

def mark_diary_push_sent(diary_id, task_id):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        conn.execute("""
            UPDATE user_diary
            SET push_state = 'SENT', push_task_id = ?, push_error = '',
                push_next_retry_at = NULL, push_started_at = NULL,
                push_sent_at = ?, updated_at = ?
            WHERE id = ?
        """, (task_id or "", now_str, now_str, diary_id))
        conn.commit()

def mark_diary_push_skipped(diary_id, reason):
    now_str = _now_app_timezone_str()
    with closing(get_db()) as conn:
        conn.execute("""
            UPDATE user_diary
            SET push_state = 'SKIPPED', push_error = ?,
                push_next_retry_at = NULL, push_started_at = NULL,
                updated_at = ?
            WHERE id = ?
        """, (str(reason or "")[:4000], now_str, diary_id))
        conn.commit()

def mark_diary_push_failed(diary_id, error, max_retries=3, now=None):
    now = now or _now_app_timezone()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT push_retry_count FROM user_diary WHERE id = ?",
            (diary_id,),
        ).fetchone()
        retry_count = (int(row["push_retry_count"]) if row else 0) + 1
        if retry_count >= max(1, int(max_retries)):
            state = "FAILED"
            next_retry_at = None
        else:
            state = "PENDING"
            delay_minutes = min(60, 2 ** (retry_count - 1))
            next_retry_at = (now + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            UPDATE user_diary
            SET push_state = ?, push_retry_count = ?, push_next_retry_at = ?,
                push_started_at = NULL, push_error = ?, updated_at = ?
            WHERE id = ?
        """, (
            state, retry_count, next_retry_at, str(error or "")[:4000],
            now_str, diary_id,
        ))
        conn.commit()
        return state

def get_diary_push_delivery(user_id, diary_date):
    with closing(get_db()) as conn:
        row = conn.execute("""
            SELECT push_state, push_retry_count, push_next_retry_at,
                   push_task_id, push_error, push_sent_at
            FROM user_diary
            WHERE user_id = ? AND diary_date = ?
        """, (user_id, diary_date)).fetchone()
    return dict(row) if row else None

def fail_diary_job(diary_id, error, max_retries=3):
    now = _now_app_timezone()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        row = conn.execute("SELECT retry_count FROM user_diary WHERE id = ?", (diary_id,)).fetchone()
        retry_count = (int(row["retry_count"]) if row else 0) + 1
        if retry_count >= max_retries:
            state = "SKIPPED"
            next_retry_at = None
        else:
            state = "GENERATING"
            delay_minutes = min(60, 5 * (2 ** (retry_count - 1)))
            next_retry_at = (now + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            UPDATE user_diary
            SET state = ?, error = ?, retry_count = ?, next_retry_at = ?,
                generation_started_at = NULL, updated_at = ?
            WHERE id = ?
        """, (state, str(error)[:4000], retry_count, next_retry_at, now_str, diary_id))
        conn.commit()
        return state

def _diary_date_from_ts_ms(user_id, ts_ms):
    ts_ms = int(ts_ms)
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT tz_iana, tz_offset_min FROM user WHERE id = ?",
            (user_id,),
        ).fetchone()

    tzinfo = _timezone_from_user_row(row)
    return datetime.fromtimestamp(ts_ms / 1000.0, tzinfo).strftime("%Y-%m-%d")

def _timezone_from_user_row(row):
    tz_iana = row["tz_iana"] if row else ""
    if tz_iana and ZoneInfo:
        try:
            return ZoneInfo(tz_iana)
        except Exception:
            pass

    try:
        offset_min = int(row["tz_offset_min"]) if row else 480
    except (TypeError, ValueError):
        offset_min = 480
    return timezone(timedelta(minutes=offset_min))

def _loads_json_array(value):
    try:
        data = json.loads(value or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str)]

def _datetime_str_to_ms(value):
    if not value:
        return 0
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(dt.replace(tzinfo=APP_TIMEZONE).timestamp() * 1000)

def create_invite_code(code, expire_at_ms):
    user_group = -1
    if len(code) == 6:
        user_group = 0 
    elif len(code) == 5:
        user_group = 1 
    elif len(code) == 32 or len(code) == 36:
        user_group = 2 
        
    expire_at_str = datetime.fromtimestamp(expire_at_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    now_str = _now_app_timezone_str()
    
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invite_code (invite_code, expire_at, user_group, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (code, expire_at_str, user_group, now_str, now_str))
        conn.commit()

def list_invite_codes():
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invite_code ORDER BY created_at DESC")
        rows = cursor.fetchall()
        
        result = []
        for row in rows:
            expire_ms = int(datetime.strptime(row['expire_at'], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            created_ms = int(datetime.strptime(row['created_at'], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            result.append({
                "inviteCode": row['invite_code'],
                "expireAt": expire_ms,
                "createdAt": created_ms
            })
        return result

def log_behaviors(messages):
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        for msg in messages:
            user_id = msg.get('userId', '-1')
            event_name = msg.get('eventName', '')
            properties = json.dumps(msg.get('properties', {}), ensure_ascii=False)
            timestamp_ms = msg.get('timestamp', int(time.time() * 1000))
            dt_str = datetime.fromtimestamp(timestamp_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            
            actual_user_id = -1
            if isinstance(user_id, str) and user_id.startswith('+'):
                cursor.execute("SELECT id FROM user WHERE phone_number = ?", (user_id,))
                u = cursor.fetchone()
                if u:
                    actual_user_id = u['id']
            else:
                try:
                    actual_user_id = int(user_id)
                except:
                    pass
                    
            cursor.execute("""
                INSERT INTO user_behavior_log (user_id, event_name, properties, timestamp)
                VALUES (?, ?, ?, ?)
            """, (actual_user_id, event_name, properties, dt_str))
            
        conn.commit()
