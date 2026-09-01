import datetime as dt
import json
import sqlite3
import threading
from unittest.mock import patch

from agent.chat.session_rotate import (
    DEFAULT_TZ,
    IDLE_SECONDS,
    _texts_from,
    decode_ts,
    encode_ts,
    maybe_rotate,
    resolve_timezone,
    rotate,
    should_rotate,
)
from bridge.agent_bridge import AgentBridge

CST = dt.timezone(dt.timedelta(hours=8))


def _at(y, m, d, hh, mm=0, tz=CST):
    return dt.datetime(y, m, d, hh, mm, tzinfo=tz)


# ── The rule ──────────────────────────────────────────────────────────────────

def test_no_history_never_rotates():
    assert should_rotate(None, _at(2026, 9, 1, 9), CST) is False


def test_long_silence_within_one_day_does_not_rotate():
    # 14:00 -> 20:00, six quiet hours but no midnight in between.
    assert should_rotate(_at(2026, 9, 1, 14), _at(2026, 9, 1, 20), CST) is False


def test_crossing_midnight_without_enough_silence_does_not_rotate():
    # 23:50 -> 01:30 next day: new date, but only 1h40m of quiet.
    assert should_rotate(_at(2026, 8, 31, 23, 50), _at(2026, 9, 1, 1, 30), CST) is False


def test_crossing_midnight_after_two_quiet_hours_rotates():
    assert should_rotate(_at(2026, 8, 31, 23, 50), _at(2026, 9, 1, 9), CST) is True


def test_threshold_is_inclusive():
    last = _at(2026, 8, 31, 23, 0)
    assert should_rotate(last, last + dt.timedelta(seconds=IDLE_SECONDS), CST) is True
    assert should_rotate(last, last + dt.timedelta(seconds=IDLE_SECONDS - 1), CST) is False


def test_the_day_boundary_is_the_users_not_utc():
    # 23:30 +08:00 -> 02:00 +08:00 crosses midnight locally; in UTC both
    # instants are still 2026-08-31, so a UTC-based check would miss it.
    last = _at(2026, 8, 31, 23, 30)
    now = _at(2026, 9, 1, 2, 0)
    assert should_rotate(last, now, CST) is True
    assert should_rotate(last, now, dt.timezone.utc) is False


def test_rotating_cannot_fire_twice_in_one_day():
    """After a rotation the triggering turn is today's latest user message."""
    last = _at(2026, 8, 31, 23, 50)
    first = _at(2026, 9, 1, 9)
    assert should_rotate(last, first, CST) is True
    # Same day, even after another long gap.
    assert should_rotate(first, _at(2026, 9, 1, 23), CST) is False


# ── Timezone resolution ───────────────────────────────────────────────────────

def test_request_payload_wins_over_stored_profile():
    tz = resolve_timezone({"tz_iana": "", "tz_offset_min": -300}, {"tz_offset_min": 480})
    assert dt.datetime(2026, 9, 1, tzinfo=tz).utcoffset() == dt.timedelta(minutes=-300)


def test_stored_profile_is_the_fallback():
    tz = resolve_timezone(None, {"tz_iana": "Asia/Shanghai", "tz_offset_min": 480})
    assert dt.datetime(2026, 9, 1, tzinfo=tz).utcoffset() == dt.timedelta(hours=8)


def test_unusable_input_falls_back_to_china_standard_time():
    assert resolve_timezone(None, None) is DEFAULT_TZ
    assert resolve_timezone("Asia/Shanghai", None) is DEFAULT_TZ
    assert resolve_timezone({"tz_iana": "Not/AZone"}, None) is DEFAULT_TZ
    assert resolve_timezone({"tz_offset_min": "abc"}, None) is DEFAULT_TZ


# ── Timestamp encoding ────────────────────────────────────────────────────────

def test_timestamp_roundtrip_preserves_the_instant():
    when = _at(2026, 8, 31, 23, 50)
    assert decode_ts(encode_ts(when)) == when


def test_decode_rejects_garbage_and_missing_values():
    assert decode_ts(None) is None
    assert decode_ts("") is None
    assert decode_ts("not-a-time") is None
    assert decode_ts(1756700000) is None


def test_decode_treats_naive_stamps_as_utc():
    assert decode_ts("2026-08-31T15:50:00").tzinfo == dt.timezone.utc


# ── Message flattening ────────────────────────────────────────────────────────

