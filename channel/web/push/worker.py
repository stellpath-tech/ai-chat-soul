import threading
import time
from datetime import datetime, timezone

from common.log import logger
from config import conf
from channel.web.push.planning import (
    generate_greeting_plans,
    poll_weather_alerts,
    schedule_recalls,
)
from channel.web.push.service import deliver_due_proactive_notifications


_START_LOCK = threading.Lock()
_STARTED = False
_WEATHER_BUCKET_LOCK = threading.Lock()
_LAST_WEATHER_BUCKET = None


def start_proactive_push_worker():
    global _STARTED
    if not bool(conf().get("proactive_push_worker_enabled", False)):
        logger.info("[ProactivePush] worker disabled")
        return False
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
    threading.Thread(
        target=_worker_loop,
        daemon=True,
        name="proactive-push-worker",
    ).start()
    logger.info("[ProactivePush] worker started")
    return True


def run_proactive_push_iteration(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    greeting_tasks = generate_greeting_plans(now_utc=now_utc)
    weather_tasks = (
        poll_weather_alerts(now_utc=now_utc)
        if _claim_weather_poll_bucket(now_utc)
        else []
    )
    recall_tasks = schedule_recalls(now_utc=now_utc)
    deliveries = deliver_due_proactive_notifications(
        limit=max(1, min(
            int(conf().get("proactive_push_retry_limit", 100)), 1000
        )),
        now=now_utc,
    )
    return {
        "greetingTasks": len(greeting_tasks),
        "weatherTasks": len(weather_tasks),
        "recallTasks": len(recall_tasks),
        "deliveries": deliveries,
    }


def _claim_weather_poll_bucket(now_utc):
    global _LAST_WEATHER_BUCKET
    bucket = int(now_utc.timestamp() // (30 * 60))
    with _WEATHER_BUCKET_LOCK:
        if _LAST_WEATHER_BUCKET == bucket:
            return False
        _LAST_WEATHER_BUCKET = bucket
        return True


def _worker_loop():
    interval = max(30, int(conf().get("proactive_push_worker_poll_seconds", 60)))
    while True:
        try:
            run_proactive_push_iteration()
        except Exception:
            logger.exception("[ProactivePush] worker iteration failed")
        time.sleep(interval)
