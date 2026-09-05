import json
import random
from datetime import datetime, timedelta, timezone

import requests

from common.log import logger
from config import conf
from channel.web.push.contracts import _user_timezone
from channel.web.push import repository


APP_TIMEZONE = timezone(timedelta(hours=8))
GREETING_WINDOWS = {
    "morning": ["0700", "0730", "0800", "0830", "0900"],
    "noon": ["1100", "1130", "1200", "1230", "1300"],
    "evening": ["1800", "1830", "1900", "1930", "2000", "2030", "2100"],
}
SEVERITY_PRIORITY = {"moderate": 1, "severe": 2, "extreme": 3}
URGENCY_PRIORITY = {"expected": 1, "immediate": 2}
WEATHER_SCENE_KEYWORDS = [
    ("WEATHER_TYPHOON", ("台风", "热带气旋")),
    ("WEATHER_HAIL", ("冰雹", "强对流")),
    ("WEATHER_THUNDER", ("雷电", "雷雨", "雷暴")),
    ("WEATHER_SHOWER", ("骤雨", "短时强降水")),
    ("WEATHER_HEAVY_RAIN", ("暴雨", "强降水")),
    ("WEATHER_COLD", ("寒潮", "强降温", "低温")),
    ("WEATHER_HEAT", ("高温", "热浪")),
    ("WEATHER_GALE", ("大风", "阵风")),
    ("WEATHER_SNOW", ("暴雪", "大雪")),
    ("WEATHER_ICE", ("冻雨", "道路结冰", "结冰")),
    ("WEATHER_FOG", ("大雾", "浓雾", "低能见度")),
    ("WEATHER_DUST", ("沙尘暴", "扬沙", "沙尘")),
]