def test_texts_from_handles_plain_and_multimodal_content():
    messages = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "回应"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "看这张图"},
        ]},
        {"role": "user", "content": "   "},
        {"role": "user", "content": [{"type": "tool_result", "content": "noise"}]},
    ]
    assert _texts_from(messages, "user") == ["第一句", "看这张图"]
    assert _texts_from(messages, "assistant") == ["回应"]


# ── Rotation against a real workspace ─────────────────────────────────────────

_ROT_CONF = {"agent_workspace": "~/cow", "thing_memory_enabled": True}

_CONVERSATION = [
    {"role": "user", "content": "今天累死了"},
    {"role": "assistant", "content": "早点休息"},
]


def _rows(workspace, table, where="", args=()):
    from agent.memory.thing_memory.store import _conn, db_path
    # _conn runs _init, so the schema exists even when the test never wrote.
    with _conn(db_path(str(workspace))) as conn:
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        return [dict(r) for r in conn.execute(sql, args)]


@patch("agent.chat.session_rotate._drop_in_memory_agent")
@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_rotate_archives_and_clears_the_conversation(_conf, fire_extract, _drop, tmp_path):
    from agent.memory.user_cache import get as cache_get, update_conversation

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))

    dropped = rotate(ws, "user_9", _at(2026, 9, 1, 9))

    assert dropped == 2
    assert cache_get(ws, "user_9")["conversation"] == []
    archived = _rows(ws, "conversation_archive", "user_id = ?", ("user_9",))
    assert [r["content"] for r in archived] == ["今天累死了", "早点休息"]
    assert decode_ts(cache_get(ws, "user_9")["last_user_msg_at"]) == _at(2026, 9, 1, 9)


@patch("agent.chat.session_rotate._drop_in_memory_agent")
@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_rotate_folds_the_dropped_dialogue_into_long_term_memory(
    _conf, fire_extract, _drop, tmp_path
):
    from agent.memory.user_cache import update_conversation

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))

    rotate(ws, "user_9", _at(2026, 9, 1, 9))

    assert fire_extract.call_count == 1
    args = fire_extract.call_args[0]
    assert args[1] == "user_9"
    assert args[3] == ["今天累死了"]        # user messages
    assert args[-1] == "早点休息"           # last assistant reply


@patch("agent.chat.session_rotate._drop_in_memory_agent")
@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_rotate_keeps_identity_and_long_term_memory(_conf, _fire, _drop, tmp_path):
    from agent.memory.thing_memory.store import add_memory
    from agent.memory.user_cache import get as cache_get, update_conversation, update_nickname

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))
    update_nickname(ws, "user_9", "小满")
    add_memory(ws, "user_9", "user_9",
               {"time": "2026-08-31", "category": "偏好", "event": "喜欢喝拿铁"})

    rotate(ws, "user_9", _at(2026, 9, 1, 9))

    assert cache_get(ws, "user_9")["nickname"] == "小满"
    kept = _rows(ws, "thing_memory", "user_id = ? AND status = 'active'", ("user_9",))
    assert [m["event"] for m in kept] == ["喜欢喝拿铁"]


@patch("agent.chat.session_rotate._drop_in_memory_agent")
@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_rotate_on_an_empty_conversation_is_harmless(_conf, fire_extract, _drop, tmp_path):
    ws = str(tmp_path)

    assert rotate(ws, "user_new", _at(2026, 9, 1, 9)) == 0
    assert fire_extract.call_count == 0
    assert _rows(ws, "conversation_archive") == []


# ── maybe_rotate ──────────────────────────────────────────────────────────────

@patch("agent.chat.session_rotate.rotate")
@patch("config.conf", return_value=_ROT_CONF)
def test_maybe_rotate_stamps_every_turn_without_rotating(_conf, rotate_fn, tmp_path):
    from agent.memory.user_cache import get as cache_get, update_conversation

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))

    # First message ever: nothing to compare against.
    assert maybe_rotate(ws, "user_9", CST, now=_at(2026, 8, 31, 22)) is False
    # An hour later, same day.
    assert maybe_rotate(ws, "user_9", CST, now=_at(2026, 8, 31, 23)) is False

    assert rotate_fn.call_count == 0
    assert decode_ts(cache_get(ws, "user_9")["last_user_msg_at"]) == _at(2026, 8, 31, 23)
    assert cache_get(ws, "user_9")["conversation"] == _CONVERSATION


