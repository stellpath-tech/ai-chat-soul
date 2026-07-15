import base64
import hashlib
import hmac
import json
import secrets
import time
import zlib
from dataclasses import dataclass

import requests

from common.log import logger
from config import conf
import channel.web.database as db
from channel.web.push.contracts import (
    DiaryReadyPushNotification,
    PushDeviceRequestError,
    PushTestNotification,
    PushTestRequestError,
    UserPushDeviceRegistration,
    UserPushDeviceUnregistration,
)


class PushTestDeviceNotRegisteredError(RuntimeError):
    pass


class PushTestDeliveryError(RuntimeError):
    pass


class _TencentPushError(RuntimeError):
    pass


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


def deliver_generated_diary_notification(user_id, diary_date):
    try:
        delivery_record = db.claim_diary_push_notification(user_id, diary_date)
        if not delivery_record:
            return None
        return _deliver_claimed_diary_notification(delivery_record)
    except Exception as error:
        logger.exception(
            "[DiaryPush] delivery setup failed user=%s date=%s",
            user_id,
            diary_date,
        )
        return {"state": "ERROR", "error": str(error)}


def retry_pending_diary_notifications(limit=20):
    results = []
    for _ in range(max(1, min(int(limit), 100))):
        delivery_record = db.claim_diary_push_notification()
        if not delivery_record:
            break
        results.append(_deliver_claimed_diary_notification(delivery_record))
    return results


def _deliver_claimed_diary_notification(delivery_record):
    diary_id = delivery_record["diary_id"]
    user_id = delivery_record["user_id"]
    diary_date = delivery_record["diary_date"]
    push_token = str(delivery_record.get("push_token") or "").strip()
    if not push_token:
        db.mark_diary_push_skipped(diary_id, "push device not registered")
        logger.info(
            "[DiaryPush] skipped without device user=%s date=%s",
            user_id,
            diary_date,
        )
        return {"state": "SKIPPED", "userId": user_id, "date": diary_date}

    try:
        push_config = _TencentPushConfig.from_runtime()
        diary_notification = DiaryReadyPushNotification.from_delivery_record(
            delivery_record
        )
        task_id = _TencentPushClient(push_config).send(
            push_token,
            diary_notification,
        )
        db.mark_diary_push_sent(diary_id, task_id)
        logger.info(
            "[DiaryPush] sent user=%s date=%s task=%s",
            user_id,
            diary_date,
            task_id,
        )
        return {
            "state": "SENT",
            "userId": user_id,
            "date": diary_date,
            "taskId": task_id,
        }
    except Exception as error:
        try:
            max_retries = _TencentPushConfig.from_runtime().max_retries
        except Exception:
            max_retries = 3
        state = db.mark_diary_push_failed(
            diary_id,
            error,
            max_retries=max_retries,
        )
        logger.warning(
            "[DiaryPush] failed user=%s date=%s next_state=%s error=%s",
            user_id,
            diary_date,
            state,
            error,
        )
        return {
            "state": state,
            "userId": user_id,
            "date": diary_date,
            "error": str(error),
        }


__all__ = [
    "PushDeviceRequestError",
    "PushTestDeliveryError",
    "PushTestDeviceNotRegisteredError",
    "PushTestRequestError",
    "deliver_generated_diary_notification",
    "register_authenticated_user_push_device",
    "retry_pending_diary_notifications",
    "send_authenticated_user_push_test",
    "unregister_authenticated_user_push_device",
]
