import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


SUPPORTED_PUSH_PLATFORMS = {"ios", "android", "harmonyos"}


class PushDeviceRequestError(ValueError):
    pass


class PushTestRequestError(ValueError):
    pass


def _required_text(
    payload,
    field_name,
    max_length,
    error_type=PushDeviceRequestError,
):
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise error_type("{} is required".format(field_name))
    value = value.strip()
    if len(value) > max_length:
        raise error_type("{} is too long".format(field_name))
    return value


def _optional_text(payload, field_name, max_length):
    value = payload.get(field_name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PushDeviceRequestError("{} must be a string".format(field_name))
    value = value.strip()
    if len(value) > max_length:
        raise PushDeviceRequestError("{} is too long".format(field_name))
    return value


@dataclass(frozen=True)
class UserPushDeviceRegistration:
    platform: str
    push_token: str
    device_id: str
    app_version: str
    os_version: str
    device_brand: str
    device_model: str

    @classmethod
    def from_request_body(cls, payload):
        if not isinstance(payload, dict):
            raise PushDeviceRequestError("request body must be an object")
        platform = _required_text(payload, "platform", 32).lower()
        if platform not in SUPPORTED_PUSH_PLATFORMS:
            raise PushDeviceRequestError("invalid platform")
        return cls(
            platform=platform,
            push_token=_required_text(payload, "pushToken", 1024),
            device_id=_optional_text(payload, "deviceId", 255),
            app_version=_optional_text(payload, "appVersion", 64),
            os_version=_optional_text(payload, "osVersion", 64),
            device_brand=_optional_text(payload, "deviceBrand", 64),
            device_model=_optional_text(payload, "deviceModel", 128),
        )

    def as_database_fields(self):
        return {
            "platform": self.platform,
            "push_token": self.push_token,
            "device_id": self.device_id,
            "app_version": self.app_version,
            "os_version": self.os_version,
            "device_brand": self.device_brand,
            "device_model": self.device_model,
        }


@dataclass(frozen=True)
class UserPushDeviceUnregistration:
    push_token: str

    @classmethod
    def from_request_body(cls, payload):
        if not isinstance(payload, dict):
            raise PushDeviceRequestError("request body must be an object")
        return cls(push_token=_required_text(payload, "pushToken", 1024))


@dataclass(frozen=True)
class PushTestNotification:
    title: str
    content: str

    @classmethod
    def from_request_body(cls, payload):
        if not isinstance(payload, dict):
            raise PushTestRequestError("request body must be an object")
        return cls(
            title=_required_text(
                payload, "title", 32, error_type=PushTestRequestError
            ),
            content=_required_text(
                payload, "content", 50, error_type=PushTestRequestError
            ),
        )

    def to_tencent_request_body(self, sender, receiver, message_random):
        return {
            "From_Account": sender,
            "To_Account": [receiver],
            "MsgRandom": message_random,
            "OfflinePushInfo": {
                "PushFlag": 0,
                "Title": self.title,
                "Desc": self.content,
            },
            "TaskName": "push-test",
            "Classification": 0,
            "OfflineStorageTime": 86400,
        }


@dataclass(frozen=True)
class DiaryReadyPushNotification:
    user_id: int
    diary_date: str
    diary_timestamp_ms: int

    SECTION_TITLE = "记忆碎片"
    TITLE = "快来接收今天的日记呀✨"
    DESCRIPTION = "满仓已经把今天的小确幸整理好啦"

    @classmethod
    def from_delivery_record(cls, delivery_record):
        diary_date = str(delivery_record["diary_date"])
        local_timezone = _user_timezone(delivery_record)
        local_noon = datetime.strptime(diary_date, "%Y-%m-%d").replace(
            hour=12,
            tzinfo=local_timezone,
        )
        return cls(
            user_id=int(delivery_record["user_id"]),
            diary_date=diary_date,
            diary_timestamp_ms=int(local_noon.timestamp() * 1000),
        )

    def to_tencent_request_body(self, sender, receiver, message_random):
        extension = json.dumps({
            "type": "diary",
            "diaryDate": self.diary_date,
            "ts": self.diary_timestamp_ms,
        }, ensure_ascii=False, separators=(",", ":"))
        return {
            "From_Account": sender,
            "To_Account": [receiver],
            "MsgRandom": message_random,
            "OfflinePushInfo": {
                "PushFlag": 0,
                "Title": self.TITLE,
                "Desc": self.DESCRIPTION,
                "Ext": extension,
                "ApnsInfo": {
                    "Title": self.SECTION_TITLE,
                    "SubTitle": self.TITLE,
                },
            },
            "DataId": "diary:{}:{}".format(self.user_id, self.diary_date),
            "TaskName": "diary",
            "Classification": 0,
            "OfflineStorageTime": 86400,
        }


def _user_timezone(delivery_record):
    timezone_name = str(delivery_record.get("tz_iana") or "")
    if timezone_name and ZoneInfo:
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            pass
    try:
        offset_minutes = int(delivery_record.get("tz_offset_min", 480))
    except (TypeError, ValueError):
        offset_minutes = 480
    return timezone(timedelta(minutes=offset_minutes))
