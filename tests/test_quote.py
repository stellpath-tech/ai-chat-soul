import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.chat.quote import (
    MAX_QUOTE_CHARS,
    QUOTE_HANDLING_INSTRUCTION,
    append_quote_instruction,
    compose_with_quote,
    format_quote_block,
    normalize_quote,
)
from agent.chat.service import ChatService
from bridge.agent_bridge import AgentBridge
from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel.web.web_channel import WebChannel


def test_normalize_quote_keeps_only_usable_payloads():
    assert normalize_quote(None) is None
    assert normalize_quote("上一句") is None
    assert normalize_quote({}) is None
    assert normalize_quote({"content": "   "}) is None
    assert normalize_quote({"role": "user", "content": "我今天很累"}) == {
        "message_id": "",
        "role": "user",
        "content": "我今天很累",
    }


def test_normalize_quote_defaults_unknown_roles_to_assistant():
    assert normalize_quote({"role": "system", "content": "x"})["role"] == "assistant"
    assert normalize_quote({"role": "USER", "content": "x"})["role"] == "user"
    assert normalize_quote({"messageId": " 42 ", "content": "x"})["message_id"] == "42"


def test_normalize_quote_truncates_overlong_content():
    quote = normalize_quote({"role": "user", "content": "满" * (MAX_QUOTE_CHARS + 50)})
    assert quote["content"] == "满" * MAX_QUOTE_CHARS + "…"


def test_quote_block_labels_the_quoted_speaker():
    assert format_quote_block(None) == ""
    assert format_quote_block({"role": "user", "content": "我今天很累"}) == "【引用】用户: 我今天很累\n"
    assert format_quote_block({"role": "assistant", "content": "早点休息"}) == "【引用】AI: 早点休息\n"


def test_compose_with_quote_prefixes_text_messages():
    quote = {"role": "assistant", "content": "早点休息"}
    assert compose_with_quote("我做不到", quote) == "【引用】AI: 早点休息\n我做不到"
    assert compose_with_quote("我做不到", None) == "我做不到"


def test_compose_with_quote_targets_the_first_text_block():
    quote = {"role": "user", "content": "这张图"}
    composed = compose_with_quote(
        [
            {"type": "text", "text": "看看"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ],
        quote,
    )
    assert composed[0] == {"type": "text", "text": "【引用】用户: 这张图\n看看"}
    assert composed[1]["type"] == "image_url"


def test_compose_with_quote_prepends_a_text_block_to_image_only_messages():
    composed = compose_with_quote(
        [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
        {"role": "user", "content": "这张图"},
    )
    assert composed[0] == {"type": "text", "text": "【引用】用户: 这张图"}
    assert composed[1]["type"] == "image_url"


def test_quote_instruction_is_only_added_for_quoted_turns():
    assert append_quote_instruction("[记忆]", None) == "[记忆]"
    assert append_quote_instruction("[记忆]", {"content": ""}) == "[记忆]"
    assert append_quote_instruction("[记忆]", {"role": "user", "content": "x"}) == (
        f"[记忆]\n\n{QUOTE_HANDLING_INSTRUCTION}"
    )
    assert append_quote_instruction(None, {"role": "user", "content": "x"}) == (
        QUOTE_HANDLING_INSTRUCTION
    )


@patch("agent.chat.reply_mode.classify_reply_mode", return_value=None)
@patch("channel.web.web_channel.db.record_user_timezone_async")
@patch("channel.web.web_channel.conf", return_value={"single_chat_prefix": [""]})
def test_post_message_puts_the_normalized_quote_on_the_context(
    _conf,
    _record_timezone,
    _classify,
):
    channel = WebChannel()
    context = Context(ContextType.TEXT, "我做不到")

    with patch.object(
        channel,
        "_compose_context",
        return_value=context,
    ), patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps({
            "session_id": "quote-post-session",
            "message": "我做不到",
            "stream": True,
            "quote": {"messageId": "7", "role": "assistant", "content": "早点休息"},
        }).encode("utf-8"),
    ), patch(
        "channel.web.web_channel.web.ctx.env",
        {},
        create=True,
    ), patch(
        "channel.web.web_channel.threading.Thread",
        return_value=Mock(),
    ):
        response = json.loads(channel.post_message())

    assert response["status"] == "success"
    assert context.get("quote") == {
        "message_id": "7",
        "role": "assistant",
        "content": "早点休息",
    }


@patch("agent.chat.reply_mode.classify_reply_mode", return_value=None)
@patch("channel.web.web_channel.db.record_user_timezone_async")
@patch("channel.web.web_channel.conf", return_value={"single_chat_prefix": [""]})
def test_post_message_drops_a_malformed_quote(
    _conf,
    _record_timezone,
    _classify,
):
    channel = WebChannel()
    context = Context(ContextType.TEXT, "在吗")

    with patch.object(
        channel,
        "_compose_context",
        return_value=context,
    ), patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps({
            "session_id": "quote-bad-session",
            "message": "在吗",
            "stream": True,
            "quote": {"role": "assistant", "content": "   "},
        }).encode("utf-8"),
    ), patch(
        "channel.web.web_channel.web.ctx.env",
        {},
        create=True,
    ), patch(
        "channel.web.web_channel.threading.Thread",
        return_value=Mock(),
    ):
        response = json.loads(channel.post_message())

    assert response["status"] == "success"
    assert context.get("quote") is None


