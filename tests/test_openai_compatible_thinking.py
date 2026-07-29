from models.openai_compatible_bot import OpenAICompatibleBot


class CapturingBot(OpenAICompatibleBot):
    def __init__(self, *, openai_v1, model="qwen3.6-plus", enable_thinking=None):
        self._OPENAI_V1 = openai_v1
        self.model = model
        self.enable_thinking = enable_thinking
        self.request_params = None

    def get_api_config(self):
        config = {
            "model": self.model,
            "api_key": "test-key",
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
        if self.enable_thinking is not None:
            config["enable_thinking"] = self.enable_thinking
        return config

    def _handle_sync_response(self, request_params, api_key, api_base):
        self.request_params = request_params
        return {"ok": True}


def _call(bot, **kwargs):
    result = bot.call_with_tools(
        messages=[{"role": "user", "content": "reply OK"}],
        stream=False,
        **kwargs,
    )
    assert result == {"ok": True}
    return bot.request_params


def test_qwen_thinking_is_disabled_at_top_level_for_openai_v0():
    params = _call(CapturingBot(openai_v1=False))

    assert params["enable_thinking"] is False
    assert "extra_body" not in params


def test_qwen_thinking_is_disabled_in_extra_body_for_openai_v1():
    params = _call(CapturingBot(openai_v1=True))

    assert params["extra_body"] == {"enable_thinking": False}
    assert "enable_thinking" not in params


def test_qwen_thinking_override_is_preserved_for_openai_v0():
    params = _call(CapturingBot(openai_v1=False), enable_thinking=True)

    assert params["enable_thinking"] is True


def test_non_qwen_models_do_not_receive_thinking_parameter():
    params = _call(CapturingBot(openai_v1=False, model="gpt-4o"))

    assert "enable_thinking" not in params
    assert "extra_body" not in params
