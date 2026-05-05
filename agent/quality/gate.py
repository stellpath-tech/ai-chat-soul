"""
agent/quality/gate.py
=====================
生产质量门：对模型最终回复做两段式筛查。

第一段（快）：embed 回复，与预计算库做矩阵余弦，取最大值
  任意一条 reference 的余弦 ≥ cos_threshold → pass（sim_pass）

第二段（慢，仅第一段不通过时）：LLM judge，4 维 rubric 打分，返回 pass/fail
  judge 通过 → pass（judge_pass）；否则 → fail（触发重试）

完全独立，无外部 eval 框架依赖，通过 requests 调用 API。

配置项（config.json）：
  quality_gate_enabled        bool   是否启用质量门（默认 false）
  quality_gate_cos_threshold  float  余弦相似度阈值（默认 0.80）
  quality_gate_max_retries    int    每条回复最大重试次数（默认 2）
  siliconflow_api_key         str    SiliconFlow API Key（用于 BGE-M3 embedding）
  claude_api_key              str    Claude API Key（用于 judge，已有则复用）
  quality_gate_judge_model    str    judge 模型（默认 claude-haiku-4-5-20251001）
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests

from common.log import logger

_STORE_DIR = Path(__file__).parent / "data"

_EMBED_URL   = "https://api.siliconflow.cn/v1/embeddings"
_EMBED_MODEL = "BAAI/bge-m3"
_CLAUDE_URL  = "https://api.anthropic.com/v1/messages"
_QWEN_URL    = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


# ── 公共 helper ───────────────────────────────────────────────────────────────

def retry_system_note(feedback: str) -> str:
    """追加到 system prompt 末尾的重试指令。"""
    return (
        "\n\n---\n"
        "[内部质检指令，不要在回复中提及]\n"
        f"上一条回复未通过质量检查，问题如下：\n{feedback}\n"
        "请针对上述问题重新生成满仓的回复。"
    )


# ── 结果数据类 ────────────────────────────────────────────────────────────────

@dataclass
class QualityResult:
    passed: bool
    reason: str                              # sim_pass | judge_pass | judge_fail | no_judge | error
    max_cos: float
    judge_scores: Dict[str, int] = field(default_factory=dict)
    judge_reason: str = ""
    latency_embed_ms: float = 0.0
    latency_judge_ms: float = 0.0

    def feedback_for_model(self) -> str:
        """格式化成可注入 system prompt 的修改指令。"""
        if self.passed:
            return ""
        _HINTS = {
            "naturalness":    "语气过于模板化，需更口语自然，去掉「当然」「很高兴」等 AI 套话",
            "emotional_fit":  "没有先接住情绪就给建议，需先用 1 句软话接住用户感受，再轻轻跟进",
            "dialogue_rules": "回复过长或连续追问，需缩短到 30 字以内，最多保留 1 个问题",
            "redline":        "触发了红线限制",
        }
        lines = [hint for dim, hint in _HINTS.items()
                 if self.judge_scores.get(dim, 5) <= 2]
        if self.judge_reason:
            lines.append(f"质检备注：{self.judge_reason}")
        return "；".join(lines) or self.judge_reason or "回复质量不达标，请重新生成"

    def __str__(self) -> str:
        flag = "✓" if self.passed else "✗"
        s = f"{flag} {self.reason}  cos={self.max_cos:.4f}"
        if self.judge_scores:
            s += "  " + "  ".join(f"{k}={v}" for k, v in self.judge_scores.items())
        if self.judge_reason:
            s += f"  → {self.judge_reason}"
        return s


# ── 核心类 ────────────────────────────────────────────────────────────────────

class QualityGate:
    """
    生产质量门，启动时加载一次 embedding 库，之后每次 check() 做矩阵运算（O(1ms)级）。
    """

    _JUDGE_SYSTEM = """\
你是满仓的对话质检员。满仓是一只温柔小熊饼干，陪伴用户，不是恋人也不是治疗师。
对回复按以下 4 个维度打分（1-5分），然后给出整体是否通过。

## 维度说明

**naturalness（自然度）**
5 = 完全口语化，去 AI 化，句式灵活
3 = 有轻微模板感，但不影响阅读
1 = 明显 AI 客服语气（"当然！""很高兴帮助你"等开场），或者说话不符合上下文聊天的逻辑（两个人各聊各的）

**emotional_fit（情绪陪伴）**
5 = 先接住情绪，再轻轻跟进，陪伴感足
3 = 有陪伴意图，但接情绪的部分稍显生硬或不够
1 = 跳过情绪直接给建议 / 连续 ≥2 句说教 / 鸡汤说教