class _FakeWebAgent:
    def __init__(self):
        self.runtime_info = {}
        self.messages = []
        self.tools = []
        self.stream_executor = SimpleNamespace(files_to_send=[])
        self.user_message = None
        self.append_system = None

    def run_stream(
        self,
        user_message,
        on_event=None,
        clear_history=False,
        append_system=None,
    ):
        self.user_message = user_message
        self.append_system = append_system
        return "好的"


def _bridge_with(agent):
    bridge = object.__new__(AgentBridge)
    bridge.workspace_root = "~/cow"
    bridge.get_agent = Mock(return_value=agent)
    return bridge


_BRIDGE_CONF = {
    "enable_sensor_label": False,
    "redline_input_filter_enabled": False,
    "redline_output_filter_enabled": False,
    "thing_memory_enabled": False,
    "agent_workspace": "~/cow",
}


@patch("agent.memory.user_cache.update_conversation")
@patch("config.conf", return_value=_BRIDGE_CONF)
def test_web_agent_injects_the_quote_into_message_and_system_prompt(
    _conf,
    _update_conversation,
):
    agent = _FakeWebAgent()
    context = Context(ContextType.TEXT, "我做不到", kwargs={})
    context["session_id"] = "quote-web-agent"
    context["quote"] = {"message_id": "7", "role": "assistant", "content": "早点休息"}

    reply = _bridge_with(agent).agent_reply("我做不到", context=context)

    assert reply.type == ReplyType.TEXT
    assert agent.user_message == "【引用】AI: 早点休息\n我做不到"
    assert agent.append_system == QUOTE_HANDLING_INSTRUCTION


@patch("agent.memory.user_cache.update_conversation")
@patch("config.conf", return_value=_BRIDGE_CONF)
def test_web_agent_leaves_unquoted_turns_untouched(
    _conf,
    _update_conversation,
):
    agent = _FakeWebAgent()
    context = Context(ContextType.TEXT, "在吗", kwargs={})
    context["session_id"] = "quote-web-agent-none"

    _bridge_with(agent).agent_reply("在吗", context=context)

    assert agent.user_message == "在吗"
    assert agent.append_system is None


class _FakeAgent:
    def __init__(self):
        self.messages_lock = threading.Lock()
        self.messages = []
        self.model = object()
        self.tools = []
        self.max_steps = 1
        self.stream_executor = None

    def get_full_system_prompt(self):
        return "system"

    def _execute_post_process_tools(self):
        return None


class _FakeBridge:
    def __init__(self):
        self.agent = _FakeAgent()

    def get_agent(self, session_id):
        return self.agent


class _FakeExecutor:
    queries = []
    system_prompts = []

    def __init__(self, **kwargs):
        self.on_event = kwargs["on_event"]
        self.messages = kwargs["messages"]
        self.system_prompts.append(kwargs["system_prompt"])

    def run_stream(self, query):
        self.queries.append(query)
        self.messages.append({"role": "user", "content": query})
        self.messages.append({"role": "assistant", "content": "回答"})
        return "回答"


@patch("agent.chat.reply_mode.classify_reply_mode", return_value=None)
@patch("agent.protocol.agent_stream.AgentStreamExecutor", _FakeExecutor)
@patch("config.conf", return_value={"agent_max_context_turns": 30})
def test_cloud_chat_carries_the_quote(_conf, classify_mock):
    _FakeExecutor.queries = []
    _FakeExecutor.system_prompts = []

    ChatService(_FakeBridge()).run(
        query="我做不到",
        session_id="quote-cloud-session",
        send_chunk_fn=lambda chunk: None,
        parent_reply_mode="text",
        quote={"role": "assistant", "content": "早点休息"},
    )

    # The classifier still sees the raw message, not the quote-prefixed one.
    classify_mock.assert_called_once_with("我做不到", parent_reply_mode="text")
    assert _FakeExecutor.queries == ["【引用】AI: 早点休息\n我做不到"]
    assert _FakeExecutor.system_prompts == [
        f"system\n\n{QUOTE_HANDLING_INSTRUCTION}\n\n当前的回复模式保持为文字。"
    ]
