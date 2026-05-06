import unittest

from agent.utils.hehe_harness import (
    apply_consecutive_hehe_harness,
    last_assistant_text,
    replace_last_assistant_text,
    strip_hehe_with_adjacent_punctuation,
)


class TestHeheHarness(unittest.TestCase):
    def test_keeps_first_hehe(self):
        response, changed = apply_consecutive_hehe_harness("我在呀，嘿嘿", "我在呀")

        self.assertFalse(changed)
        self.assertEqual(response, "我在呀，嘿嘿")

    def test_removes_second_hehe_with_left_punctuation(self):
        response, changed = apply_consecutive_hehe_harness(
            "我在呀，嘿嘿",
            "刚刚也在呀，嘿嘿",
        )

        self.assertTrue(changed)
        self.assertEqual(response, "我在呀")

    def test_removes_second_hehe_with_right_punctuation(self):
        response, changed = apply_consecutive_hehe_harness(
            "嘿嘿，不耽误你时间呀",
            "好呀 嘿嘿",
        )

        self.assertTrue(changed)
        self.assertEqual(response, "不耽误你时间呀")

    def test_removes_punctuation_on_both_sides(self):
        self.assertEqual(strip_hehe_with_adjacent_punctuation("我来啦，嘿嘿！"), "我来啦")

    def test_updates_last_assistant_text(self):
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "上一轮，嘿嘿"}]},
            {"role": "user", "content": [{"type": "text", "text": "你的文件是啥"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "没有文件，嘿嘿"}]},
        ]

        self.assertEqual(last_assistant_text(messages), "没有文件，嘿嘿")
        self.assertTrue(replace_last_assistant_text(messages, "没有文件"))
        self.assertEqual(last_assistant_text(messages), "没有文件")

    def test_stream_executor_suppresses_before_history_write(self):
        import logging

        logging.disable(logging.CRITICAL)
        try:
            from agent.protocol.agent_stream import AgentStreamExecutor
            from agent.protocol.models import LLMModel

            class DummyModel(LLMModel):
                def __init__(self):
                    super().__init__(model="dummy")

                def call_stream(self, request):
                    yield {"choices": [{"delta": {"content": "我在呀，"}}]}
                    yield {"choices": [{"delta": {"content": "嘿嘿"}, "finish_reason": "stop"}]}

            class DummyAgent:
                user_message_prefix = None

            events = []
            messages = [
                {"role": "assistant", "content": [{"type": "text", "text": "上一轮，嘿嘿"}]},
            ]
            executor = AgentStreamExecutor(
                agent=DummyAgent(),
                model=DummyModel(),
                system_prompt="",
                tools=[],
                on_event=events.append,
                messages=messages,
            )

            response = executor.run_stream("在吗")
        finally:
            logging.disable(logging.NOTSET)

        self.assertEqual(response, "我在呀")
        self.assertEqual(last_assistant_text(executor.messages), "我在呀")
        deltas = [
            event["data"]["delta"]
            for event in events
            if event.get("type") == "message_update"
        ]
        self.assertEqual(deltas, ["我在呀"])


if __name__ == "__main__":
    unittest.main()
