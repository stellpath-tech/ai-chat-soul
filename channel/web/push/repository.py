import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import channel.web.database as core_db


APP_TIMEZONE = timezone(timedelta(hours=8))
PUSH_TYPES = {"greeting", "weather", "diary", "recall"}
PUSH_TASK_STATES = {"PENDING", "SENT", "CANCELLED", "FAILED"}


def _now_app_timezone():
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def _format_datetime(value):
    if isinstance(value, str):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(APP_TIMEZONE).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def update_user_activity(
    user_id,
    timezone_profile,
    notification_enabled=None,
    location=None,
    now=None,
):
    now_str = _format_datetime(now or _now_app_timezone())
    assignments = [
        "last_active_at = ?",
        "tz_iana = ?",
        "tz_offset_min = ?",
        "tz_updated_at = ?",
        "updated_at = ?",
    ]
    params = [
        now_str,
        timezone_profile["tz_iana"],
        int(timezone_profile["tz_offset_min"]),
        now_str,
        now_str,
    ]
    if notification_enabled is not None:
        assignments.append("notification_enabled = ?")
        params.append(1 if notification_enabled else 0)
    if location is not None:
        assignments.extend(["last_lat = ?", "last_lon = ?", "location_updated_at = ?"])
        params.extend([float(location["lat"]), float(location["lon"]), now_str])
    params.append(int(user_id))
    with closing(core_db.get_db()) as conn:
        cursor = conn.execute(
            "UPDATE user SET {} WHERE id = ? AND account_status = 'active'".format(
                ", ".join(assignments)
            ),
            params,
        )
        conn.commit()
        return cursor.rowcount == 1


def get_user(user_id):
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT u.id, u.tz_iana, u.tz_offset_min, u.last_active_at,
                   u.last_lat, u.last_lon, u.location_updated_at,
                   u.notification_enabled, u.account_status,
                   p.push_token, p.platform, p.enabled AS push_device_enabled
            FROM user u
            LEFT JOIN user_push_device p ON p.user_id = u.id
            WHERE u.id = ?
        """, (int(user_id),)).fetchone()
    return dict(row) if row else None


def list_users():
    with closing(core_db.get_db()) as conn:
        rows = conn.execute("""
            SELECT u.id, u.tz_iana, u.tz_offset_min, u.last_active_at,
                   u.last_lat, u.last_lon, u.location_updated_at,
                   u.notification_enabled, u.account_status,
                   p.push_token, p.platform, p.enabled AS push_device_enabled
            FROM user u
            LEFT JOIN user_push_device p ON p.user_id = u.id
            WHERE u.account_status = 'active'
            ORDER BY u.id
        """).fetchall()
    return [dict(row) for row in rows]


def mark_diary_viewed(user_id, ts_ms, now=None):
    diary_date = core_db._diary_date_from_ts_ms(user_id, ts_ms)
    now_value = now or _now_app_timezone()
    now_str = _format_datetime(now_value)
    with closing(core_db.get_db()) as conn:
        user = conn.execute("""
            SELECT tz_iana, tz_offset_min
            FROM user WHERE id = ? AND account_status = 'active'
        """, (int(user_id),)).fetchone()
        if not user:
            return False
        aware_now = (
            now_value.astimezone(APP_TIMEZONE)
            if now_value.tzinfo is not None
            else now_value.replace(tzinfo=APP_TIMEZONE)
        )
        current_local_date = aware_now.astimezone(
            core_db._timezone_from_user_row(user)
        ).strftime("%Y-%m-%d")
        if diary_date != current_local_date:
            return False
        conn.execute("""
            INSERT INTO user_diary_view
            (user_id, diary_date, viewed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, diary_date) DO UPDATE SET
                updated_at = excluded.updated_at
        """, (int(user_id), diary_date, now_str, now_str, now_str))
        conn.commit()
        return True


def get_diary_candidate(user_id, diary_date):
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT d.id AS diary_id, d.user_id, d.diary_date, d.state,
                   d.generated_at, v.viewed_at, d.push_state,
                   u.tz_iana, u.tz_offset_min, u.last_active_at,
                   u.notification_enabled, u.account_status,
                   p.push_token, p.platform, p.enabled AS push_device_enabled
            FROM user_diary d
            JOIN user u ON u.id = d.user_id
            LEFT JOIN user_diary_view v
              ON v.user_id = d.user_id AND v.diary_date = d.diary_date
            LEFT JOIN user_push_device p ON p.user_id = d.user_id
            WHERE d.user_id = ? AND d.diary_date = ?
        """, (int(user_id), str(diary_date))).fetchone()
    return dict(row) if row else None


