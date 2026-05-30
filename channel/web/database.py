import sqlite3
import os
import time
import json
import uuid
import re
from datetime import datetime, timedelta
from contextlib import closing

# Store DB in workspace data dir
DB_DIR = os.path.expanduser('~/cow/data')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'soul.db')
DEFAULT_USER_NICKNAME = "宝宝"
DEFAULT_BEAR_NICKNAME = "满仓"
MAX_CHAT_MESSAGES_PER_USER = 1000

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

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
            "deletion_requested_at": "DATETIME DEFAULT NULL",
            "deletion_deadline": "DATETIME DEFAULT NULL",
            "deleted_at": "DATETIME DEFAULT NULL",
        })
        
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
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_chat_message (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id BIGINT NOT NULL,
          session_id VARCHAR(255) NOT NULL DEFAULT '',
          role VARCHAR(32) NOT NULL,
          content TEXT NOT NULL,
          message_type VARCHAR(32) NOT NULL DEFAULT 'text',
          source VARCHAR(32) NOT NULL DEFAULT 'APP',
          request_id VARCHAR(64) NOT NULL DEFAULT '',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_chat_message_user_id_id ON user_chat_message(user_id, id)")
        
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
        if datetime.now() > expire_at:
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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nickname, used_nickname FROM user WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
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
        return True

def request_account_deletion(user_id):
    now = datetime.now()
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
        conn.commit()
    return deadline

def cleanup_expired_deleted_accounts(workspace_root=None, now=None):
    now = now or datetime.now()
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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("""
            INSERT INTO user_feedback (user_id, feedback_type, description, images, contact, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, feedback_type, description, json.dumps(images or [], ensure_ascii=False), contact, now_str))
        conn.commit()

def append_chat_message(user_id, session_id, role, content, message_type="text", source="APP", request_id=""):
    if not user_id or user_id == -1 or not content:
        return None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_chat_message
            (user_id, session_id, role, content, message_type, source, request_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, session_id or "", role, content, message_type, source or "APP", request_id or "", now_str))
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
            SELECT id, role, content, message_type, source, request_id, created_at
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
                    "messageType": msg["message_type"],
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

def create_invite_code(code, expire_at_ms):
    user_group = -1
    if len(code) == 6:
        user_group = 0 
    elif len(code) == 5:
        user_group = 1 
    elif len(code) == 32 or len(code) == 36:
        user_group = 2 
        
    expire_at_str = datetime.fromtimestamp(expire_at_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
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
