"""
Session persistence store.
Saves per-user conversation history as JSON files under {workspace}/sessions/.
"""

import json
import os
import threading
from typing import List, Dict

from common.log import logger


class SessionStore:
    def __init__(self, workspace_root: str, max_turns: int = 80):
        self.sessions_dir = os.path.join(workspace_root, "sessions")
        self.max_turns = max_turns
        self._lock = threading.Lock()
        os.makedirs(self.sessions_dir, exist_ok=True)

    # ── public API ──────────────────────────────────────────────────────────

    def load(self, session_id: str) -> List[Dict]:
        path = self._path(session_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._truncate(data.get("messages", []))
        except Exception as e:
            logger.warning(f"[SessionStore] load failed ({session_id}): {e}")
            return []

    def save(self, session_id: str, messages: List[Dict]):
        path = self._path(session_id)
        truncated = self._truncate(messages)
        try:
            with self._lock:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"messages": truncated}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[SessionStore] save failed ({session_id}): {e}")

    def delete(self, session_id: str):
        path = self._path(session_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"[SessionStore] delete failed ({session_id}): {e}")

    # ── internals ───────────────────────────────────────────────────────────

    def _path(self, session_id: str) -> str:
        safe = session_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self.sessions_dir, f"{safe}.json")

    def _truncate(self, messages: List[Dict]) -> List[Dict]:
        """Keep only the last max_turns user turns (and their associated messages)."""
        user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if len(user_indices) <= self.max_turns:
            return messages
        cutoff = user_indices[-self.max_turns]
        return messages[cutoff:]
