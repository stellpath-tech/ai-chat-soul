import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from common.log import logger
from config import conf
import channel.web.database as db
from channel.web.diary.prompts import (
    IMAGE_NEGATIVE_PROMPT,
    IMAGE_POSITIVE_PROMPT,
    PRODUCT_DIARY_SYSTEM_PROMPT,
    QUIET_DIARY_SYSTEM_PROMPT,
    QUIET_SCENES,
)
from channel.web.diary.storage import decode_image_base64, store_diary_image
from channel.web.push.service import deliver_generated_diary_notification

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


APP_TIMEZONE = timezone(timedelta(hours=8))
_MANUAL_THREADS = set()
_MANUAL_THREADS_LOCK = threading.Lock()


def _configured(name, default=None):
    value = conf().get(name, default)
    return default if value is None else value


def _user_timezone(user):
    name = str(user.get("tz_iana") or "")
    if name and ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    try:
        offset = int(user.get("tz_offset_min", 480))
    except (TypeError, ValueError):
        offset = 480
    return timezone(timedelta(minutes=offset))


def diary_window(user, diary_date):
    tzinfo = _user_timezone(user)
    local_start = datetime.strptime(diary_date, "%Y-%m-%d").replace(tzinfo=tzinfo)
    local_end = local_start + timedelta(days=1)
    start_at = local_start.astimezone(APP_TIMEZONE).replace(tzinfo=None)
    end_at = local_end.astimezone(APP_TIMEZONE).replace(tzinfo=None)
    return (
        start_at.strftime("%Y-%m-%d %H:%M:%S"),
        end_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def enqueue_diary_for_user(user_id, diary_date, mode="auto", force=False, run_async=True):
    user = _get_user(user_id)
    if not user:
        raise ValueError("active user not found")
    datetime.strptime(diary_date, "%Y-%m-%d")
    job = db.create_or_reset_diary_job(user_id, diary_date, mode=mode, force=force)
    if run_async and job.get("state") == "GENERATING":
        thread = threading.Thread(
            target=_run_manual_job,
            args=(user, diary_date),
            daemon=True,
            name="diary-generate-{}-{}".format(user_id, diary_date),
        )
        with _MANUAL_THREADS_LOCK:
            _MANUAL_THREADS.add(thread)
        thread.start()
    return job


def _run_manual_job(user, diary_date):
    try:
        generate_user_diary(user, diary_date)
    finally:
        current = threading.current_thread()
        with _MANUAL_THREADS_LOCK:
            _MANUAL_THREADS.discard(current)


def generate_user_diary(user, diary_date):
    job = db.claim_diary_job(user["id"], diary_date)
    if not job:
        return None
    try:
        start_at, end_at = diary_window(user, diary_date)
        messages = db.list_chat_messages_in_window(user["id"], start_at, end_at)
        transcript = _build_transcript(messages)
        weather_text = next(
            (str(message.get("weather_text") or "").strip() for message in reversed(messages)
             if str(message.get("weather_text") or "").strip()),
            "",
        )
        threshold = max(0, int(_configured("diary_quiet_message_threshold", 3)))
        mode = str(job.get("mode") or "auto").lower()
        resolved_mode = "quiet" if mode == "quiet" or (mode == "auto" and len(messages) < threshold) else "normal"
        if resolved_mode == "normal" and not messages:
            raise ValueError("normal diary has no messages")

        text_output = _generate_text(diary_date, transcript, resolved_mode)
        image_urls = []
        if bool(_configured("diary_image_enabled", False)):
            image_urls = _generate_images(
                user["id"], diary_date, text_output.get("image_prompts") or [], resolved_mode,
            )

        source_ids = [str(message["id"]) for message in messages]
        transcript_hash = hashlib.sha256(
            json.dumps(transcript, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        title = str(text_output.get("title") or "满仓的日记").strip()
        summary = str(text_output.get("summary") or "满仓记下了今天的一点陪伴").strip()
        content = str(text_output["diary"]).strip()
        db.complete_diary_job(
            job["id"], title, content, summary, image_urls, weather_text, resolved_mode,
            source_ids, transcript_hash,
        )
        db.append_diary_card_message(
            user["id"], title, summary, weather_text=weather_text,
            request_id="diary:{}".format(diary_date),
        )
        deliver_generated_diary_notification(user["id"], diary_date)
        logger.info(
            "[Diary] generated user=%s date=%s mode=%s messages=%s images=%s",
            user["id"], diary_date, resolved_mode, len(messages), len(image_urls),
        )
        return {"state": "DONE", "title": title, "imageUrls": image_urls}
    except Exception as error:
        state = db.fail_diary_job(job["id"], error, max_retries=int(_configured("diary_max_retries", 3)))
        logger.exception(
            "[Diary] generation failed user=%s date=%s next_state=%s",
            user["id"], diary_date, state,
        )
        return {"state": state, "error": str(error)}


def _get_user(user_id):
    for user in db.list_active_users_for_diary():
        if int(user["id"]) == int(user_id):
            return user
    return None


def _build_transcript(messages):
    result = []
    for message in messages:
        content = str(message.get("content") or "").strip()
        if message.get("image_url"):
            content = ("[图片] " + content).strip()
        if not content:
            continue
        result.append({
            "id": str(message["id"]),
            "role": str(message.get("role") or "user"),
            "content": content,
            "createdAt": str(message.get("created_at") or ""),
        })
    return result


def _generate_text(diary_date, transcript, resolved_mode):
    max_chars = max(60, int(_configured("diary_max_chars", 120)))
    scene = _quiet_scene(diary_date) if resolved_mode == "quiet" else ""
    user_prompt = """生成 {date} 的满仓日记。
模式：{mode}
目标正文长度：约 {max_chars} 字，允许自然浮动，但不能截断句子。
安静日场景：{scene}
聊天记录：
{transcript}
""".format(
        date=diary_date,
        mode=resolved_mode,
        max_chars=max_chars,
        scene=scene or "无",
        transcript=json.dumps(transcript, ensure_ascii=False),
    )
    system_prompt = PRODUCT_DIARY_SYSTEM_PROMPT
    if resolved_mode == "quiet":
        system_prompt += "\n\n" + QUIET_DIARY_SYSTEM_PROMPT

    last_error = None
    for attempt in range(2):
        try:
            output = _call_chat_model(system_prompt, user_prompt)
            parsed = _parse_json_object(output)
            parsed["diary"] = _normalize_diary(parsed.get("diary"))
            errors = _validate_diary(parsed, max_chars)
            if errors:
                raise ValueError("; ".join(errors))
            parsed["image_prompts"] = _normalize_image_prompts(
                parsed.get("image_prompts"), resolved_mode, scene,
            )
            return parsed
        except Exception as error:
            last_error = error
            user_prompt += "\n上次输出未通过校验：{}。请重新输出完整 JSON。".format(error)
    raise last_error or RuntimeError("diary text generation failed")


def _call_chat_model(system_prompt, user_prompt):
    api_key = str(_configured("diary_text_api_key", "") or _configured("open_ai_api_key", "") or "")
    api_base = str(_configured("diary_text_api_base", "") or _configured("open_ai_api_base", "https://api.openai.com/v1")).rstrip("/")
    model = str(_configured("diary_text_model", "") or _configured("model", ""))
    if not api_key or not model:
        raise ValueError("diary text model configuration is incomplete")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "top_p": 0.9,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    response = requests.post(
        api_base + "/chat/completions", headers=headers, json=payload, timeout=(10, 120),
    )
    if response.status_code == 400 and "response_format" in response.text:
        payload.pop("response_format", None)
        response = requests.post(
            api_base + "/chat/completions", headers=headers, json=payload, timeout=(10, 120),
        )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _parse_json_object(value):
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output is not a JSON object")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


def _normalize_diary(value):
    return re.sub(r"\s+", "", str(value or "")).strip()


def _validate_diary(output, max_chars):
    content = str(output.get("diary") or "")
    errors = []
    if not content:
        errors.append("diary is empty")
    if re.search(r"AI|模型|prompt|规则|系统设定", content, flags=re.I):
        errors.append("diary contains technical wording")
    if len(content) < max(30, int(max_chars * 0.55)):
        errors.append("diary is too short")
    if "\n" in content:
        errors.append("diary contains line breaks")
    return errors


def _quiet_scene(seed):
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(QUIET_SCENES)
    return QUIET_SCENES[index]


def _normalize_image_prompts(value, resolved_mode, quiet_scene):
    prompts = value if isinstance(value, list) else []
    scenes = []
    for item in prompts[:2]:
        scene = item.get("scene") if isinstance(item, dict) else item
        if str(scene or "").strip():
            scenes.append(str(scene).strip())
    if not scenes:
        scenes = [quiet_scene if resolved_mode == "quiet" else "满仓陪在用户身边，记录今天温柔的生活片段"]
    return [{
        "scene": scene,
        "positive_prompt": IMAGE_POSITIVE_PROMPT + "\n具体场景：" + scene,
        "negative_prompt": IMAGE_NEGATIVE_PROMPT,
    } for scene in scenes]


def _generate_images(user_id, diary_date, prompts, resolved_mode):
    count = max(1, min(2, int(_configured("diary_image_count", 1))))
    results = []
    for index, prompt in enumerate(prompts[:count]):
        try:
            image_bytes, content_type = _call_image_model(prompt)
            public_url = store_diary_image(
                user_id, diary_date, uuid.uuid4().hex, image_bytes, content_type,
            )
            results.append(public_url)
        except Exception:
            logger.exception(
                "[Diary] image generation failed user=%s date=%s index=%s mode=%s",
                user_id, diary_date, index, resolved_mode,
            )
    return results


def _call_image_model(prompt):
    api_key = str(_configured("diary_image_api_key", "") or _configured("open_ai_api_key", "") or "")
    api_base = str(_configured("diary_image_api_base", "") or "https://api.openai.com/v1").rstrip("/")
    model = str(_configured("diary_image_model", "gpt-image-2"))
    if not api_key:
        raise ValueError("diary image model configuration is incomplete")
    payload = {
        "model": model,
        "prompt": prompt["positive_prompt"] + "\n反向约束：" + prompt["negative_prompt"],
        "size": str(_configured("diary_image_size", "1024x1024")),
        "quality": str(_configured("diary_image_quality", "medium")),
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    last_error = None
    for attempt in range(4):
        if attempt:
            time.sleep((2, 5, 10)[attempt - 1])
        try:
            response = requests.post(
                api_base + "/images/generations", headers=headers, json=payload, timeout=(10, 180),
            )
            response.raise_for_status()
            item = response.json()["data"][0]
            if item.get("b64_json"):
                return decode_image_base64(item["b64_json"]), "image/png"
            if item.get("url"):
                downloaded = requests.get(item["url"], timeout=(10, 120))
                downloaded.raise_for_status()
                return downloaded.content, downloaded.headers.get("Content-Type", "image/png").split(";", 1)[0]
            raise ValueError("image response has neither b64_json nor url")
        except Exception as error:
            last_error = error
    raise last_error or RuntimeError("image generation failed")
