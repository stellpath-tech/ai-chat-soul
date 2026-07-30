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
    KEY_MOMENT_SYSTEM_PROMPT,
    PRODUCT_DIARY_SYSTEM_PROMPT,
    REFERENCE_IMAGE,
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
    min_chars = max(80, int(_configured("diary_v29_min_chars", 160)))
    max_chars = max(min_chars, int(_configured("diary_v29_max_chars", 700)))
    transcript_json = json.dumps(transcript, ensure_ascii=False)
    key_moment_prompt = """请为 {date} 提炼满仓日记的 key moments。
模式：{mode}
聊天记录（按时间顺序，必须只依据这些内容）：
{transcript}
""".format(
        date=diary_date,
        mode=resolved_mode,
        transcript=transcript_json,
    )

    last_error = None
    for attempt in range(2):
        try:
            key_output = _call_chat_model(
                KEY_MOMENT_SYSTEM_PROMPT, key_moment_prompt, max_tokens=1800,
            )
            key_moments = _parse_key_moments(key_output)
            if not key_moments:
                raise ValueError("key moment stage returned no usable moments")

            numbered_moments = "\n".join(
                "{}：{}".format(index + 1, moment)
                for index, moment in enumerate(key_moments)
            )
            diary_prompt = """请为 {date} 写满仓的完整日记正文。
模式：{mode}
请严格围绕以下 key moments 写作：
{moments}

只输出日记正文，不要输出标题、解释、编号或 key moments 列表。
""".format(
                date=diary_date,
                mode=resolved_mode,
                moments=numbered_moments,
            )
            output = _call_chat_model(
                PRODUCT_DIARY_SYSTEM_PROMPT, diary_prompt, max_tokens=2200,
            )
            content = _normalize_diary(output)
            errors = _validate_diary(content, min_chars, max_chars)
            if errors:
                raise ValueError("; ".join(errors))
            return {
                "title": "满仓的日记",
                "summary": key_moments[0][:80],
                "diary": content,
                "key_moments": key_moments,
                "image_prompts": _build_image_prompts(key_moments),
            }
        except Exception as error:
            last_error = error
            key_moment_prompt += "\n上次生成未通过校验：{}。请重新严格按 v29 规则输出。".format(error)
    raise last_error or RuntimeError("diary text generation failed")


def _call_chat_model(system_prompt, user_prompt, max_tokens=2200):
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
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    response = requests.post(
        api_base + "/chat/completions", headers=headers, json=payload, timeout=(10, 120),
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _parse_key_moments(value):
    text = str(value or "").strip()
    numbered = []
    fallback = []
    for line in text.splitlines():
        match = re.match(r"^\s*\d+\s*[.、)：:]\s*(.+?)\s*$", line)
        cleaned = (match.group(1) if match else re.sub(r"^\s*[-*•]\s*", "", line)).strip()
        if not cleaned or cleaned in ("无", "（无）", "(无)"):
            continue
        if match:
            numbered.append(cleaned)
        else:
            fallback.append(cleaned)
    return (numbered or fallback)[:4]


def _normalize_diary(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [re.sub(r"[ \t]+", "", part).strip() for part in re.split(r"\n+", text)]
    return "\n\n".join(part for part in paragraphs if part)


def _validate_diary(content, min_chars=None, max_chars=None):
    content = str(content or "")
    min_chars = max(80, int(min_chars if min_chars is not None else _configured("diary_v29_min_chars", 160)))
    max_chars = max(min_chars, int(max_chars if max_chars is not None else _configured("diary_v29_max_chars", 700)))
    errors = []
    if not content:
        errors.append("diary is empty")
    if re.search(r"AI|模型|prompt|规则|系统设定", content, flags=re.I):
        errors.append("diary contains technical wording")
    if len(content) < min_chars:
        errors.append("diary is too short")
    if len(content) > max_chars:
        errors.append("diary is too long")
    paragraph_count = len([part for part in content.split("\n\n") if part.strip()])
    if paragraph_count < 2 or paragraph_count > 7:
        errors.append("diary must contain 2-7 paragraphs")
    return errors


def _build_image_prompts(key_moments):
    moments = [str(moment).strip() for moment in key_moments[:4] if str(moment).strip()]
    if not moments:
        return []
    numbered_moments = "\n".join(
        "{}：{}".format(index + 1, moment)
        for index, moment in enumerate(moments)
    )
    return [{
        "scene": "；".join(moments),
        "positive_prompt": (
            IMAGE_POSITIVE_PROMPT
            + "\n\n【已填写的 key moments（共 {} 条，请严格生成 {} 个拼接小画面）】：\n".format(
                len(moments), len(moments),
            )
            + numbered_moments
        ),
        "negative_prompt": IMAGE_NEGATIVE_PROMPT,
    }]


def _generate_images(user_id, diary_date, prompts, resolved_mode):
    results = []
    for index, prompt in enumerate(prompts[:1]):
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
    headers = {"Authorization": "Bearer " + api_key}
    use_reference = bool(_configured("diary_reference_image_enabled", True)) and bool(REFERENCE_IMAGE)
    reference_bytes = decode_image_base64(REFERENCE_IMAGE) if use_reference else None
    last_error = None
    for attempt in range(4):
        if attempt:
            time.sleep((2, 5, 10)[attempt - 1])
        try:
            if reference_bytes:
                response = requests.post(
                    api_base + "/images/edits",
                    headers=headers,
                    data=payload,
                    files={"image": ("mancang-v29-reference.png", reference_bytes, "image/png")},
                    timeout=(10, 180),
                )
            else:
                response = requests.post(
                    api_base + "/images/generations",
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=(10, 180),
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
