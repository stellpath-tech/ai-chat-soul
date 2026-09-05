import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


SUPPORTED_PUSH_PLATFORMS = {"ios", "android", "harmonyos"}
SUPPORTED_PUSH_TYPES = {"greeting", "weather", "diary", "recall"}


class PushDeviceRequestError(ValueError):
    pass


class PushTestRequestError(ValueError):
    pass


class UserActivityRequestError(ValueError):
    pass


class PushContentRequestError(ValueError):
    pass


class PushContentImageRequestError(ValueError):
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
class UserActivityReport:
    timezone_profile: dict
    notification_enabled: object
    location: object

    @classmethod
    def from_request_body(cls, payload):
        if not isinstance(payload, dict):
            raise UserActivityRequestError("request body must be an object")
        timezone_payload = payload.get("timezone")
        if not isinstance(timezone_payload, dict):
            raise UserActivityRequestError("timezone is required")
        timezone_name = str(timezone_payload.get("tz_iana") or "").strip()
        if not timezone_name or len(timezone_name) > 255:
            raise UserActivityRequestError("invalid timezone.tz_iana")
        try:
            timezone_offset = int(timezone_payload.get("tz_offset_min"))
        except (TypeError, ValueError):
            raise UserActivityRequestError("invalid timezone.tz_offset_min")
        if timezone_offset < -720 or timezone_offset > 840:
            raise UserActivityRequestError("invalid timezone.tz_offset_min")

        notification_enabled = payload.get("notificationEnabled")
        if notification_enabled is not None and not isinstance(notification_enabled, bool):
            raise UserActivityRequestError("notificationEnabled must be a boolean")

        location_payload = payload.get("location")
        location = None
        if location_payload is not None:
            if not isinstance(location_payload, dict):
                raise UserActivityRequestError("location must be an object")
            try:
                latitude = float(location_payload.get("lat"))
                longitude = float(location_payload.get("lon"))
            except (TypeError, ValueError):
                raise UserActivityRequestError("invalid location")
            if latitude < -90 or latitude > 90 or longitude < -180 or longitude > 180:
                raise UserActivityRequestError("invalid location")
            location = {"lat": latitude, "lon": longitude}

        return cls(
            timezone_profile={
                "tz_iana": timezone_name,
                "tz_offset_min": timezone_offset,
            },
            notification_enabled=notification_enabled,
            location=location,
        )


@dataclass(frozen=True)
class PushContentMutation:
    content_no: str
    push_type: str
    delivery_scene: str
    title: str
    body: str
    enabled: bool

    @classmethod
    def from_request_body(cls, payload):
        if not isinstance(payload, dict):
            raise PushContentRequestError("request body must be an object")
        content_no = _content_required_text(payload, "contentNo", 64)
        push_type = _content_required_text(payload, "pushType", 16).lower()
        if push_type not in SUPPORTED_PUSH_TYPES:
            raise PushContentRequestError("invalid pushType")
        delivery_scene = _content_required_text(payload, "deliveryScene", 64).upper()
        if not delivery_scene.startswith(push_type.upper() + "_"):
            raise PushContentRequestError("deliveryScene does not match pushType")
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise PushContentRequestError("enabled must be a boolean")
        return cls(
            content_no=content_no,
            push_type=push_type,
            delivery_scene=delivery_scene,
            title=_content_required_text(payload, "title", 255),
            body=_content_required_text(payload, "body", 2000),
            enabled=enabled,
        )

    def as_database_fields(self):
        return {
            "content_no": self.content_no,
            "push_type": self.push_type,
            "delivery_scene": self.delivery_scene,
            "title": self.title,
            "body": self.body,
            "enabled": self.enabled,
        }


def _content_required_text(payload, field_name, max_length):
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise PushContentRequestError("{} is required".format(field_name))
    value = value.strip()
    if len(value) > max_length:
        raise PushContentRequestError("{} is too long".format(field_name))
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
class ProactivePushNotification:
    push_id: str
    push_type: str
    title: str
    body: str
    card: dict

    @classmethod
    def from_task(cls, task):
        try:
            card = json.loads(task.get("card_json") or "{}")
        except (TypeError, ValueError) as error:
            raise ValueError("invalid proactive push card") from error
        if not isinstance(card, dict):
            raise ValueError("invalid proactive push card")
        push_id = str(task.get("push_id") or card.get("pushId") or "").strip()
        push_type = str(task.get("push_type") or card.get("type") or "").strip()
        title = str(card.get("title") or "").strip()
        body = str(card.get("body") or "").strip()
        if not push_id or push_type not in SUPPORTED_PUSH_TYPES or not title or not body:
            raise ValueError("incomplete proactive push card")
        card["pushId"] = push_id
        card["type"] = push_type
        return cls(push_id, push_type, title, body, card)

    def to_tencent_request_body(self, sender, receiver, message_random):
        extension_payload = {
            "type": self.push_type,
            "pushId": self.push_id,
        }
        if self.push_type == "diary":
            if self.card.get("diaryDate"):
                extension_payload["diaryDate"] = self.card["diaryDate"]
            if self.card.get("ts") is not None:
                extension_payload["ts"] = self.card["ts"]
        return {
            "From_Account": sender,
            "To_Account": [receiver],
            "MsgRandom": message_random,
            "OfflinePushInfo": {
                "PushFlag": 0,
                "Title": self.title,
                "Desc": self.body,
                "Ext": _compact_json(extension_payload),
            },
            "DataId": self.push_id,
            "TaskName": self.push_type,
            "Classification": 0,
            "OfflineStorageTime": 86400,
        }

def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
