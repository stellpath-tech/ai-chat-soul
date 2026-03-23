"""
测试 Session 系统提示词注入逻辑：
1. 无配置时，默认从 templates/AGENT.md 读取（满仓角色设定）
2. config.json 配置了 character_desc 时，优先使用配置值
3. 显式传入 system_prompt 时，优先级最高
"""

import os
import sys
import unittest
from unittest.mock import patch, mock_open, MagicMock, patch as patch_obj

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


MOCK_AGENT_TEMPLATE = "你是满仓，一个AI伴侣。"


def make_conf(character_desc=""):
    """构造一个模拟的 conf() 返回值"""
    mock = MagicMock()
    mock.get = lambda key, default=None: character_desc if key == "character_desc" else default
    return mock


class TestLoadAgentTemplate(unittest.TestCase):
    """测试 _load_agent_template()"""

    def test_reads_templates_agent_md(self):
        """正常情况下读取 templates/AGENT.md 内容"""
        from models.session_manager import _load_agent_template
        result = _load_agent_template()
        self.assertTrue(len(result) > 0, "templates/AGENT.md 内容不应为空")
        self.assertIn("满仓", result, "templates/AGENT.md 应包含满仓角色设定")

    def test_returns_empty_string_when_file_missing(self):
        """文件不存在时返回空字符串，不抛异常"""
        from models import session_manager
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = session_manager._load_agent_template()
        self.assertEqual(result, "")


class TestSessionSystemPrompt(unittest.TestCase):
    """测试 Session.__init__ 的 system_prompt 优先级"""

    def test_default_uses_agent_template(self):
        """未配置 character_desc 时，system_prompt 应为 templates/AGENT.md 内容"""
        import models.session_manager as sm
        with patch.object(sm, "conf", return_value=make_conf("")):
            session = sm.Session("user1")
        self.assertIn("满仓", session.system_prompt)
        self.assertNotIn("ChatGPT", session.system_prompt)
        self.assertNotIn("helpful assistant", session.system_prompt)

    def test_character_desc_overrides_template(self):
        """config 中配置了 character_desc 时，应优先使用"""
        import models.session_manager as sm
        custom_desc = "你是一个自定义角色。"
        with patch.object(sm, "conf", return_value=make_conf(custom_desc)):
            session = sm.Session("user2")
        self.assertEqual(session.system_prompt, custom_desc)

    def test_explicit_system_prompt_has_highest_priority(self):
        """显式传入 system_prompt 时，无论配置如何都应使用传入值"""
        import models.session_manager as sm
        explicit = "你是一个测试角色。"
        with patch.object(sm, "conf", return_value=make_conf("某个配置值")):
            session = sm.Session("user3", system_prompt=explicit)
        self.assertEqual(session.system_prompt, explicit)

    def test_no_hardcoded_chatgpt_identity(self):
        """默认情况下不应出现旧的 ChatGPT 硬编码身份"""
        import models.session_manager as sm
        with patch.object(sm, "conf", return_value=make_conf("")):
            session = sm.Session("user4")
        self.assertNotIn("ChatGPT", session.system_prompt)
        self.assertNotIn("OpenAI训练", session.system_prompt)


class TestTemplatesFileIntegrity(unittest.TestCase):
    """检查 templates/ 目录文件是否存在且包含关键内容"""

    def _templates_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

    def test_agent_md_exists(self):
        path = os.path.join(self._templates_dir(), "AGENT.md")
        self.assertTrue(os.path.exists(path), "templates/AGENT.md 不存在")

    def test_rule_md_exists(self):
        path = os.path.join(self._templates_dir(), "RULE.md")
        self.assertTrue(os.path.exists(path), "templates/RULE.md 不存在")

    def test_user_md_exists(self):
        path = os.path.join(self._templates_dir(), "USER.md")
        self.assertTrue(os.path.exists(path), "templates/USER.md 不存在")

    def test_agent_md_contains_role_definition(self):
        path = os.path.join(self._templates_dir(), "AGENT.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("满仓", content, "AGENT.md 应包含满仓角色定义")

    def test_rule_md_contains_safety_rules(self):
        path = os.path.join(self._templates_dir(), "RULE.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("满仓", content, "RULE.md 应包含满仓规则")


if __name__ == "__main__":
    unittest.main(verbosity=2)
