"""
Memory extractor

Triggers a lightweight LLM extraction every N turns to update MEMORY.md.
No agent tool-calling involved — writes the file directly in Python.
"""

import json
import os
from typing import Optional, List, Dict
from pathlib import Path
from datetime import datetime


_EXTRACTION_SYSTEM_PROMPT = """\
你是一个记忆提取助手，负责从对话中提取用户的关键信息并更新记忆文件。

你的任务是从给定的对话历史中提取以下三类信息：
1. **身份信息**：用户的姓名、职业、年龄、所在城市等基本身份信息
2. **偏好信息**：用户喜欢/不喜欢的事物、习惯、口味等
3. **关键事件**：用户提到的重要事件、经历、情绪转折点等

要求：
- 只提取对话中实际出现的信息，不要编造或推断
- 每条信息简洁，控制在20字以内
- 如果某类信息为空，返回空列表
- 返回 JSON，格式如下：
{
  "identity": ["姓名：小雨", "职业：学生"],
  "preferences": ["喜欢草莓蛋糕", "有一只橘猫叫奶茶"],
  "key_events": ["2024-01 考试没考好，复习很久但结果不理想"]
}
"""

_EXTRACTION_USER_TEMPLATE = """\
请从以下最近的对话历史中提取关键信息：

{conversation}

返回 JSON，不要其他内容。
"""