def list_contents(
    push_type=None,
    delivery_scene=None,
    enabled=None,
    keyword=None,
    limit=30,
    offset=0,
):
    safe_limit = max(1, min(int(limit or 30), 100))
    safe_offset = max(0, int(offset or 0))
    where = []
    params = []
    if push_type:
        where.append("push_type = ?")
        params.append(str(push_type))
    if delivery_scene:
        where.append("delivery_scene = ?")
        params.append(str(delivery_scene))
    if enabled is not None:
        where.append("enabled = ?")
        params.append(1 if enabled else 0)
    if keyword:
        pattern = "%{}%".format(str(keyword).strip())
        where.append("(content_no LIKE ? OR title LIKE ? OR body LIKE ?)")
        params.extend([pattern, pattern, pattern])
    clause = " WHERE " + " AND ".join(where) if where else ""
    with closing(core_db.get_db()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM push_content" + clause,
            params,
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM push_content{} ORDER BY id DESC LIMIT ? OFFSET ?".format(clause),
            params + [safe_limit, safe_offset],
        ).fetchall()
        content_ids = [row["id"] for row in rows]
        images_by_content = {content_id: [] for content_id in content_ids}
        if content_ids:
            placeholders = ",".join("?" for _ in content_ids)
            image_rows = conn.execute("""
                SELECT id, content_id, object_key
                FROM push_content_image
                WHERE enabled = 1 AND content_id IN ({})
                ORDER BY id
            """.format(placeholders), content_ids).fetchall()
            for image in image_rows:
                images_by_content[image["content_id"]].append({
                    "imageId": image["id"],
                    "objectKey": image["object_key"],
                })
    return {
        "items": [_format_content(row, images_by_content.get(row["id"], [])) for row in rows],
        "total": int(total),
    }


