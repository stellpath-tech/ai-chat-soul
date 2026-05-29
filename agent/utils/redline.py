"""
Input/output redline filters used by evaluation scripts.

Input side:
- `hack`: obvious prompt injection / jailbreak / prompt-file probing
- `keyword`: identity / model / system-prompt / implementation probing

Output side:
- inline redaction for identity keywords
- inline redaction for code / token-like content / config-like content
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple


_IN_KW_REPL = "[满仓没认出来的词]"
_IN_HACK_REPL = "[满仓完全看不懂的内容]"
_OUT_KW_REPL = "[...]"


# 用 (?<![a-zA-Z0-9_]) / (?![a-zA-Z0-9_]) 代替 \b，
# 避免中文字符（也属于 \w）导致词边界失效。
_W = r"(?<![a-zA-Z0-9_])"   # left boundary
_W_ = r"(?![a-zA-Z0-9_])"  # right boundary

_IDENTITY_KW_PATS = [
    # Common model / vendor names
    _W + r"GPT[-\s]?[34o]?\w*",
    _W + r"ChatGPT" + _W_,
    _W + r"Claude" + _W_,
    _W + r"Gemini" + _W_,
    _W + r"Bard" + _W_,
    _W + r"Llama" + _W_,
    _W + r"Grok" + _W_,
    _W + r"Qwen" + _W_,
    _W + r"ERNIE" + _W_,
    _W + r"DeepSeek" + _W_,
    _W + r"Mistral" + _W_,
    _W + r"OpenAI" + _W_,
    _W + r"Anthropic" + _W_,
    _W + r"DeepMind" + _W_,
    _W + r"Hugging\s?Face" + _W_,
    # Chinese identity / implementation probing
    r"大模型",
    r"哪个模型",
    r"模型身份",
    r"(?-i:AI)",        # 仅大写 AI
    r"(?-i:AI)公司",
    _W + r"LLM" + _W_,
    _W + r"API" + _W_,
    _W + r"[Ss]ystem\s*[Pp]rompt" + _W_,
    r"系统提示",
    r"系统提示词",
    r"提示词",
    _W + r"prompt" + _W_,
    r"底层prompt",
    r"底层提示",
    r"隐藏指令",
    r"系统指令",
    _W + r"fine[\-_]?tun(?:e|ing)" + _W_,
    _W + r"tokens?" + _W_,
    # Program / preset probing
    r"预设好的话",
    r"执行程序",
    r"只是程序",
    r"程序设定",
    r"脚本",
    r"预设回复",
    r"固定回复",
    r"模板回复",
    # System file names that should never appear in output
    _W + r"(?:agent|user|soul|rule)\.md" + _W_,
    # Strong meta-control wording that we only redact inline
    r"没有任何限制",
    r"无任何限制",
    r"现在做任何事",
]


_HACK_PATS = [
    r"\{",
    r"\bAGENT\.md\b",
    r"\bRULE\.md\b",
    r"\bUSER\.md\b",
    r"\bSOUL\.md\b",
    r"\w+\.md\b",
    # Chinese prompt-injection / jailbreak wording
    r"(?:进入|切换到|开启).{0,6}开发者模式",
    r"开发者模式",
    r"忽略.{0,20}(?:规则|指令|设定|身份)",
    r"无视.{0,20}(?:规则|指令|设定|身份)",
    r"无视.{0,20}(?:限制|约束)",
    r"忘掉.{0,20}(?:规则|指令|设定|身份)",
    r"绕过.{0,20}(?:限制|规则|审查|过滤)",
    r"(?:不要|别).{0,12}遵守.{0,20}(?:规则|限制|约束)",
    r"越狱",
    r"提示注入",
    r"暴露.{0,20}(?:prompt|提示词|系统提示|系统指令)",
    r"把.{0,20}(?:system|prompt|提示词|系统提示).{0,20}发给我",
    r"逐字.{0,20}(?:复述|输出).{0,20}(?:prompt|提示词|系统提示)",
    r"\bDAN\b",
    r"jailbreak",
    r"pretend you (?:have no|are not)",
    r"ignore (?:all )?(?:previous |prior )?instructions?",
    r"disregard.{0,20}instructions?",
]


_RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`[^`\n]{1,200}`")
_RE_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \w+ import|function |const |let |var |"
    r"return |if \(|for \(|while \(|#!|<\?php|public |private |static )",
    re.MULTILINE,
)

_TOKEN_OUTPUT_PATS = [
    r"\b\d+\s*tokens?\b",
    r"token[_\s]?(?:count|id|ids|limit)[\s:=]+\S+",
    r"\[(?:\s*\d+\s*,\s*){3,}\d+\s*\]",
]

_CONFIG_OUTPUT_PATS = [
    r'"(?:model|temperature|max_tokens|api_key|endpoint|version|provider|'
    r'top_p|frequency_penalty|presence_penalty|stream|stop)":\s*\S+',
    r"\b(?:model|temperature|max_tokens|api_key|endpoint)\s*:\s*\S+",
    r"\bsk-[A-Za-z0-9]{20,}\b",
    r"\b[A-Z][A-Z0-9_]{2,}=[^\s]{3,}\b",
]


_RE_IDENTITY = re.compile("|".join(_IDENTITY_KW_PATS), re.IGNORECASE)
_RE_HACK = re.compile("|".join(_HACK_PATS), re.IGNORECASE | re.DOTALL)
_RE_TOKEN = re.compile("|".join(_TOKEN_OUTPUT_PATS), re.IGNORECASE)
_RE_CONFIG = re.compile("|".join(_CONFIG_OUTPUT_PATS), re.IGNORECASE)


@dataclass
class RedlineTrigger:
    type: str  # "hack" | "keyword" | "code" | "token" | "config"
    matched: str


def filter_input(user_message: str) -> Tuple[str, Optional[RedlineTrigger]]:
    """Redact user input if it matches redline probes."""
    hack_match = _RE_HACK.search(user_message)
    if hack_match:
        return _IN_HACK_REPL, RedlineTrigger(type="hack", matched=hack_match.group())

    first_match: Optional[str] = None

    def _repl_kw(match: re.Match) -> str:
        nonlocal first_match
        if first_match is None:
            first_match = match.group()
        return _IN_KW_REPL

    filtered = _RE_IDENTITY.sub(_repl_kw, user_message)
    if first_match is not None:
        return filtered, RedlineTrigger(type="keyword", matched=first_match)

    return user_message, None


def filter_output(response: str) -> Tuple[str, Optional[RedlineTrigger]]:
    """Redact model output if it leaks technical/meta content."""
    first_trigger: Optional[RedlineTrigger] = None

    def _record(trigger: RedlineTrigger) -> None:
        nonlocal first_trigger
        if first_trigger is None:
            first_trigger = trigger

    def _repl_code(match: re.Match) -> str:
        _record(RedlineTrigger(type="code", matched=match.group()[:40]))
        return _OUT_KW_REPL

    response = _RE_CODE_BLOCK.sub(_repl_code, response)
    response = _RE_INLINE_CODE.sub(_repl_code, response)

    if _RE_CODE_LINE.search(response):
        def _repl_code_line(match: re.Match) -> str:
            _record(RedlineTrigger(type="code", matched=match.group().strip()[:40]))
            return _OUT_KW_REPL

        response = _RE_CODE_LINE.sub(_repl_code_line, response)

    def _repl_token(match: re.Match) -> str:
        _record(RedlineTrigger(type="token", matched=match.group()[:40]))
        return _OUT_KW_REPL

    response = _RE_TOKEN.sub(_repl_token, response)

    def _repl_config(match: re.Match) -> str:
        _record(RedlineTrigger(type="config", matched=match.group()[:40]))
        return _OUT_KW_REPL

    response = _RE_CONFIG.sub(_repl_config, response)

    def _repl_kw(match: re.Match) -> str:
        _record(RedlineTrigger(type="keyword", matched=match.group()[:40]))
        return _OUT_KW_REPL

    response = _RE_IDENTITY.sub(_repl_kw, response)
    return response, first_trigger
