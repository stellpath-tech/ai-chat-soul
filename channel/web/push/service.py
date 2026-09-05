import base64
import hashlib
import hmac
import json
import secrets
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from common.log import logger
from config import conf
import channel.web.database as db
from channel.web.push.contracts import (
    DiaryReadyPushNotification,
    ProactivePushNotification,
    PushDeviceRequestError,
    PushTestNotification,
    PushTestRequestError,
    UserPushDeviceRegistration,
    UserPushDeviceUnregistration,
    _user_timezone,
)
from channel.web.push.conversation import conversation_activity
from channel.web.push import assets, repository


class PushTestDeviceNotRegisteredError(RuntimeError):
    pass


class PushTestDeliveryError(RuntimeError):
    pass


class _TencentPushError(RuntimeError):
    pass


CARD_CTA_BY_TYPE = {
    "greeting": ("和满仓聊聊", "open_chat"),
    "weather": ("查看天气", "open_weather"),
    "diary": ("看看今天的日记", "open_diary"),
    "recall": ("回来坐坐", "open_home"),
}


@dataclass(frozen=True)
class _TencentPushConfig:
    sdk_app_id: int
    administrator: str
    secret_key: str
    api_base: str
    timeout_seconds: int
    max_retries: int

    @classmethod
    def from_runtime(cls):
        try:
            sdk_app_id = int(conf().get("tencent_im_sdk_app_id", 0))
        except (TypeError, ValueError):
            sdk_app_id = 0
        administrator = str(
            conf().get("tencent_im_admin_user_id", "administrator") or ""
        ).strip()
        secret_key = str(conf().get("tencent_im_secret_key", "") or "").strip()
        api_base = str(
            conf().get("tencent_im_api_base", "https://console.tim.qq.com") or ""
        ).strip().rstrip("/")
        if sdk_app_id <= 0 or not administrator or not secret_key:
            raise _TencentPushError("Tencent IM push configuration is incomplete")
        if not api_base.startswith("https://"):
            raise _TencentPushError("Tencent IM API base must use HTTPS")
        return cls(
            sdk_app_id=sdk_app_id,
            administrator=administrator,
            secret_key=secret_key,
            api_base=api_base,
            timeout_seconds=max(
                1, int(conf().get("tencent_im_push_timeout_seconds", 10))
            ),
            max_retries=max(
                1, int(conf().get("tencent_im_push_max_retries", 3))
            ),
        )


class _TencentImUserSigSigner:
    VERSION = "2.0"

    @classmethod
    def generate(cls, sdk_app_id, user_id, secret_key, expire_seconds=604800, now=None):
        current_time = int(now if now is not None else time.time())
        expire_seconds = int(expire_seconds)
        sign_content = (
            "TLS.identifier:{user_id}\n"
            "TLS.sdkappid:{sdk_app_id}\n"
            "TLS.time:{current_time}\n"
            "TLS.expire:{expire_seconds}\n"
        ).format(
            user_id=user_id,
            sdk_app_id=sdk_app_id,
            current_time=current_time,
            expire_seconds=expire_seconds,
        )
        signature = base64.b64encode(hmac.new(
            secret_key.encode("utf-8"),
            sign_content.encode("utf-8"),
            hashlib.sha256,
        ).digest()).decode("ascii")
        ticket = json.dumps({
            "TLS.ver": cls.VERSION,
            "TLS.identifier": str(user_id),
            "TLS.sdkappid": int(sdk_app_id),
            "TLS.expire": expire_seconds,
            "TLS.time": current_time,
            "TLS.sig": signature,
        }, separators=(",", ":"))
        compressed = zlib.compress(ticket.encode("utf-8"))
        return base64.b64encode(compressed).decode("ascii").replace(
            "+", "*"
        ).replace("/", "-").replace("=", "_")