def _format_content(row, images):
    return {
        "id": row["id"],
        "contentNo": row["content_no"],
        "pushType": row["push_type"],
        "deliveryScene": row["delivery_scene"],
        "title": row["title"],
        "body": row["body"],
        "enabled": bool(row["enabled"]),
        "images": images,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def create_content(content_no, push_type, delivery_scene, title, body, enabled=True):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        cursor = conn.execute("""
            INSERT INTO push_content
            (content_no, push_type, delivery_scene, title, body, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            content_no, push_type, delivery_scene, title, body,
            1 if enabled else 0, now_str, now_str,
        ))
        conn.commit()
        return cursor.lastrowid


def update_content(content_id, content_no, push_type, delivery_scene, title, body, enabled):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        cursor = conn.execute("""
            UPDATE push_content
            SET content_no = ?, push_type = ?, delivery_scene = ?, title = ?,
                body = ?, enabled = ?, updated_at = ?
            WHERE id = ?
        """, (
            content_no, push_type, delivery_scene, title, body,
            1 if enabled else 0, now_str, int(content_id),
        ))
        conn.commit()
        return cursor.rowcount == 1


def disable_content(content_id):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        cursor = conn.execute(
            "UPDATE push_content SET enabled = 0, updated_at = ? WHERE id = ?",
            (now_str, int(content_id)),
        )
        if cursor.rowcount:
            conn.execute("""
                UPDATE user_push_task
                SET state = 'CANCELLED', error = 'push content disabled',
                    started_at = NULL, next_retry_at = NULL, updated_at = ?
                WHERE content_id = ? AND state = 'PENDING'
            """, (now_str, int(content_id)))
        conn.commit()
        return cursor.rowcount == 1


def get_content(content_id):
    with closing(core_db.get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM push_content WHERE id = ?",
            (int(content_id),),
        ).fetchone()
    return dict(row) if row else None


def add_content_image(content_id, object_key, sha256, size_bytes):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        content = conn.execute(
            "SELECT id FROM push_content WHERE id = ?",
            (int(content_id),),
        ).fetchone()
        if not content:
            return None
        cursor = conn.execute("""
            INSERT INTO push_content_image
            (content_id, object_key, sha256, size_bytes, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
        """, (
            int(content_id), object_key, sha256 or "",
            int(size_bytes or 0), now_str, now_str,
        ))
        conn.commit()
        return {
            "imageId": cursor.lastrowid,
            "objectKey": object_key,
        }


def disable_content_image(content_id, image_id):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        cursor = conn.execute("""
            UPDATE push_content_image
            SET enabled = 0, updated_at = ?
            WHERE id = ? AND content_id = ? AND enabled = 1
        """, (now_str, int(image_id), int(content_id)))
        conn.commit()
        return cursor.rowcount == 1


def select_content(user_id, push_type, delivery_scenes, now=None):
    scenes = [str(scene) for scene in delivery_scenes if str(scene)]
    if not scenes:
        return None
    since = (now or _now_app_timezone()) - timedelta(days=14)
    placeholders = ",".join("?" for _ in scenes)
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT c.*
            FROM push_content c
            WHERE c.enabled = 1
              AND c.push_type = ?
              AND c.delivery_scene IN ({})
              AND NOT EXISTS (
                SELECT 1 FROM user_push_task t
                WHERE t.user_id = ? AND t.content_id = c.id
                  AND t.state = 'SENT' AND t.sent_at >= ?
              )
            ORDER BY RANDOM()
            LIMIT 1
        """.format(placeholders), [
            str(push_type), *scenes, int(user_id), _format_datetime(since),
        ]).fetchone()
        if not row:
            return None
        image = conn.execute("""
            SELECT id, object_key
            FROM push_content_image
            WHERE content_id = ? AND enabled = 1
            ORDER BY RANDOM()
            LIMIT 1
        """, (row["id"],)).fetchone()
    result = dict(row)
    result["image_id"] = image["id"] if image else 0
    result["image_object_key"] = image["object_key"] if image else ""
    return result


def create_task(
    user_id,
    push_type,
    local_date,
    business_key,
    content_id,
    scheduled_at,
    card,
    source_id=0,
    state="PENDING",
    error="",
):
    if push_type not in PUSH_TYPES:
        raise ValueError("invalid push type")
    if state not in PUSH_TASK_STATES:
        raise ValueError("invalid push task state")
    now_str = _format_datetime(_now_app_timezone())
    push_id = "psh_" + uuid.uuid4().hex
    card_value = dict(card or {})
    card_value["pushId"] = push_id
    with closing(core_db.get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("""
            SELECT * FROM user_push_task
            WHERE user_id = ? AND push_type = ? AND business_key = ?
        """, (int(user_id), push_type, business_key)).fetchone()
        if existing:
            conn.commit()
            return dict(existing)
        cursor = conn.execute("""
            INSERT INTO user_push_task
            (push_id, user_id, push_type, local_date, business_key, content_id,
             source_id, scheduled_at, state, card_json, error,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            push_id, int(user_id), push_type, str(local_date), str(business_key),
            int(content_id or 0), int(source_id or 0), _format_datetime(scheduled_at),
            state, json.dumps(card_value, ensure_ascii=False, separators=(",", ":")),
            str(error or "")[:4000], now_str, now_str,
        ))
        task_id = cursor.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM user_push_task WHERE id = ?",
            (task_id,),
        ).fetchone()
        return dict(row)


def get_task(user_id, push_type, business_key):
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT * FROM user_push_task
            WHERE user_id = ? AND push_type = ? AND business_key = ?
        """, (int(user_id), str(push_type), str(business_key))).fetchone()
    return dict(row) if row else None


def get_task_by_push_id(user_id, push_id):
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT t.*, c.content_no
            FROM user_push_task t
            LEFT JOIN push_content c ON c.id = t.content_id
            WHERE t.user_id = ? AND t.push_id = ?
        """, (int(user_id), str(push_id))).fetchone()
    return dict(row) if row else None


def has_greeting_plan(user_id, local_date):
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT 1 FROM user_push_task
            WHERE user_id = ? AND push_type = 'greeting' AND local_date = ?
            LIMIT 1
        """, (int(user_id), str(local_date))).fetchone()
    return row is not None


def has_weather_task_for_date(user_id, local_date):
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT 1 FROM user_push_task
            WHERE user_id = ? AND push_type = 'weather' AND local_date = ?
              AND state IN ('PENDING', 'SENT')
            LIMIT 1
        """, (int(user_id), str(local_date))).fetchone()
    return row is not None


def claim_task(task_id=None, push_types=None, now=None, stale_after_minutes=10):
    now_value = now or _now_app_timezone()
    now_str = _format_datetime(now_value)
    stale_str = _format_datetime(now_value - timedelta(minutes=stale_after_minutes))
    where = [
        "state = 'PENDING'",
        "scheduled_at <= ?",
        "(next_retry_at IS NULL OR next_retry_at <= ?)",
        "(started_at IS NULL OR started_at <= ?)",
    ]
    params = [now_str, now_str, stale_str]
    if task_id is not None:
        where.append("id = ?")
        params.append(int(task_id))
    if push_types:
        values = [str(value) for value in push_types]
        where.append("push_type IN ({})".format(",".join("?" for _ in values)))
        params.extend(values)
    with closing(core_db.get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT * FROM user_push_task
            WHERE {}
            ORDER BY CASE push_type WHEN 'weather' THEN 0 WHEN 'recall' THEN 1 ELSE 2 END,
                     scheduled_at, id
            LIMIT 1
        """.format(" AND ".join(where)), params).fetchone()
        if not row:
            conn.rollback()
            return None
        cursor = conn.execute("""
            UPDATE user_push_task
            SET started_at = ?, updated_at = ?
            WHERE id = ? AND state = 'PENDING'
              AND (started_at IS NULL OR started_at <= ?)
        """, (now_str, now_str, row["id"], stale_str))
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        result = dict(row)
        result["started_at"] = now_str
        return result


def update_task_card(task_id, card, chat_message_id=0):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        cursor = conn.execute("""
            UPDATE user_push_task
            SET card_json = ?, chat_message_id = ?, updated_at = ?
            WHERE id = ? AND state = 'PENDING'
        """, (
            json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            int(chat_message_id or 0), now_str, int(task_id),
        ))
        conn.commit()
        return cursor.rowcount == 1


def mark_task_sent(task_id, provider_task_id, now=None):
    now_str = _format_datetime(now or _now_app_timezone())
    with closing(core_db.get_db()) as conn:
        conn.execute("""
            UPDATE user_push_task
            SET state = 'SENT', provider_task_id = ?, error = '', sent_at = ?,
                next_retry_at = NULL, started_at = NULL, updated_at = ?
            WHERE id = ?
        """, (str(provider_task_id or ""), now_str, now_str, int(task_id)))
        conn.commit()


def cancel_task(task_id, reason):
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        conn.execute("""
            UPDATE user_push_task
            SET state = 'CANCELLED', error = ?, next_retry_at = NULL,
                started_at = NULL, updated_at = ?
            WHERE id = ? AND state = 'PENDING'
        """, (str(reason or "")[:4000], now_str, int(task_id)))
        conn.commit()


def fail_task(task_id, error, max_retries=3, now=None):
    now_value = now or _now_app_timezone()
    now_str = _format_datetime(now_value)
    with closing(core_db.get_db()) as conn:
        row = conn.execute(
            "SELECT retry_count FROM user_push_task WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        retry_count = (int(row["retry_count"]) if row else 0) + 1
        if retry_count >= max(1, int(max_retries)):
            state = "FAILED"
            next_retry_at = None
        else:
            state = "PENDING"
            next_retry_at = _format_datetime(
                now_value + timedelta(minutes=min(60, 2 ** (retry_count - 1)))
            )
        conn.execute("""
            UPDATE user_push_task
            SET state = ?, retry_count = ?, next_retry_at = ?, started_at = NULL,
                error = ?, updated_at = ?
            WHERE id = ?
        """, (
            state, retry_count, next_retry_at, str(error or "")[:4000],
            now_str, int(task_id),
        ))
        conn.commit()
        return state, retry_count, next_retry_at


def cancel_next_greeting(user_id, sent_at, minutes=30):
    start_str = _format_datetime(sent_at)
    end_str = _format_datetime(sent_at + timedelta(minutes=int(minutes)))
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT id FROM user_push_task
            WHERE user_id = ? AND push_type = 'greeting' AND state = 'PENDING'
              AND scheduled_at >= ? AND scheduled_at <= ?
            ORDER BY scheduled_at, id
            LIMIT 1
        """, (int(user_id), start_str, end_str)).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute("""
            UPDATE user_push_task
            SET state = 'CANCELLED', error = 'cancelled by weather push',
                next_retry_at = NULL, started_at = NULL, updated_at = ?
            WHERE id = ? AND state = 'PENDING'
        """, (now_str, row["id"]))
        conn.commit()
        return row["id"]


def diary_sent_for_quiet_window(user_id, local_dates):
    dates = [str(value) for value in local_dates if value]
    if not dates:
        return False
    placeholders = ",".join("?" for _ in dates)
    with closing(core_db.get_db()) as conn:
        row = conn.execute("""
            SELECT 1 FROM user_push_task
            WHERE user_id = ? AND push_type = 'diary' AND state = 'SENT'
              AND local_date IN ({})
            LIMIT 1
        """.format(placeholders), [int(user_id), *dates]).fetchone()
    return row is not None


def ensure_greeting_chat_message(user_id, push_id, content):
    request_id = "push:" + str(push_id)
    now_str = _format_datetime(_now_app_timezone())
    with closing(core_db.get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("""
            SELECT id FROM user_chat_message
            WHERE user_id = ? AND request_id = ? AND role = 'assistant'
            LIMIT 1
        """, (int(user_id), request_id)).fetchone()
        if existing:
            conn.commit()
            return existing["id"]
        cursor = conn.execute("""
            INSERT INTO user_chat_message
            (user_id, session_id, role, content, image_url, message_type,
             weather_text, diary_title, diary_summary, source, request_id, created_at)
            VALUES (?, ?, 'assistant', ?, '', 'text', '', '', '', 'APP', ?, ?)
        """, (int(user_id), "user_{}".format(user_id), str(content), request_id, now_str))
        conn.execute("""
            DELETE FROM user_chat_message
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM user_chat_message
                WHERE user_id = ? ORDER BY id DESC LIMIT ?
              )
        """, (
            int(user_id), int(user_id), core_db.MAX_CHAT_MESSAGES_PER_USER,
        ))
        conn.commit()
        return cursor.lastrowid
