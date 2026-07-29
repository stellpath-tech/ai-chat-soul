from unittest.mock import patch

import pytest

from agent.protocol.models import LLMRequest
from bridge.agent_bridge import AgentLLMModel


def _model_with_stream(fake_stream):
    model = object.__new__(AgentLLMModel)
    model.model = "qwen3.6-plus"
    model._stream_with_model = fake_stream
    return model


def _content_chunk(text):
    return {
        "choices": [
            {
                "delta": {"content": text},
                "finish_reason": None,
            }
        ]
    }


def _empty_chunk():
    return {
        "choices": [
            {
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ]
    }


@patch("config.conf", return_value={"fallback_model": ""})
@patch("bridge.agent_bridge.time.sleep")
def test_retries_once_after_15_seconds_before_any_output(sleep, _conf):
    attempts = []

    def fake_stream(request, model_id, bot=None):
        attempts.append(model_id)
        if len(attempts) == 1:
            raise TimeoutError("read timeout")
        yield _content_chunk("OK")

    model = _model_with_stream(fake_stream)
    chunks = list(model.call_stream(LLMRequest()))

    assert [chunk["choices"][0]["delta"]["content"] for chunk in chunks] == ["OK"]
    assert attempts == ["qwen3.6-plus", "qwen3.6-plus"]
    sleep.assert_called_once_with(15)


@patch("config.conf", return_value={"fallback_model": "fallback-model"})
@patch("bridge.agent_bridge.time.sleep")
def test_does_not_retry_after_partial_content(sleep, _conf):
    attempts = []

    def fake_stream(request, model_id, bot=None):
        attempts.append(model_id)
        yield _content_chunk("partial")
        raise TimeoutError("read timeout")

    model = _model_with_stream(fake_stream)
    stream = model.call_stream(LLMRequest())

    assert next(stream)["choices"][0]["delta"]["content"] == "partial"
    with pytest.raises(RuntimeError, match="PARTIAL_RESPONSE_ABORT"):
        next(stream)

    assert attempts == ["qwen3.6-plus"]
    sleep.assert_not_called()


@patch("config.conf", return_value={"fallback_model": ""})
@patch("bridge.agent_bridge.time.sleep")
def test_empty_protocol_chunk_does_not_create_a_conflicting_response(sleep, _conf):
    attempts = []

    def fake_stream(request, model_id, bot=None):
        attempts.append(model_id)
        if len(attempts) == 1:
            yield _empty_chunk()
            raise TimeoutError("read timeout")
        yield _content_chunk("only answer")

    model = _model_with_stream(fake_stream)
    chunks = list(model.call_stream(LLMRequest()))
    visible = [
        chunk["choices"][0]["delta"].get("content")
        for chunk in chunks
        if chunk["choices"][0]["delta"].get("content")
    ]

    assert visible == ["only answer"]
    assert attempts == ["qwen3.6-plus", "qwen3.6-plus"]
    sleep.assert_called_once_with(15)


@patch("config.conf", return_value={"fallback_model": ""})
@patch("bridge.agent_bridge.time.sleep")
def test_stops_after_one_retry(sleep, _conf):
    attempts = []

    def fake_stream(request, model_id, bot=None):
        attempts.append(model_id)
        raise TimeoutError("read timeout")
        yield

    model = _model_with_stream(fake_stream)

    with pytest.raises(RuntimeError, match="REQUEST_RETRY_EXHAUSTED"):
        list(model.call_stream(LLMRequest()))

    assert attempts == ["qwen3.6-plus", "qwen3.6-plus"]
    sleep.assert_called_once_with(15)


@patch("config.conf", return_value={"fallback_model": "fallback-model"})
@patch("bridge.agent_bridge.time.sleep")
def test_fallback_is_used_only_after_the_single_primary_retry(sleep, _conf):
    attempts = []
    fallback_bot = object()

    def fake_stream(request, model_id, bot=None):
        attempts.append((model_id, bot))
        if model_id == "qwen3.6-plus":
            raise TimeoutError("read timeout")
        yield _content_chunk("fallback answer")

    model = _model_with_stream(fake_stream)
    model._make_fallback_bot = lambda: fallback_bot
    chunks = list(model.call_stream(LLMRequest()))

    visible = [
        chunk["choices"][0]["delta"].get("content")
        for chunk in chunks
        if chunk["choices"][0]["delta"].get("content")
    ]
    assert visible == ["fallback answer"]
    assert attempts == [
        ("qwen3.6-plus", None),
        ("qwen3.6-plus", None),
        ("fallback-model", fallback_bot),
    ]
    sleep.assert_called_once_with(15)


@patch("config.conf", return_value={"fallback_model": "fallback-model"})
@patch("bridge.agent_bridge.time.sleep")
def test_non_retryable_error_does_not_retry_or_fallback(sleep, _conf):
    attempts = []

    def fake_stream(request, model_id, bot=None):
        attempts.append(model_id)
        raise ValueError("invalid request status 400")
        yield

    model = _model_with_stream(fake_stream)

    with pytest.raises(RuntimeError, match="REQUEST_RETRY_EXHAUSTED"):
        list(model.call_stream(LLMRequest()))

    assert attempts == ["qwen3.6-plus"]
    sleep.assert_not_called()