def generate_greeting_plans(now_utc=None, random_source=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    random_source = random_source or random.SystemRandom()
    created = []
    for user in repository.list_users():
        if not _can_receive_scheduled_push(user):
            continue
        now_app = now_utc.astimezone(APP_TIMEZONE).replace(tzinfo=None)
        if not _active_within(user.get("last_active_at"), now_app, 72):
            continue
        local_now = now_utc.astimezone(_user_timezone(user))
        local_date = local_now.strftime("%Y-%m-%d")
        marker_key = local_date + ":plan"
        marker = repository.get_task(user["id"], "greeting", marker_key)
        if marker:
            selected_periods = json.loads(marker["card_json"]).get("periods") or []
        else:
            available_periods = [
                period for period, windows in GREETING_WINDOWS.items()
                if _available_windows(windows, local_now)
            ]
            requested_count = 1 if random_source.random() < 0.5 else 2
            selected_periods = random_source.sample(
                available_periods, min(requested_count, len(available_periods))
            ) if available_periods else []
            marker = repository.create_task(
                user_id=user["id"], push_type="greeting",
                local_date=local_date, business_key=marker_key,
                content_id=0, scheduled_at=now_app,
                card={"periods": selected_periods}, state="CANCELLED",
                error="greeting plan marker",
            )
            selected_periods = json.loads(marker["card_json"]).get("periods") or []
        for period in selected_periods:
            business_key = "{}:{}".format(local_date, period)
            if repository.get_task(user["id"], "greeting", business_key):
                continue
            available_windows = _available_windows(GREETING_WINDOWS[period], local_now)
            window = random_source.choice(available_windows)
            content = repository.select_content(
                user["id"], "greeting", ["GREETING_" + window], now=now_app
            )
            if not content:
                created.append(repository.create_task(
                    user_id=user["id"], push_type="greeting",
                    local_date=local_date, business_key=business_key,
                    content_id=0, scheduled_at=now_app, card={},
                    state="CANCELLED", error="no available greeting content",
                ))
                continue
            scheduled_local = _random_time_in_window(
                local_now, window, random_source
            )
            card = _content_card(content, "greeting", "open_chat")
            created.append(repository.create_task(
                user_id=user["id"], push_type="greeting",
                local_date=local_date, business_key=business_key,
                content_id=content["id"],
                scheduled_at=scheduled_local.astimezone(APP_TIMEZONE),
                card=card,
            ))
    return created


def _available_windows(windows, local_now):
    result = []
    next_minute = local_now.replace(second=0, microsecond=0)
    if local_now.second or local_now.microsecond:
        next_minute += timedelta(minutes=1)
    for value in windows:
        start = local_now.replace(
            hour=int(value[:2]), minute=int(value[2:]), second=0, microsecond=0
        )
        if next_minute <= start + timedelta(minutes=29):
            result.append(value)
    return result


def _random_time_in_window(local_now, window, random_source):
    start = local_now.replace(
        hour=int(window[:2]), minute=int(window[2:]), second=0, microsecond=0
    )
    next_minute = local_now.replace(second=0, microsecond=0)
    if local_now.second or local_now.microsecond:
        next_minute += timedelta(minutes=1)
    earliest = max(start, next_minute)
    latest = start + timedelta(minutes=29)
    if earliest > latest:
        earliest = latest
    span_minutes = max(0, int((latest - earliest).total_seconds() // 60))
    return earliest + timedelta(minutes=random_source.randint(0, span_minutes))


def _content_card(content, push_type, action):
    card = {
        "type": push_type,
        "title": content["title"],
        "body": content["body"],
        "action": action,
    }
    if content.get("image_object_key"):
        card["imageObjectKey"] = content["image_object_key"]
        card["imageVersion"] = int(content.get("image_id") or 0)
    return card


class QWeatherAlertClient:
    def __init__(self, http_get=None):
        self._http_get = http_get or requests.get

    def current_alerts(self, latitude, longitude):
        api_host = str(conf().get(
            "qweather_api_host", "https://mt2x88w6bx.re.qweatherapi.com"
        ) or "").strip().rstrip("/")
        api_key = str(conf().get("qweather_api_key", "") or "").strip()
        if not api_host or not api_key:
            raise ValueError("QWeather alert configuration is incomplete")
        if not api_host.startswith("https://"):
            api_host = "https://" + api_host
        response = self._http_get(
            "{}/weatheralert/v1/current/{}/{}".format(
                api_host, round(float(latitude), 2), round(float(longitude), 2)
            ),
            headers={"X-QW-Api-Key": api_key},
            params={"lang": "zh"},
            timeout=(5, 15),
        )
        response.raise_for_status()
        data = response.json()
        alerts = data.get("alerts") or []
        return alerts if isinstance(alerts, list) else []


def poll_weather_alerts(now_utc=None, client=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    now_app = now_utc.astimezone(APP_TIMEZONE).replace(tzinfo=None)
    client = client or QWeatherAlertClient()
    users_by_location = {}
    for user in repository.list_users():
        if not _can_receive_scheduled_push(user):
            continue
        if user.get("last_lat") is None or user.get("last_lon") is None:
            continue
        if not _active_within(user.get("last_active_at"), now_app, 24):
            continue
        local_now = now_utc.astimezone(_user_timezone(user))
        if local_now.hour < 7 or local_now.hour >= 23:
            continue
        local_date = local_now.strftime("%Y-%m-%d")
        if repository.has_weather_task_for_date(user["id"], local_date):
            continue
        key = (round(float(user["last_lat"]), 2), round(float(user["last_lon"]), 2))
        users_by_location.setdefault(key, []).append((user, local_date))

    created = []
    for (latitude, longitude), location_users in users_by_location.items():
        try:
            alert = select_weather_alert(client.current_alerts(latitude, longitude))
        except Exception:
            logger.exception(
                "[WeatherPush] QWeather request failed lat=%s lon=%s",
                latitude, longitude,
            )
            continue
        if not alert:
            continue
        scene = weather_delivery_scene(alert)
        if not scene:
            continue
        for user, local_date in location_users:
            if repository.has_weather_task_for_date(user["id"], local_date):
                continue
            if repository.get_task(user["id"], "weather", str(alert["id"])):
                continue
            content = repository.select_content(
                user["id"], "weather", [scene], now=now_app
            )
            if not content:
                continue
            card = _content_card(content, "weather", "open_weather")
            card.update({
                "effectiveTime": alert.get("effectiveTime"),
                "onsetTime": alert.get("onsetTime"),
                "expireTime": alert.get("expireTime"),
                "headline": alert.get("headline"),
                "description": alert.get("description"),
                "criteria": alert.get("criteria"),
                "responseTypes": alert.get("responseTypes") or [],
                "instruction": alert.get("instruction"),
            })
            created.append(repository.create_task(
                user_id=user["id"], push_type="weather",
                local_date=local_date, business_key=str(alert["id"]),
                content_id=content["id"], scheduled_at=now_app,
                card=card,
            ))
    return created


def select_weather_alert(alerts):
    candidates = []
    for alert in alerts or []:
        message_type = (alert.get("messageType") or {}).get("code")
        urgency = alert.get("urgency")
        severity = alert.get("severity")
        if message_type not in ("alert", "update"):
            continue
        if urgency not in URGENCY_PRIORITY or severity not in SEVERITY_PRIORITY:
            continue
        if not weather_delivery_scene(alert):
            continue
        candidates.append(alert)
    if not candidates:
        return None
    return max(candidates, key=lambda alert: (
        SEVERITY_PRIORITY[alert["severity"]],
        URGENCY_PRIORITY[alert["urgency"]],
        str(alert.get("issuedTime") or ""),
    ))


def weather_delivery_scene(alert):
    event_type = alert.get("eventType") or {}
    value = "{} {}".format(
        str(event_type.get("name") or ""), str(event_type.get("code") or "")
    )
    for scene, keywords in WEATHER_SCENE_KEYWORDS:
        if any(keyword in value for keyword in keywords):
            return scene
    return ""


def schedule_recalls(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    now_app = now_utc.astimezone(APP_TIMEZONE).replace(tzinfo=None)
    created = []
    for user in repository.list_users():
        if not _can_receive_scheduled_push(user):
            continue
        last_active = _parse_app_datetime(user.get("last_active_at"))
        if not last_active:
            continue
        timezone_info = _user_timezone(user)
        local_now = now_utc.astimezone(timezone_info)
        if local_now.hour != 20:
            continue
        local_active = last_active.replace(tzinfo=APP_TIMEZONE).astimezone(timezone_info)
        inactive_days = (local_now.date() - local_active.date()).days
        if inactive_days not in (7, 15, 30):
            continue
        scene = "RECALL_{:02d}".format(inactive_days)
        business_key = "{}|{}".format(user["last_active_at"], inactive_days)
        if repository.get_task(user["id"], "recall", business_key):
            continue
        content = repository.select_content(
            user["id"], "recall", [scene], now=now_app
        )
        if not content:
            continue
        card = _content_card(content, "recall", "open_home")
        card["inactiveDays"] = inactive_days
        created.append(repository.create_task(
            user_id=user["id"],
            push_type="recall",
            local_date=local_now.strftime("%Y-%m-%d"),
            business_key=business_key,
            content_id=content["id"],
            scheduled_at=now_app,
            card=card,
        ))
    return created


def _can_receive_scheduled_push(user):
    return (
        user.get("account_status") == "active"
        and int(user.get("push_device_enabled") or 0) == 1
        and bool(str(user.get("push_token") or "").strip())
        and int(user.get("notification_enabled") or 0) == 1
    )


def _active_within(last_active_at, now_app, hours):
    value = _parse_app_datetime(last_active_at)
    return bool(value and now_app - value <= timedelta(hours=int(hours)))


def _parse_app_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(APP_TIMEZONE).replace(tzinfo=None)
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