class MemoryFlushManager:
    """
    Manages periodic memory extraction every N conversation turns.

    Every `turn_threshold` turns, extracts identity/preferences/key_events
    from recent conversation via a direct LLM API call, then writes the
    result into MEMORY.md in the workspace directory.
    """

    def __init__(
        self,
        workspace_dir: Path,
        llm_model=None,  # unused, kept for interface compatibility
    ):
        self.workspace_dir = workspace_dir
        self.memory_dir = workspace_dir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.last_flush_token_count: Optional[int] = None
        self.last_flush_timestamp: Optional[datetime] = None
        self.turn_count: int = 0

    # ── trigger logic ────────────────────────────────────────────────────────

    def should_flush(
        self,
        current_tokens: int = 0,
        token_threshold: int = 50000,
        turn_threshold: int = 10,  # 10 turns default
    ) -> bool:
        if current_tokens > 0 and current_tokens >= token_threshold:
            if self.last_flush_token_count is not None:
                if current_tokens <= self.last_flush_token_count + 5000:
                    return False
            return True
        if self.turn_count >= turn_threshold:
            return True
        return False

    def increment_turn(self):
        self.turn_count += 1

    # ── path helpers ─────────────────────────────────────────────────────────

    def get_today_memory_file(self, user_id: Optional[str] = None) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        if user_id:
            d = self.memory_dir / "users" / user_id
            d.mkdir(parents=True, exist_ok=True)
            return d / f"{today}.md"
        return self.memory_dir / f"{today}.md"

    def get_main_memory_file(self, user_id: Optional[str] = None) -> Path:
        if user_id:
            d = self.memory_dir / "users" / user_id
            d.mkdir(parents=True, exist_ok=True)
            return d / "MEMORY.md"
        return Path(self.workspace_dir) / "MEMORY.md"

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _call_llm_extract(self, conversation_text: str) -> Optional[Dict]:
        """
        Call LLM via OpenAI-compatible chat completions to extract memories.
        Returns parsed dict or None on failure.
        """
        import requests

        api_key = os.environ.get("OPENAI_API_KEY", "")
        api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
        model = os.environ.get("MEMORY_EXTRACT_MODEL", "gpt-4o-mini")

        if not api_key or api_key in ("", "YOUR API KEY", "YOUR_API_KEY"):
            from common.log import logger
            logger.warning("[MemoryExtractor] No OPENAI_API_KEY, skipping extraction")
            return None

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": _EXTRACTION_USER_TEMPLATE.format(
                    conversation=conversation_text
                )},
            ],
            "temperature": 0.0,
            "max_tokens": 800,
        }

        try:
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content)
        except Exception as e:
            from common.log import logger
            logger.warning(f"[MemoryExtractor] LLM call failed: {e}")
            return None

    # ── MEMORY.md update ─────────────────────────────────────────────────────

    def _update_memory_md(self, extracted: Dict, memory_path: Path):
        """
        Merge extracted memories into MEMORY.md.

        The file is structured as three sections; we append new items that
        are not already present (simple string-match dedup).
        """
        identity_new = extracted.get("identity", [])
        prefs_new = extracted.get("preferences", [])
        events_new = extracted.get("key_events", [])

        if not any([identity_new, prefs_new, events_new]):
            return  # nothing to write

        # Read existing content
        existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

        def _merge_section(header: str, new_items: List[str], text: str) -> str:
            """Append new_items under header, skipping duplicates."""
            if not new_items:
                return text
            # Ensure header exists
            if header not in text:
                text = text.rstrip() + f"\n\n## {header}\n"
            for item in new_items:
                item = item.strip()
                if item and item not in text:
                    # Insert before next ## header or at end of section
                    idx = text.find(f"\n## ", text.find(f"## {header}") + 1)
                    entry = f"- {item}\n"
                    if idx == -1:
                        text = text.rstrip() + "\n" + entry
                    else:
                        text = text[:idx] + entry + text[idx:]
            return text

        result = existing
        result = _merge_section("身份信息", identity_new, result)
        result = _merge_section("偏好与习惯", prefs_new, result)
        result = _merge_section("关键事件", events_new, result)

        memory_path.write_text(result, encoding="utf-8")

    # ── main entry point ─────────────────────────────────────────────────────

    async def execute_flush(
        self,
        messages: List[Dict],
        current_tokens: int = 0,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        Extract memories from recent conversation and write to MEMORY.md.

        Args:
            messages: Recent conversation messages (list of {role, content})
            current_tokens: Current token count (for tracking)
            user_id: Optional user ID for per-user memory files

        Returns:
            True if extraction completed successfully
        """
        from common.log import logger

        try:
            # Build plain-text conversation for the LLM
            lines = []
            for msg in messages[-20:]:  # last 20 messages (~10 turns)
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Extract text parts from structured content
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if role in ("user", "assistant") and content.strip():
                    label = "用户" if role == "user" else "满仓"
                    lines.append(f"{label}: {content.strip()}")

            if not lines:
                return False

            conversation_text = "\n".join(lines)
            extracted = self._call_llm_extract(conversation_text)

            if extracted:
                memory_path = self.get_main_memory_file(user_id)
                self._update_memory_md(extracted, memory_path)
                logger.info(
                    f"[MemoryExtractor] Updated MEMORY.md: "
                    f"identity={len(extracted.get('identity', []))}, "
                    f"prefs={len(extracted.get('preferences', []))}, "
                    f"events={len(extracted.get('key_events', []))}"
                )

            self.last_flush_token_count = current_tokens
            self.last_flush_timestamp = datetime.now()
            self.turn_count = 0
            return True

        except Exception as e:
            from common.log import logger
            logger.warning(f"[MemoryExtractor] Extraction failed: {e}")
            return False

    def get_status(self) -> dict:
        return {
            "last_flush_tokens": self.last_flush_token_count,
            "last_flush_time": self.last_flush_timestamp.isoformat() if self.last_flush_timestamp else None,
            "turn_count": self.turn_count,
            "today_file": str(self.get_today_memory_file()),
            "main_file": str(self.get_main_memory_file()),
        }


def create_memory_files_if_needed(workspace_dir: Path, user_id: Optional[str] = None):
    """Create default memory files if they don't exist"""
    memory_dir = workspace_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    if user_id:
        user_dir = memory_dir / "users" / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        main_memory = user_dir / "MEMORY.md"
    else:
        main_memory = Path(workspace_dir) / "MEMORY.md"

    if not main_memory.exists():
        main_memory.write_text("", encoding="utf-8")
