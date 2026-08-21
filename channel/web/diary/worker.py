import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from common.log import logger
from config import conf
import channel.web.database as db
from channel.web.diary.service import _user_timezone, generate_user_diary
from channel.web.push.service import retry_pending_diary_notifications


_START_LOCK = threading.Lock()
_STARTED = False
_DATE_RETRY_LOCK = threading.Lock()
_ACTIVE_DATE_RETRIES = set()


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
    generation_day_offset = max(0, min(1, int(conf().get("diary_generation_day_offset", 1))))
    scheduled = []
    for user in db.list_active_users_for_diary():
        local_now = now_utc.astimezone(_user_timezone(user))
        if local_now.hour < generation_hour:
            continue
        diary_date = (local_now.date() - timedelta(days=generation_day_offset)).strftime("%Y-%m-%d")
        job = db.create_or_reset_diary_job(user["id"], diary_date, mode="auto", force=False)
        if job.get("state") != "GENERATING":
            continue
        scheduled.append((user, diary_date))

    if not scheduled:
        return []

    configured_workers = max(1, min(20, int(conf().get("diary_generation_workers", 5))))
    worker_count = min(configured_workers, len(scheduled))
    logger.info("[Diary] scheduled batch starting jobs=%s workers=%s", len(scheduled), worker_count)
    processed = _generate_diary_jobs(scheduled, worker_count)
    logger.info("[Diary] scheduled batch complete processed=%s", len(processed))
    return processed


def trigger_diary_date_retry(diary_date):
    diary_date = datetime.strptime(str(diary_date), "%Y-%m-%d").strftime("%Y-%m-%d")
    with _DATE_RETRY_LOCK:
        if diary_date in _ACTIVE_DATE_RETRIES:
            return {"targetDate": diary_date, "started": False, "reason": "already_running"}
        _ACTIVE_DATE_RETRIES.add(diary_date)
    threading.Thread(
        target=_run_diary_date_retry,
        args=(diary_date,),
        daemon=True,
        name="diary-date-retry-{}".format(diary_date),
    ).start()
    return {"targetDate": diary_date, "started": True}


def retry_diaries_for_date(diary_date):
    diary_date = datetime.strptime(str(diary_date), "%Y-%m-%d").strftime("%Y-%m-%d")
    retry_done_without_image = bool(conf().get("diary_image_enabled", False))
    scheduled = []
    action_counts = {}
    users = db.list_active_users_for_diary()
    for user in users:
        job = db.prepare_diary_job_for_date_retry(
            user["id"],
            diary_date,
            retry_done_without_image=retry_done_without_image,
        )
        action = str(job.get("action") or "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
        if job.get("state") == "GENERATING" and not action.startswith("skipped_"):
            scheduled.append((user, diary_date))

    configured_workers = max(1, min(20, int(conf().get("diary_generation_workers", 5))))
    worker_count = min(configured_workers, len(scheduled)) if scheduled else 0
    logger.info(
        "[Diary] date retry batch starting date=%s checked=%s jobs=%s workers=%s actions=%s",
        diary_date, len(users), len(scheduled), worker_count, action_counts,
    )
    processed = _generate_diary_jobs(
        scheduled, worker_count, deliver_notification=False,
    ) if scheduled else []
    result = {
        "targetDate": diary_date,
        "checked": len(users),
        "scheduled": len(scheduled),
        "processed": len(processed),
        "actions": action_counts,
    }
    logger.info("[Diary] date retry batch complete result=%s", result)
    return result


def _run_diary_date_retry(diary_date):
    try:
        retry_diaries_for_date(diary_date)
    except Exception:
        logger.exception("[Diary] date retry batch failed date=%s", diary_date)
    finally:
        with _DATE_RETRY_LOCK:
            _ACTIVE_DATE_RETRIES.discard(diary_date)


def _generate_diary_jobs(scheduled, worker_count, deliver_notification=True):
    processed = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="diary-generate") as executor:
        if deliver_notification:
            futures = {
                executor.submit(generate_user_diary, user, diary_date): (user, diary_date)
                for user, diary_date in scheduled
            }
        else:
            futures = {
                executor.submit(generate_user_diary, user, diary_date, False): (user, diary_date)
                for user, diary_date in scheduled
            }
        for future in as_completed(futures):
            user, diary_date = futures[future]
            try:
                result = future.result()
            except Exception:
                logger.exception(
                    "[Diary] scheduled generation failed user_id=%s date=%s",
                    user["id"], diary_date,
                )
                continue
            if result:
                processed.append({"userId": user["id"], "date": diary_date, "state": result.get("state")})
    return processed


def _worker_loop():
    interval = max(30, int(conf().get("diary_worker_poll_seconds", 300)))
    while True:
        try:
            run_scheduled_diaries_once()
            retry_pending_diary_notifications()
        except Exception:
            logger.exception("[Diary] worker iteration failed")
        time.sleep(interval)