@patch("agent.chat.session_rotate._drop_in_memory_agent")
@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_maybe_rotate_starts_a_new_day(_conf, _fire, _drop, tmp_path):
    from agent.memory.user_cache import get as cache_get, update_conversation

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))
    maybe_rotate(ws, "user_9", CST, now=_at(2026, 8, 31, 23, 50))

    assert maybe_rotate(ws, "user_9", CST, now=_at(2026, 9, 1, 9)) is True

    assert cache_get(ws, "user_9")["conversation"] == []
    assert decode_ts(cache_get(ws, "user_9")["last_user_msg_at"]) == _at(2026, 9, 1, 9)
    # The turn that triggered it is now today's latest message.
    assert maybe_rotate(ws, "user_9", CST, now=_at(2026, 9, 1, 23)) is False


# ── Persistence across a flush ────────────────────────────────────────────────

@patch("config.conf", return_value=_ROT_CONF)
def test_the_stamp_survives_the_flush_to_sqlite(_conf, tmp_path):
    from agent.memory.user_cache import (
        _load_from_db,
        flush,
        update_conversation,
        update_last_user_msg_at,
    )

    ws = str(tmp_path)
    stamp = encode_ts(_at(2026, 8, 31, 23, 50))
    update_conversation(ws, "user_9", list(_CONVERSATION))
    update_last_user_msg_at(ws, "user_9", stamp)

    flush(ws, "user_9")  # file deleted, row written

    assert _load_from_db(ws, "user_9")["last_user_msg_at"] == stamp


@patch("config.conf", return_value=_ROT_CONF)
def test_current_setting_gains_the_column_on_an_old_database(_conf, tmp_path):
    """Boxes running before this feature have a five-column current_setting."""
    from agent.memory.thing_memory.store import _conn, db_path
    from agent.memory.user_cache import _load_from_db

    path = db_path(str(tmp_path))
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE current_setting ("
        "  user_id TEXT PRIMARY KEY, conversation TEXT, nickname TEXT,"
        "  memory TEXT, updated_at TEXT);"
    )
    legacy.execute(
        "INSERT INTO current_setting VALUES (?, ?, ?, ?, ?)",
        ("user_9", json.dumps(_CONVERSATION, ensure_ascii=False), "小满", "[]", "2026-08-31 23:50:00"),
    )
    legacy.commit()
    legacy.close()

    with _conn(path) as conn:  # _init runs the migration
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(current_setting)")}
    assert "last_user_msg_at" in columns

    restored = _load_from_db(str(tmp_path), "user_9")
    assert restored["last_user_msg_at"] is None       # no stamp yet -> never rotates
    assert restored["conversation"] == _CONVERSATION  # existing rows untouched
    assert restored["nickname"] == "小满"


# ── In-memory reset ───────────────────────────────────────────────────────────

class _FakeAgent:
    def __init__(self, messages):
        self.messages_lock = threading.Lock()
        self.messages = messages


def test_reset_conversation_clears_the_live_agent():
    bridge = AgentBridge.__new__(AgentBridge)
    agent = _FakeAgent(list(_CONVERSATION))
    bridge.agents = {"user_9": agent}

    assert bridge.reset_conversation("user_9") == 2
    assert agent.messages == []
    # The agent instance itself stays warm for the next turn.
    assert bridge.agents["user_9"] is agent


def test_reset_conversation_ignores_sessions_that_are_not_in_memory():
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agents = {}
    assert bridge.reset_conversation("user_9") == 0


@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_rotate_reaches_through_to_the_live_agent(_conf, _fire, tmp_path):
    """The whole chain: rotate -> Bridge -> AgentBridge -> Agent.messages."""
    from agent.memory.user_cache import update_conversation

    agent = _FakeAgent(list(_CONVERSATION))
    agent_bridge = AgentBridge.__new__(AgentBridge)
    agent_bridge.agents = {"user_9": agent}

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))

    with patch("bridge.bridge.Bridge") as FakeBridge:
        FakeBridge.return_value.peek_agent_bridge.return_value = agent_bridge
        rotate(ws, "user_9", _at(2026, 9, 1, 9))

    assert agent.messages == []


@patch("agent.memory.thing_memory.fire_extract")
@patch("config.conf", return_value=_ROT_CONF)
def test_rotate_still_clears_disk_when_the_bridge_is_cold(_conf, _fire, tmp_path):
    """No agent in memory yet (fresh process) — rotation must not blow up."""
    from agent.memory.user_cache import get as cache_get, update_conversation

    ws = str(tmp_path)
    update_conversation(ws, "user_9", list(_CONVERSATION))

    with patch("bridge.bridge.Bridge") as FakeBridge:
        FakeBridge.return_value.peek_agent_bridge.return_value = None
        assert rotate(ws, "user_9", _at(2026, 9, 1, 9)) == 2

    assert cache_get(ws, "user_9")["conversation"] == []
