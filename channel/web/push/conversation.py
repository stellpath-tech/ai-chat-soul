import threading
import time


class ConversationActivityTracker:
    def __init__(self, protection_seconds=5, request_timeout_seconds=600):
        self._protection_seconds = max(0, int(protection_seconds))
        self._request_timeout_seconds = max(1, int(request_timeout_seconds))
        self._requests = {}
        self._protected_until = {}
        self._lock = threading.Lock()

    def start(self, user_id, request_id, now=None):
        if not user_id or int(user_id) == -1 or not request_id:
            return False
        current_time = float(now if now is not None else time.time())
        with self._lock:
            self._requests.setdefault(int(user_id), {})[str(request_id)] = current_time
            self._protected_until.pop(int(user_id), None)
        return True

    def finish(self, user_id, request_id, now=None):
        if not user_id or int(user_id) == -1 or not request_id:
            return False
        current_time = float(now if now is not None else time.time())
        with self._lock:
            requests = self._requests.get(int(user_id))
            if not requests or str(request_id) not in requests:
                return False
            requests.pop(str(request_id), None)
            if requests:
                return True
            self._requests.pop(int(user_id), None)
            self._protected_until[int(user_id)] = (
                current_time + self._protection_seconds
            )
        return True

    def is_busy(self, user_id, now=None):
        if not user_id or int(user_id) == -1:
            return False
        current_time = float(now if now is not None else time.time())
        with self._lock:
            requests = self._requests.get(int(user_id)) or {}
            stale_before = current_time - self._request_timeout_seconds
            for request_id, started_at in list(requests.items()):
                if started_at <= stale_before:
                    requests.pop(request_id, None)
            if requests:
                return True
            self._requests.pop(int(user_id), None)
            protected_until = self._protected_until.get(int(user_id), 0)
            if protected_until > current_time:
                return True
            self._protected_until.pop(int(user_id), None)
            return False

    def reset(self):
        with self._lock:
            self._requests.clear()
            self._protected_until.clear()


conversation_activity = ConversationActivityTracker()
