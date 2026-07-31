import json
import threading
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.chat.reply_mode import (
    CLASSIFIER_TIMEOUT,
    REPLY_MODE_MODEL,
    append_reply_mode_instruction,
    classify_reply_mode,
    parse_reply_mode,
)
from agent.chat.service import ChatService
from bridge.agent_bridge import AgentBridge
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.web.web_channel import WebChannel


class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": self._content,
                    }
                }
            ]
        }


def test_parse_reply_mode_accepts_only_supported_directives():
    assert parse_reply_mode('{"reply_mode":"voice"}') == "voice"
    assert parse_reply_mode('```json\n{"reply_mode":"text"}\n```') == "text"
    assert parse_reply_mode('{"reply_mode":null}') is None
    assert parse_reply_mode('{"reply_mode":"audio"}') is None
    assert parse_reply_mode("not-json") is None


def test_reply_mode_instruction_is_the_final_system_sentence():
    base = "基础设定\n\n[记忆]\n用户喜欢散步"

    voice_prompt = append_reply_mode_instruction(base, "voice")
    text_prompt = append_reply_mode_instruction(base, "text")

    assert voice_prompt.endswith("当前回复模式已经切换为语音模式。")
    assert text_prompt.endswith("当前回复模式已经切换为文字模式。")
    assert append_reply_mode_instruction(base, None) == base
    assert append_reply_mode_instruction(base, "unknown") == base


@patch(
    "agent.chat.reply_mode.conf",
    return_value={
        "open_ai_api_key": "test-key",
        "open_ai_api_base": "https://example.test/v1",
    },
)
def test_classifier_uses_one_qwen_flash_call_with_thinking_disabled(_conf):
    post = Mock(return_value=_FakeResponse('{"reply_mode":"voice"}'))

    result = classify_reply_mode(
        "满仓，给我发语音吧",
        request_id="req-1",
        http_post=post,
    )

    assert result == "voice"
    post.assert_called_once()
    call = post.call_args
    assert call.args[0] == "https://example.test/v1/chat/completions"
    assert call.kwargs["json"]["model"] == REPLY_MODE_MODEL
    assert call.kwargs["json"]["enable_thinking"] is False
    assert call.kwargs["json"]["messages"][-1]["content"] == "满仓，给我发语音吧"
    assert call.kwargs["timeout"] == CLASSIFIER_TIMEOUT


@patch(
    "agent.chat.reply_mode.conf",
    return_value={
        "open_ai_api_key": "test-key",
        "open_ai_api_base": "https://example.test/v1",
    },
)
def test_classifier_failure_returns_none_without_retry(_conf):
    post = Mock(side_effect=TimeoutError("read timeout"))

    assert classify_reply_mode("打字回复", http_post=post) is None
    post.assert_called_once()


@patch("agent.chat.reply_mode.conf")
def test_blank_message_skips_classifier_call(conf_mock):
    post = Mock()

    assert classify_reply_mode("  ", http_post=post) is None
    post.assert_not_called()
    conf_mock.assert_not_called()


def test_web_sse_events_and_done_share_one_reply_mode():
    channel = WebChannel()
    request_id = "reply-mode-sse"
    session_id = "reply-mode-session"
    channel.sse_queues[request_id] = Queue()
    channel.request_to_session[request_id] = session_id

    callback = channel._make_sse_callback(
        request_id,
        reply_mode="voice",
    )
    callback({
        "type": "message_update",
        "data": {"delta": "你好"},
    })

    context = Context(ContextType.TEXT, "你好")
    context["request_id"] = request_id
    context["session_id"] = session_id
    context["user_id"] = -1
    context["source"] = "APP"
    context["reply_mode"] = "voice"
    channel.send(Reply(ReplyType.TEXT, "你好"), context)

    delta = channel.sse_queues[request_id].get_nowait()
    done = channel.sse_queues[request_id].get_nowait()
    assert delta["type"] == "delta"
    assert delta["reply_mode"] == "voice"
    assert done["type"] == "done"
    assert done["reply_mode"] == "voice"


def test_poll_response_carries_reply_mode():
    channel = WebChannel()
    request_id = "reply-mode-poll"
    session_id = "reply-mode-poll-session"
    channel.session_queues[session_id] = Queue()
    channel.request_to_session[request_id] = session_id
    channel.sse_queues.pop(request_id, None)

    context = Context(ContextType.TEXT, "改成打字")
    context["request_id"] = request_id
    context["session_id"] = session_id
    context["user_id"] = -1
    context["source"] = "APP"
    context["reply_mode"] = "text"
    channel.send(Reply(ReplyType.TEXT, "好的"), context)

    with patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps({"session_id": session_id}).encode("utf-8"),
    ):
        response = json.loads(channel.poll_response())

    assert response["status"] == "success"
    assert response["has_content"] is True
    assert response["reply_mode"] == "text"


