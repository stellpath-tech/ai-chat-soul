"""Regression for rename refuse hard-guard: only name-targeted phrases force deny."""

import unittest

from agent.memory.thing_memory.extractor import (
    _REFUSE_PATTERN,
    _validate_rename_result,
)


class TestRenameRefuseGuard(unittest.TestCase):
    def test_mood_buhao_does_not_force_deny(self):
        """user50 case: accepting rename + comforting '心情不好' must stay bear."""
        reply = (
            "好呀～小满\n"
            "听起来像初夏刚灌浆的麦穗，饱满又安静\n"
            "宝宝喜欢这个称呼的话，以后满仓就是小满啦\n"
            "心情不好的时候，小满会一直在这里陪着宝宝\n"
            "不用急着好起来，慢慢来就好～"
        )
        self.assertIsNone(_REFUSE_PATTERN.search(reply))
        self.assertEqual(_validate_rename_result("bear", reply), "bear")

    def test_accept_with_confirm_keeps_bear(self):
        reply = "好呀，以后就叫你小满啦"
        self.assertEqual(_validate_rename_result("bear", reply), "bear")

    def test_name_targeted_refuse_phrases_force_deny(self):
        cases = [
            "唔……这个名字不好，换一个吧",
            "这个称呼不好听呢",
            "还是换个名字好不好",
            "还是换一个名字吧",
            "我不想叫这个名字",
            "我不愿意叫这个",
            "我不同意叫这个名字",
            "才不要这个名字",
            "哪有人叫这个呀",
        ]
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertIsNotNone(_REFUSE_PATTERN.search(reply))
                self.assertEqual(_validate_rename_result("bear", reply), "denied")
                self.assertEqual(_validate_rename_result("user", reply), "denied")

    def test_bare_buhao_buyao_no_longer_match(self):
        """Former broad tokens must not trip the guard alone."""
        for reply in (
            "心情不好的时候陪着你",
            "不要急着好起来",
            "不想说也没关系",
            "不行也没关系，慢慢来",
            "我不喜欢下雨天",
        ):
            with self.subTest(reply=reply):
                self.assertIsNone(_REFUSE_PATTERN.search(reply))
                self.assertEqual(_validate_rename_result("bear", reply), "bear")

    def test_llm_denied_still_trusted(self):
        self.assertEqual(_validate_rename_result("denied", "好呀小满"), "denied")


if __name__ == "__main__":
    unittest.main()
