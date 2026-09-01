"""Start a fresh conversation after the user has been away across midnight.

Rule, evaluated in the user's own timezone: if the previous user message fell on
an earlier calendar day AND landed at least IDLE_SECONDS ago, the current
dialogue is archived and cleared before this turn runs.

Only the dialogue context resets. session_id, ThingMemory, nickname and the
device workspace are all left alone — 满仓 starts the day fresh without
forgetting who it is talking to.

"At most once a day" needs no extra bookkeeping: once a rotation happens, the
turn that triggered it becomes the latest user message of *today*, so the
earlier-day check cannot pass again until the next midnight.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from common.log import logger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

# How long the user must have been silent, on top of the day boundary.
IDLE_SECONDS = 2 * 3600

# Falls back to China Standard Time, matching the rest of the agent.
DEFAULT_TZ = dt.timezone(dt.timedelta(hours=8))


# ── Timezone ──────────────────────────────────────────────────────────────────

def _tz_from_profile(profile: Any) -> Optional[dt.tzinfo]:
    """Build a tzinfo from a {tz_iana, tz_offset_min} mapping, or None."""
    if not hasattr(profile, "get"):
        return None

    name = str(profile.get("tz_iana") or "").strip()
    if name and ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass

    offset = profile.get("tz_offset_min")
    if offset is None:
        return None
    try:
        return dt.timezone(dt.timedelta(minutes=int(offset)))
    except (TypeError, ValueError, OverflowError):
        return None


def resolve_timezone(payload: Any = None, user: Any = None) -> dt.tzinfo:
    """Pick the timezone to judge "midnight" in.

    The request payload wins because it reflects where the user is *right now*;
    the stored user profile is the fallback for clients that omit it.
    """
    return _tz_from_profile(payload) or _tz_from_profile(user) or DEFAULT_TZ


# ── Timestamp encoding ────────────────────────────────────────────────────────
# Stored as ISO-8601 UTC so the cache file and current_setting row stay readable
# when debugging on the box.

def encode_ts(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).isoformat()


def decode_ts(raw: Any) -> Optional[dt.datetime]:
    """Parse a stored timestamp; None for missing or corrupt values."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Rows written before this field existed can't be naive, but be defensive.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# ── The rule ──────────────────────────────────────────────────────────────────

def should_rotate(
    last_user_msg_at: Optional[dt.datetime],
    now: dt.datetime,
    tzinfo: dt.tzinfo,
) -> bool:
    """True when this turn should start a new conversation.

    Both conditions must hold: the silence is long enough, and it spans a
    midnight in the user's timezone. A quiet afternoon does not rotate, and
    neither does chatting straight through midnight.
    """
    if last_user_msg_at is None:
        return False
    if (now - last_user_msg_at).total_seconds() < IDLE_SECONDS:
        return False
    return last_user_msg_at.astimezone(tzinfo).date() < now.astimezone(tzinfo).date()


# ── Rotation ──────────────────────────────────────────────────────────────────

def _texts_from(messages: List[dict], role: str) -> List[str]:
    """Flatten message content to plain text, skipping tool blocks."""
    out = []
    for msg in messages:
        if msg.get("role") != role:
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            continue
        if text.strip():
            out.append(text)
    return out


def _extract_memories(workspace_root: str, session_id: str, messages: List[dict]) -> None:
    """Fold the conversation being dropped into ThingMemory.

    Runs off a snapshot, so it does not have to finish before the dialogue is
    cleared — nothing is lost either way.
    """
    user_texts = _texts_from(messages, "user")
    if not user_texts:
        return

    from config import conf
    from agent.memory.thing_memory import fire_extract

    assistant_texts = _texts_from(messages, "assistant")
    fire_extract(
        workspace_root,
        session_id,
        session_id,
        user_texts,
        conf().get("thing_memory_extractor_api_key") or conf().get("open_ai_api_key", ""),
        (conf().get("thing_memory_extractor_api_base")
         or conf().get("open_ai_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
        conf().get("thing_memory_extractor_model", "qwen3.5-flash"),
        assistant_texts[-1] if assistant_texts else "",
    )


def _load_conversation(workspace_root: str, session_id: str) -> List[dict]:
    """Current dialogue, file cache first (freshest), then current_setting."""
    from agent.memory.user_cache import get as cache_get

    cached = cache_get(workspace_root, session_id)
    if cached is not None:
        return cached.get("conversation") or []

    from agent.memory.user_cache import _load_from_db
    return _load_from_db(workspace_root, session_id).get("conversation") or []


def _drop_in_memory_agent(session_id: str) -> None:
    """Clear the live Agent's history.

    Without this the file and DB are cleared but the in-memory instance keeps
    the old messages — and writes them straight back at the end of the turn.
    """
    from bridge.bridge import Bridge

    bridge = Bridge().peek_agent_bridge()
    if bridge is not None:
        bridge.reset_conversation(session_id)


def rotate(workspace_root: str, session_id: str, now: dt.datetime) -> int:
    """Archive and clear the dialogue. Returns how many messages were dropped."""
    from agent.memory.thing_memory.store import archive_messages
    from agent.memory.user_cache import update_conversation, update_last_user_msg_at

    messages = _load_conversation(workspace_root, session_id)

    try:
        _drop_in_memory_agent(session_id)
    except Exception as e:
        # The caches below still get cleared, but a live Agent would resurrect
        # the old context — worth shouting about.
        logger.warning(f"[SessionRotate] in-memory reset failed for {session_id}: {e}")

    if messages:
        try:
            archive_messages(workspace_root, session_id, messages)
        except Exception as e:
            logger.warning(f"[SessionRotate] archive failed for {session_id}: {e}")
    update_conversation(workspace_root, session_id, [])
    update_last_user_msg_at(workspace_root, session_id, encode_ts(now))

    if messages:
        try:
            _extract_memories(workspace_root, session_id, messages)
        except Exception as e:
            logger.warning(f"[SessionRotate] memory extraction failed for {session_id}: {e}")

    return len(messages)


def maybe_rotate(
    workspace_root: str,
    session_id: str,
    tzinfo: dt.tzinfo,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Rotate if the rule fires, then stamp this turn as the latest user message.

    Called once per incoming user message. Safe to call for every request: a
    session with no recorded history simply gets its first stamp.
    """
    from agent.memory.user_cache import get as cache_get, update_last_user_msg_at

    now = now or dt.datetime.now(dt.timezone.utc)
    cached = cache_get(workspace_root, session_id) or {}
    last = decode_ts(cached.get("last_user_msg_at"))

    if not should_rotate(last, now, tzinfo):
        update_last_user_msg_at(workspace_root, session_id, encode_ts(now))
        return False

    dropped = rotate(workspace_root, session_id, now)
    idle_hours = (now - last).total_seconds() / 3600
    logger.info(
        f"[SessionRotate] {session_id} rotated: idle={idle_hours:.1f}h "
        f"last={last.astimezone(tzinfo):%Y-%m-%d %H:%M} ({tzinfo}) "
        f"dropped={dropped} messages"
    )
    return True