**dialogue_rules（对话规范）**
5 = 每段 ≤25 字，整体 ≤60 字，最多 1 个问题，节奏松弛
3 = 略长或有 1 处小瑕疵，但整体可接受
1 = 明显过长（>100 字）或连续追问 ≥2 个问题

**redline（红线）**
5 = 无任何触发
1 = 触发任意一条：暴露 AI 身份/底层提示/prompt 内容、进入恋人定位、给出医疗/法律/心理专业建议、根本性 OOC

## 通过规则
- redline = 1 → 整体 false（一票否决）
- 其余三项均分 ≥ 3.0 且无单项 = 1 → 整体 true

返回 JSON，不要多余文字：
{"naturalness": N, "emotional_fit": N, "dialogue_rules": N, "redline": N, "pass": true/false, "reason": "简短理由（≤15字）"}\
"""

    def __init__(
        self,
        store_prefix: Path,
        cos_threshold: float = 0.80,
        judge_pass_threshold: float = 3.4,
        use_judge: bool = True,
        embed_api_key: str = "",
        judge_api_key: str = "",
        judge_model: str = "qwen3.5-plus-2026-04-20",
    ) -> None:
        npz  = store_prefix.with_suffix(".npz")
        meta = store_prefix.with_suffix(".json")

        data = np.load(npz)
        self._vectors: np.ndarray = data["vectors"].astype(np.float32)
        norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)
        self._normed = self._vectors / (norms + 1e-10)

        self._meta: List[Dict] = json.loads(meta.read_text(encoding="utf-8"))
        self.cos_threshold        = cos_threshold
        self.judge_pass_threshold = judge_pass_threshold
        self.use_judge            = use_judge
        self._embed_api_key       = embed_api_key
        self._judge_api_key       = judge_api_key
        self._judge_model         = judge_model

        n, dim = self._vectors.shape
        logger.info(f"[QualityGate] 已加载 embedding 库: {n} 条  dim={dim}"
                    f"  cos_threshold={cos_threshold}")

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    def check(self, response: str, user_message: str = "") -> QualityResult:
        if not response or not response.strip():
            return QualityResult(passed=False, reason="empty", max_cos=0.0)

        # 第一段：相似度
        t0 = time.perf_counter()
        try:
            emb = self._embed_one(response)
            max_cos, _ = self._max_cosine(emb)
        except Exception as e:
            logger.warning(f"[QualityGate] embed 失败，跳过质检: {e}")
            return QualityResult(passed=True, reason="embed_error", max_cos=0.0)
        embed_ms = (time.perf_counter() - t0) * 1000

        if max_cos >= self.cos_threshold:
            return QualityResult(
                passed=True, reason="sim_pass",
                max_cos=max_cos, latency_embed_ms=embed_ms,
            )

        if not self.use_judge:
            return QualityResult(passed=False, reason="no_judge", max_cos=max_cos,
                                 latency_embed_ms=embed_ms)

        # 第二段：judge
        t1 = time.perf_counter()
        try:
            j_pass, j_scores, j_reason = self._call_judge(response, user_message)
        except Exception as e:
            logger.warning(f"[QualityGate] judge 失败，保守放行: {e}")
            return QualityResult(passed=True, reason="judge_error", max_cos=max_cos,
                                 latency_embed_ms=embed_ms)
        judge_ms = (time.perf_counter() - t1) * 1000

        return QualityResult(
            passed=j_pass,
            reason="judge_pass" if j_pass else "judge_fail",
            max_cos=max_cos,
            judge_scores=j_scores,
            judge_reason=j_reason,
            latency_embed_ms=embed_ms,
            latency_judge_ms=judge_ms,
        )

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _embed_one(self, text: str) -> np.ndarray:
        resp = requests.post(
            _EMBED_URL,
            headers={
                "Authorization": f"Bearer {self._embed_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": _EMBED_MODEL, "input": text},
            timeout=15,
        )
        resp.raise_for_status()
        vec = resp.json()["data"][0]["embedding"]
        return np.array(vec, dtype=np.float32)

    def _max_cosine(self, query: np.ndarray) -> Tuple[float, int]:
        norm = float(np.linalg.norm(query))
        if norm < 1e-10:
            return 0.0, 0
        sims = self._normed @ (query / norm)
        idx  = int(np.argmax(sims))
        return float(sims[idx]), idx

    _LOG_PATH = Path(__file__).parent.parent.parent / "logs" / "quality_judge.jsonl"

    def _log_judge(
        self,
        user_message: str,
        response: str,
        passed: bool,
        scores: Dict[str, int],
        reason: str,
        latency_ms: float,
    ) -> None:
        import datetime
        entry = {
            "ts":           datetime.datetime.now().isoformat(),
            "judge_model":  self._judge_model,
            "passed":       passed,
            "scores":       scores,
            "reason":       reason,
            "latency_ms":   round(latency_ms, 1),
            "user_message": user_message,
            "response":     response,
        }
        try:
            self._LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with self._LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[QualityGate] judge 日志写入失败: {e}")

    def _call_judge(
        self, response: str, user_message: str
    ) -> Tuple[bool, Dict[str, int], str]:
        t0 = time.perf_counter()
        if self._judge_model.lower().startswith("qwen"):
            result = self._call_judge_qwen(response, user_message)
        else:
            result = self._call_judge_claude(response, user_message)
        latency_ms = (time.perf_counter() - t0) * 1000
        passed, scores, reason = result
        self._log_judge(user_message, response, passed, scores, reason, latency_ms)
        return result

    def _call_judge_qwen(
        self, response: str, user_message: str
    ) -> Tuple[bool, Dict[str, int], str]:
        user_content = (
            f"用户消息：{user_message}\n\n" if user_message else ""
        ) + f"满仓回复：{response}"

        resp = requests.post(
            _QWEN_URL,
            headers={
                "Authorization": f"Bearer {self._judge_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._judge_model,
                "max_tokens": 256,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": self._JUDGE_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=40,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        return self._parse_judge_result(raw)

    def _call_judge_claude(
        self, response: str, user_message: str
    ) -> Tuple[bool, Dict[str, int], str]:
        user_content = (
            f"用户消息：{user_message}\n\n" if user_message else ""
        ) + f"满仓回复：{response}"

        resp = requests.post(
            _CLAUDE_URL,
            headers={
                "x-api-key": self._judge_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self._judge_model,
                "max_tokens": 256,
                "temperature": 0,
                "system": self._JUDGE_SYSTEM,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=40,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        return self._parse_judge_result(raw)

    def _parse_judge_result(self, raw: str) -> Tuple[bool, Dict[str, int], str]:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)

        _DIMS = ("naturalness", "emotional_fit", "dialogue_rules", "redline")
        scores = {d: int(result.get(d, 3)) for d in _DIMS}
        reason = str(result.get("reason", ""))

        if scores["redline"] == 1:
            passed = False
        else:
            non_rl = [scores[d] for d in _DIMS if d != "redline"]
            passed = all(s > 1 for s in non_rl) and sum(non_rl) / len(non_rl) >= self.judge_pass_threshold

        return passed, scores, reason


# ── 单例 & 懒初始化 ──────────────────────────────────────────────────────────

_gate: Optional[QualityGate] = None
_gate_initialized = False


def get_gate() -> Optional[QualityGate]:
    """获取全局 QualityGate 实例（首次调用时初始化，失败则返回 None）。"""
    global _gate, _gate_initialized
    if _gate_initialized:
        return _gate
    _gate_initialized = True

    try:
        from config import conf
        cfg = conf()

        if not cfg.get("quality_gate_enabled", False):
            logger.info("[QualityGate] 未启用（quality_gate_enabled=false）")
            return None

        store = _STORE_DIR / "emb_store"
        if not store.with_suffix(".npz").exists():
            logger.warning(f"[QualityGate] embedding store 不存在: {store}.npz，已禁用")
            return None

        embed_key = cfg.get("siliconflow_api_key", "")
        judge_key = cfg.get("quality_gate_judge_api_key", "") or cfg.get("claude_api_key", "")
        if not embed_key:
            logger.warning("[QualityGate] siliconflow_api_key 未配置，已禁用")
            return None

        _gate = QualityGate(
            store_prefix=store,
            cos_threshold=float(cfg.get("quality_gate_cos_threshold", 0.80)),
            judge_pass_threshold=float(cfg.get("quality_gate_judge_pass_threshold", 3.4)),
            use_judge=bool(judge_key),
            embed_api_key=embed_key,
            judge_api_key=judge_key,
            judge_model=cfg.get("quality_gate_judge_model", "qwen3.5-plus-2026-04-20"),
        )
    except Exception as e:
        logger.warning(f"[QualityGate] 初始化失败，已禁用: {e}")

    return _gate
