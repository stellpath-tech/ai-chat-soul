"""
agent.utils.kaomoji
====================
从 agent/utils/kaomojis.txt 加载颜文字，
按情感类型分类后加权随机抽取，每轮对话注入 RULE.md 的 {kaomoji_today} 占位符。
"""
from __future__ import annotations

import os
import re
import random
from typing import Dict, List, Optional

_KAOMOJI_FILE = os.path.join(os.path.dirname(__file__), "kaomojis.txt")
_BUCKETS_FILE = os.path.join(os.path.dirname(__file__), "kaomoji_buckets.json")

_RULES: Dict[str, List[str]] = {
    "软萌_温柔": [
        r"˘[ωᵕ˘]", r"ᵕ̣", r"꒳", r"⸝⸝", r"꒰", r"₍", r"ᐢ",
        r"[•͈].*ᴗ", r"ˊ˘ˋ", r"˘̩", r"ᵕ̈", r"꒱",
    ],
    "爱心_温暖": [
        r"♡", r"♥", r"❤", r"灬.*[♡♥]",
    ],
    "开心_活泼": [
        r"[≧≦].*▽", r"[≧]◡[≦]", r"◠‿◠", r"✧", r"ᗜ",
        r"[≧∇≦]", r"☆", r"★", r"🎉", r"🎊", r"🥳", r"✨",
    ],
    "难过_共情": [
        r"[TＴ][\-_][TＴ]", r"[╥╯]﹏[╥╰]", r"ಥ.ಥ", r"ノД", r"つД",
        r"´;[︵ω]", r"இ", r";w;", r"｡゚.*Д.*゚｡", r"´°̥.*ω.*°̥",
        r"\(;.*_;?\)", r"\( *; *ω *; *\)", r"\(´;", r"\(つ.*[Дд]",
    ],
    "萌猫_动物": [
        r"ฅ", r"=\^.*\^=", r"ﻌ", r"\^[・ω・]\^",
    ],
    "害羞_微妙": [
        r"[⁄/]{2,}.*ω", r">///<", r"⁄⁄.*⁄⁄",
    ],
    "活力_应援": [
        r"و", r"٩.*و", r"ง.*ง", r"۶",
    ],
}

_BLACKLIST: List[str] = [
    r"`Д´\)ノ", r"ꐦ", r"͡°.*͜ʖ", r"凸", r"\( *-_- *\)$",
]

_WEIGHTS: Dict[str, float] = {
    "软萌_温柔": 0.30,
    "爱心_温暖": 0.25,
    "开心_活泼": 0.20,
    "难过_共情": 0.12,
    "萌猫_动物": 0.08,
    "害羞_微妙": 0.03,
    "活力_应援": 0.02,
}

_buckets: Dict[str, List[str]] = {k: [] for k in _RULES}
_loaded = False
_rng = random.Random()


def _load():
    global _loaded
    if _loaded:
        return

    # 优先加载人工校准后的 JSON（由 import_kaomoji_from_review.py 生成）
    if os.path.exists(_BUCKETS_FILE):
        import json
        with open(_BUCKETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for cat in _buckets:
            _buckets[cat] = data.get(cat, [])
        _loaded = True
        return

    # 回退：从 kaomojis.txt 按正则分类
    try:
        with open(_KAOMOJI_FILE, encoding="utf-8") as f:
            all_km = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        _buckets["软萌_温柔"] = ["(ˊ˘ˋ*)", "꒰ᐢ. ̫.ᐢ꒱", "(⸝⸝ᵕᴗᵕ⸝⸝)"]
        _buckets["爱心_温暖"] = ["(｡•ᴗ•｡)♡", "(⺣◡⺣)♡"]
        _buckets["开心_活泼"] = ["(◍•ᴗ•◍)", "(≧◡≦)"]
        _buckets["难过_共情"] = ["(ᵕ̣̣̣̣̣̣﹏ᵕ̣̣̣̣̣̣)", "(っ˘̩╭╮˘̩)っ"]
        _buckets["萌猫_动物"] = ["ฅ^•ﻌ•^ฅ"]
        _buckets["害羞_微妙"] = ["(//ω//)"]
        _buckets["活力_应援"] = ["٩(◕‿◕｡)۶"]
        _loaded = True
        return

    def matches_any(s: str, patterns: List[str]) -> bool:
        return any(re.search(p, s) for p in patterns)

    for km in all_km:
        if matches_any(km, _BLACKLIST):
            continue
        for cat, patterns in _RULES.items():
            if matches_any(km, patterns):
                _buckets[cat].append(km)
                break

    _loaded = True


def kaomoji_library_str() -> str:
    """
    返回格式化后的完整颜文字库字符串（供注入 {kaomoji_today} 占位符）。
    每行一个类别：「类别名：km1 km2 km3 ...」
    """
    _load()
    lines = []
    for cat in _WEIGHTS:  # 按权重定义的顺序
        pool = _buckets.get(cat, [])
        if pool:
            lines.append(f"{cat}：{' '.join(pool)}")
    return "\n".join(lines)


def pick_kaomoji(no_prob: float = 0.50) -> str:
    """
    harness 随机决定本轮是否使用颜文字，返回注入 RULE.md {kaomoji_today} 的字符串：
    - 以 no_prob 的概率返回"（本轮不用颜文字）"
    - 否则返回完整颜文字库，让模型自行按情绪选择
    """
    if _rng.random() < no_prob:
        return "（本轮不用颜文字）"
    return kaomoji_library_str()
