"""
agent.utils.style
=================
每轮对话随机选取一个风格模式标签，注入 RULE.md 的 {style_today} 占位符。

风格定义来自 SFT v2 训练数据标注体系：
  M2 极简陪伴型   — 极短回复，留白为主
  M4 谷仓场景型   — 用谷仓/烘焙意象具象化情绪
  M6 好奇追问型   — 结尾提一个真心想知道的问题
  M8 身体反应型   — 用饼干身体细节（耳朵/裂缝等）传递感受
  M7（无标签）    — 通用模式，不附加提示
"""

import random

_rng = random.Random()

# 风格标签 → 注入文本
STYLE_LABELS: dict[str, str] = {
    "M2": "[酌情采用极简陪伴风格]",
    "M4": "[酌情采用谷仓场景风格]",
    "M6": "[酌情采用好奇追问风格]",
    "M8": "[酌情采用身体反应风格]",
}

# 各风格被随机选中的相对权重（M7=不注入，权重最高保持自然多样性）
_WEIGHTS: dict[str, float] = {
    "M2": 0.10,
    "M4": 0.18,
    "M6": 0.15,
    "M8": 0.12,
    "M7": 0.45,   # 不注入任何标签，走通用路径
}

_MODES   = list(_WEIGHTS.keys())
_W_LIST  = [_WEIGHTS[m] for m in _MODES]


def pick_style() -> str:
    """
    随机选取本轮风格，返回注入 RULE.md {style_today} 的字符串。
    - M7 返回空字符串（不附加任何提示）
    - 其他模式返回对应的风格标签
    """
    mode = _rng.choices(_MODES, weights=_W_LIST, k=1)[0]
    return STYLE_LABELS.get(mode, "")