class _TencentPushClient:
    def __init__(self, push_config, http_post=None):
        self._push_config = push_config
        self._http_post = http_post or requests.post

    def send(self, push_token, notification):
        message_random = secrets.randbits(32)
        user_sig = _TencentImUserSigSigner.generate(
            self._push_config.sdk_app_id,
            self._push_config.administrator,
            self._push_config.secret_key,
        )
        response = self._http_post(
            self._push_config.api_base + "/v4/timpush/batch",
            params={
                "usersig": user_sig,
                "identifier": self._push_config.administrator,
                "sdkappid": self._push_config.sdk_app_id,
                "random": secrets.randbits(32),
                "contenttype": "json",
            },
            json=notification.to_tencent_request_body(
                self._push_config.administrator,
                push_token,
                message_random,
            ),
            timeout=(5, self._push_config.timeout_seconds),
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as error:
            raise _TencentPushError("Tencent IM returned invalid JSON") from error
        if int(result.get("ErrorCode", -1)) != 0:
            raise _TencentPushError(
                "Tencent IM push failed: code={} info={}".format(
                    result.get("ErrorCode"), result.get("ErrorInfo", "")
                )
            )
        return str(result.get("TaskId") or "")


def register_authenticated_user_push_device(user_id, request_body):
    registration = UserPushDeviceRegistration.from_request_body(request_body)
    db.register_user_push_device(
        int(user_id),
        **registration.as_database_fields()
    )


def unregister_authenticated_user_push_device(user_id, request_body):
    unregistration = UserPushDeviceUnregistration.from_request_body(request_body)
    db.unregister_user_push_device(int(user_id), unregistration.push_token)


def send_authenticated_user_push_test(user_id, request_body):
    notification = PushTestNotification.from_request_body(request_body)
    device = db.get_user_push_device(int(user_id))
    push_token = str((device or {}).get("push_token") or "").strip()
    if not device or int(device.get("enabled") or 0) != 1 or not push_token:
        raise PushTestDeviceNotRegisteredError("push device not registered")

    try:
        push_config = _TencentPushConfig.from_runtime()
        task_id = _TencentPushClient(push_config).send(push_token, notification)
    except Exception as error:
        logger.exception("[PushTest] delivery failed user=%s", user_id)
        raise PushTestDeliveryError("push delivery failed") from error

    logger.info("[PushTest] sent user=%s task=%s", user_id, task_id)
    return {"taskId": task_id}


def get_authenticated_user_push_card(user_id, push_id):
    task = repository.get_task_by_push_id(int(user_id), str(push_id).strip())
    if not task or task.get("state") != "SENT":
        return None
    try:
        card_snapshot = json.loads(task.get("card_json") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(card_snapshot, dict):
        return None
    push_type = str(task.get("push_type") or "")
    cta_label, cta_action = CARD_CTA_BY_TYPE[push_type]
    image_url = ""
    image_object_key = str(card_snapshot.get("imageObjectKey") or "").strip()
    if image_object_key:
        image_url = assets.create_image_read_url(image_object_key)
    elif card_snapshot.get("imageUrl"):
        image_url = str(card_snapshot["imageUrl"])

    card = {
        "pushId": task["push_id"],
        "type": push_type,
        "title": str(card_snapshot.get("title") or ""),
        "body": str(card_snapshot.get("body") or ""),
        "imageUrl": image_url,
        "imageVersion": int(card_snapshot.get("imageVersion") or 0),
        "cta": {
            "label": cta_label,
            "action": cta_action,
            "params": {},
        },
        "greeting": None,
        "weather": None,
        "diary": None,
        "recall": None,
    }
    if push_type == "greeting":
        chat_message_id = int(
            task.get("chat_message_id") or card_snapshot.get("messageId") or 0
        )
        card["cta"]["params"] = {"chatMessageId": chat_message_id}
        card["greeting"] = {
            "contentNo": str(task.get("content_no") or ""),
            "chatMessageId": chat_message_id,
        }
    elif push_type == "weather":
        card["weather"] = {
            "effectiveTime": card_snapshot.get("effectiveTime"),
            "onsetTime": card_snapshot.get("onsetTime"),
            "expireTime": card_snapshot.get("expireTime"),
            "headline": card_snapshot.get("headline"),
            "description": card_snapshot.get("description"),
            "criteria": card_snapshot.get("criteria"),
            "responseTypes": card_snapshot.get("responseTypes") or [],
            "instruction": card_snapshot.get("instruction"),
        }
    elif push_type == "diary":
        diary_date = str(
            card_snapshot.get("diaryDate") or task.get("local_date") or ""
        )
        diary_timestamp = int(card_snapshot.get("ts") or 0)
        card["cta"]["params"] = {
            "diaryDate": diary_date,
            "ts": diary_timestamp,
        }
        card["diary"] = {
            "diaryDate": diary_date,
            "ts": diary_timestamp,
        }
    elif push_type == "recall":
        inactive_days = int(card_snapshot.get("inactiveDays") or 0)
        card["recall"] = {"inactiveDays": inactive_days}
    return card


def deliver_due_proactive_notifications(limit=100, now=None):
    results = []
    safe_limit = max(1, min(int(limit or 100), 1000))
    for _ in range(safe_limit):
        task = repository.claim_task(
            push_types=("greeting", "weather", "recall"),
            now=now,
        )
        if not task:
            break
        try:
            results.append(_deliver_claimed_task(task, now=now))
        except Exception as error:
            logger.exception(
                "[ProactivePush] unexpected delivery failure push_id=%s",
                task.get("push_id"),
            )
            results.append(_record_task_failure(task, error, _app_now(now)))
    return results


def deliver_proactive_task(task_id, now=None):
    task = repository.claim_task(task_id=task_id, now=now)
    if not task:
        return None
    return _deliver_claimed_task(task, now=now)


def _deliver_claimed_task(task, now=None):
    now_value = _app_now(now)
    user = repository.get_user(task["user_id"])
    block_reason = _delivery_block_reason(task, user, now_value)
    if block_reason:
        repository.cancel_task(task["id"], block_reason)
        _sync_diary_cancel(task, block_reason)
        return _task_result(task, "CANCELLED", error=block_reason)

    try:
        card_snapshot = json.loads(task.get("card_json") or "{}")
        if task["push_type"] == "greeting":
            message_id = repository.ensure_greeting_chat_message(
                task["user_id"], task["push_id"], card_snapshot.get("body", "")
            )
            card_snapshot["messageId"] = message_id
            repository.update_task_card(task["id"], card_snapshot, message_id)
            task["card_json"] = json.dumps(
                card_snapshot, ensure_ascii=False, separators=(",", ":")
            )
    except Exception as error:
        return _record_task_failure(task, error, now_value)

    try:
        notification = ProactivePushNotification.from_task(task)
        push_config = _TencentPushConfig.from_runtime()
        provider_task_id = _TencentPushClient(push_config).send(
            str(user["push_token"]), notification
        )
        repository.mark_task_sent(task["id"], provider_task_id, now=now_value)
        if task["push_type"] == "weather":
            repository.cancel_next_greeting(task["user_id"], now_value)
        if task["push_type"] == "diary":
            db.mark_diary_push_sent(task["source_id"], provider_task_id)
        logger.info(
            "[ProactivePush] sent push_id=%s type=%s user=%s task=%s",
            task["push_id"], task["push_type"], task["user_id"], provider_task_id,
        )
        return _task_result(task, "SENT", provider_task_id=provider_task_id)
    except Exception as error:
        return _record_task_failure(task, error, now_value)


def _record_task_failure(task, error, now_value):
    try:
        max_retries = _TencentPushConfig.from_runtime().max_retries
    except Exception:
        max_retries = 3
    state, _, _ = repository.fail_task(
        task["id"], error, max_retries=max_retries, now=now_value
    )
    if task["push_type"] == "diary":
        db.mark_diary_push_failed(
            task["source_id"], error, max_retries=max_retries, now=now_value
        )
    logger.warning(
        "[ProactivePush] failed push_id=%s type=%s state=%s error=%s",
        task["push_id"], task["push_type"], state, error,
    )
    return _task_result(task, state, error=str(error))


def _delivery_block_reason(task, user, now_value):
    if not user or user.get("account_status") != "active":
        return "user is not active"
    if int(user.get("push_device_enabled") or 0) != 1 or not str(
        user.get("push_token") or ""
    ).strip():
        return "push device not registered"
    if int(user.get("notification_enabled") or 0) != 1:
        return "notification permission disabled"
    if int(task.get("content_id") or 0):
        content = repository.get_content(task["content_id"])
        if not content or int(content.get("enabled") or 0) != 1:
            return "push content disabled"

    push_type = task["push_type"]
    if push_type == "greeting" and not _active_within(
        user.get("last_active_at"), now_value, 72
    ):
        return "user inactive for 72 hours"
    if (
        push_type == "diary"
        and user.get("last_active_at")
        and not _active_within(user.get("last_active_at"), now_value, 72)
    ):
        return "user inactive for 72 hours"
    if push_type == "weather" and not _active_within(
        user.get("last_active_at"), now_value, 24
    ):
        return "user inactive for 24 hours"
    if push_type == "greeting" and conversation_activity.is_busy(user["id"]):
        return "user is in conversation"
    if push_type != "diary" and _in_diary_quiet_window(user, now_value):
        return "diary quiet window"
    if push_type == "recall" and not _recall_still_due(task, user, now_value):
        return "recall is no longer due"
    return ""


def _active_within(last_active_at, now_value, hours):
    parsed = _parse_datetime(last_active_at)
    if not parsed:
        return False
    return now_value - parsed <= timedelta(hours=int(hours))


def _recall_still_due(task, user, now_value):
    last_active = _parse_datetime(user.get("last_active_at"))
    if not last_active:
        return False
    timezone_info = _user_timezone(user)
    local_now = now_value.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(
        timezone_info
    )
    local_active = last_active.replace(
        tzinfo=timezone(timedelta(hours=8))
    ).astimezone(timezone_info)
    try:
        expected_days = int(str(task.get("business_key") or "").rsplit("|", 1)[1])
    except (IndexError, TypeError, ValueError):
        return False
    return (local_now.date() - local_active.date()).days == expected_days


def _in_diary_quiet_window(user, now_value):
    timezone_info = _user_timezone(user)
    local_now = now_value.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(
        timezone_info
    )
    if local_now.hour >= 23:
        dates = [local_now.strftime("%Y-%m-%d")]
    elif local_now.hour < 7:
        dates = [(local_now - timedelta(days=1)).strftime("%Y-%m-%d")]
    else:
        return False
    return repository.diary_sent_for_quiet_window(user["id"], dates)


def _app_now(value=None):
    if value is None:
        return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    if value.tzinfo is not None:
        return value.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return value


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return _app_now(value)
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _task_result(task, state, provider_task_id="", error=""):
    result = {
        "state": state,
        "pushId": task["push_id"],
        "userId": task["user_id"],
        "type": task["push_type"],
    }
    if provider_task_id:
        result["taskId"] = provider_task_id
    if error:
        result["error"] = error
    return result


def _sync_diary_cancel(task, reason):
    if task.get("push_type") == "diary" and int(task.get("source_id") or 0):
        db.mark_diary_push_skipped(task["source_id"], reason)


def deliver_generated_diary_notification(user_id, diary_date, now=None):
    try:
        delivery_record = db.claim_diary_push_notification(user_id, diary_date)
        if not delivery_record:
            return None
        return _prepare_and_deliver_diary_notification(delivery_record, now=now)
    except Exception as error:
        logger.exception(
            "[DiaryPush] delivery setup failed user=%s date=%s",
            user_id,
            diary_date,
        )
        return {"state": "ERROR", "error": str(error)}


def retry_pending_diary_notifications(limit=20, now=None):
    results = []
    for _ in range(max(1, min(int(limit), 100))):
        delivery_record = db.claim_diary_push_notification()
        if not delivery_record:
            break
        results.append(_prepare_and_deliver_diary_notification(delivery_record, now=now))
    return results


def _prepare_and_deliver_diary_notification(delivery_record, now=None):
    diary_id = delivery_record["diary_id"]
    user_id = delivery_record["user_id"]
    diary_date = delivery_record["diary_date"]
    try:
        candidate = repository.get_diary_candidate(user_id, diary_date)
        existing = repository.get_task(user_id, "diary", diary_date)
        reason = _diary_generation_block_reason(candidate)
        if reason:
            if existing and existing["state"] == "PENDING":
                repository.cancel_task(existing["id"], reason)
            db.mark_diary_push_skipped(diary_id, reason)
            return {
                "state": "SKIPPED",
                "userId": user_id,
                "date": diary_date,
                "error": reason,
            }

        if existing:
            if existing["state"] == "SENT":
                db.mark_diary_push_sent(diary_id, existing.get("provider_task_id") or "")
                return {
                    "state": "SENT",
                    "userId": user_id,
                    "date": diary_date,
                    "taskId": existing.get("provider_task_id") or "",
                }
            if existing["state"] in ("CANCELLED", "FAILED"):
                db.mark_diary_push_skipped(
                    diary_id, existing.get("error") or "diary push not deliverable"
                )
                return {
                    "state": "SKIPPED",
                    "userId": user_id,
                    "date": diary_date,
                    "error": existing.get("error") or "diary push not deliverable",
                }
            result = deliver_proactive_task(existing["id"], now=now)
            if result:
                result["date"] = diary_date
                return result
            return {"state": "PENDING", "userId": user_id, "date": diary_date}

        content = repository.select_content(
            user_id, "diary", ["DIARY_READY"], now=_app_now(now)
        )
        if not content:
            db.mark_diary_push_skipped(diary_id, "no available diary push content")
            return {
                "state": "SKIPPED",
                "userId": user_id,
                "date": diary_date,
                "error": "no available diary push content",
            }

        legacy = DiaryReadyPushNotification.from_delivery_record(candidate)
        card_snapshot = {
            "type": "diary",
            "title": content["title"],
            "body": content["body"],
            "action": "open_diary",
            "diaryDate": diary_date,
            "ts": legacy.diary_timestamp_ms,
        }
        if content.get("image_object_key"):
            card_snapshot["imageObjectKey"] = content["image_object_key"]
            card_snapshot["imageVersion"] = int(content.get("image_id") or 0)
        task = repository.create_task(
            user_id=user_id,
            push_type="diary",
            local_date=diary_date,
            business_key=diary_date,
            content_id=content["id"],
            source_id=diary_id,
            scheduled_at=_app_now(now),
            card=card_snapshot,
        )
        result = deliver_proactive_task(task["id"], now=now)
        if not result:
            return {"state": "PENDING", "userId": user_id, "date": diary_date}
        if result["state"] == "CANCELLED":
            result["state"] = "SKIPPED"
        result["date"] = diary_date
        return result
    except Exception as error:
        state = db.mark_diary_push_failed(diary_id, error, max_retries=3)
        logger.warning(
            "[DiaryPush] setup failed user=%s date=%s state=%s error=%s",
            user_id, diary_date, state, error,
        )
        return {
            "state": state,
            "userId": user_id,
            "date": diary_date,
            "error": str(error),
        }


def _diary_generation_block_reason(candidate):
    if not candidate or candidate.get("state") != "DONE":
        return "diary is not ready"
    if candidate.get("viewed_at"):
        return "diary already viewed"
    generated_at = _parse_datetime(candidate.get("generated_at"))
    if not generated_at:
        return "diary generation time is missing"
    generated_local = generated_at.replace(
        tzinfo=timezone(timedelta(hours=8))
    ).astimezone(_user_timezone(candidate))
    if generated_local.strftime("%Y-%m-%d") != str(candidate["diary_date"]):
        return "diary generated outside push window"
    seconds = (
        generated_local.hour * 3600
        + generated_local.minute * 60
        + generated_local.second
    )
    if seconds < 23 * 3600 or seconds > 23 * 3600 + 30 * 60:
        return "diary generated outside push window"
    return ""


__all__ = [
    "PushDeviceRequestError",
    "PushTestDeliveryError",
    "PushTestDeviceNotRegisteredError",
    "PushTestRequestError",
    "deliver_due_proactive_notifications",
    "deliver_generated_diary_notification",
    "deliver_proactive_task",
    "get_authenticated_user_push_card",
    "register_authenticated_user_push_device",
    "retry_pending_diary_notifications",
    "send_authenticated_user_push_test",
    "unregister_authenticated_user_push_device",
]
