import threading
import time
from datetime import datetime, timedelta, timezone

from common.log import logger
from config import conf
import channel.web.database as db
from channel.web.diary.service import _user_timezone, generate_user_diary


_START_LOCK = threading.Lock()
_STARTED = False


def start_diary_worker():
    global _STARTED
    if not bool(conf().get("diary_worker_enabled", False)):
        logger.info("[Diary] worker disabled")
        return False
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
    threading.Thread(target=_worker_loop, daemon=True, name="diary-worker").start()
    logger.info("[Diary] worker started")
    return True


def run_scheduled_diaries_once(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    generation_hour = max(0, min(23, int(conf().get("diary_generation_hour", 1))))
    processed = []
    for user in db.list_active_users_for_diary():
        local_now = now_utc.astimezone(_user_timezone(user))
        if local_now.hour < generation_hour:
            continue
        diary_date = (local_now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        job = db.create_or_reset_diary_job(user["id"], diary_date, mode="auto", force=False)
        if job.get("state") != "GENERATING":
            continue
        result = generate_user_diary(user, diary_date)
        if result:
            processed.append({"userId": user["id"], "date": diary_date, "state": result.get("state")})
    return processed


def _worker_loop():
    interval = max(30, int(conf().get("diary_worker_poll_seconds", 300)))
    while True:
        try:
            run_scheduled_diaries_once()
        except Exception:
            logger.exception("[Diary] worker iteration failed")
        time.sleep(interval)