@patch("agent.chat.reply_mode.classify_reply_mode", return_value="voice")
@patch("channel.web.web_channel.db.record_user_timezone_async")
@patch("channel.web.web_channel.conf", return_value={"single_chat_prefix": [""]})
def test_message_response_exposes_the_same_classification(
    _conf,
    _record_timezone,
    classify_mock,
):
    channel = WebChannel()
    context = Context(ContextType.TEXT, "满仓，发语音")
    fake_thread = Mock()

    with patch.object(
        channel,
        "_compose_context",
        return_value=context,
    ), patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps({
            "session_id": "reply-mode-post-session",
            "message": "满仓，发语音",
            "stream": True,
        }).encode("utf-8"),
    ), patch(
        "channel.web.web_channel.web.ctx.env",
        {},
        create=True,
    ), patch(
        "channel.web.web_channel.threading.Thread",
        return_value=fake_thread,
    ):
        response = json.loads(channel.post_message())

    assert response["status"] == "success"
    assert response["reply_mode"] == "voice"
    classify_mock.assert_called_once_with(
        "满仓，发语音",
        request_id=response["request_id"],
    )
    assert context.get("reply_mode") == "voice"
    fake_thread.start.assert_called_once()


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
    system_prompts = []

    def __init__(self, **kwargs):
        self.on_event = kwargs["on_event"]
        self.messages = kwargs["messages"]
        self.system_prompts.append(kwargs["system_prompt"])

    def run_stream(self, query):
        self.on_event({
            "type": "message_update",
            "data": {"delta": "回答"},
        })
        self.messages.append({"role": "user", "content": query})
        self.messages.append({"role": "assistant", "content": "回答"})
        return "回答"


@patch("agent.chat.reply_mode.classify_reply_mode", return_value="text")
@patch("agent.protocol.agent_stream.AgentStreamExecutor", _FakeExecutor)
@patch("config.conf", return_value={"agent_max_context_turns": 30})
def test_cloud_chat_chunks_carry_the_single_classification(
    _conf,
    classify_mock,
):
    chunks = []
    _FakeExecutor.system_prompts = []

    ChatService(_FakeBridge()).run(
        query="别发语音，打字回复",
        session_id="cloud-session",
        send_chunk_fn=chunks.append,
    )

    classify_mock.assert_called_once_with("别发语音，打字回复")
    assert _FakeExecutor.system_prompts == [
        "system\n\n当前回复模式已经切换为文字模式。"
    ]
    assert chunks == [
        {
            "chunk_type": "content",
            "delta": "回答",
            "segment_id": 0,
            "reply_mode": "text",
        }
    ]


class _FakeWebAgent:
    def __init__(self):
        self.runtime_info = {}
        self.messages = []
        self.tools = []
        self.stream_executor = SimpleNamespace(files_to_send=[])
        self.append_system = None

    def run_stream(
        self,
        user_message,
        on_event=None,
        clear_history=False,
        append_system=None,
    ):
        self.append_system = append_system
        return "好的"


@patch("agent.memory.user_cache.update_conversation")
@patch(
    "agent.memory.thing_memory.get_memory_block",
    return_value="[记忆]\n用户喜欢散步",
)
@patch(
    "config.conf",
    return_value={
        "enable_sensor_label": False,
        "redline_input_filter_enabled": False,
        "redline_output_filter_enabled": False,
        "thing_memory_enabled": True,
        "agent_workspace": "~/cow",
    },
)
def test_web_agent_appends_reply_mode_after_other_dynamic_blocks(
    _conf,
    _memory,
    _update_conversation,
):
    agent = _FakeWebAgent()
    bridge = object.__new__(AgentBridge)
    bridge.workspace_root = "~/cow"
    bridge.get_agent = Mock(return_value=agent)
    context = Context(ContextType.TEXT, "发语音")
    context["session_id"] = "reply-mode-web-agent"
    context["reply_mode"] = "voice"
    context["append_system_prompt"] = "[其他动态状态]"

    reply = bridge.agent_reply("满仓，给我发语音", context=context)

    assert reply.type == ReplyType.TEXT
    assert agent.append_system == (
        "[其他动态状态]\n\n"
        "[记忆]\n用户喜欢散步\n\n"
        "当前回复模式已经切换为语音模式。"
    )
