import sqlite3
import os
import time
import json
import uuid
import re
from datetime import datetime
from contextlib import closing

# Store DB in workspace data dir
DB_DIR = os.path.expanduser('~/cow/data')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'soul.db')

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
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
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

        conn.commit()

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
            cursor.execute("""
                UPDATE user SET invite_code = ?, user_group = ?, auth_token = ?, updated_at = ?
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

def get_user_by_token(token):
    if not token:
        return None
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user WHERE auth_token = ?", (token,))
        user = cursor.fetchone()
        return dict(user) if user else None

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
