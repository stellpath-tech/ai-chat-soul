import sys
import time
import web
import json
import uuid
import base64
import tempfile
import sqlite3
from collections import deque
from queue import Queue, Empty
from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from common.log import logger
from common.singleton import singleton
from config import conf
import os
import mimetypes
import threading
import logging
from datetime import datetime, timedelta, timezone
import channel.web.database as db
from channel.web.metrics import MetricsHandler, metrics_processor, USER_AUTH_TOTAL
from channel.web.push.service import (
    PushDeviceRequestError,
    PushTestDeliveryError,
    PushTestDeviceNotRegisteredError,
    PushTestRequestError,
    get_authenticated_user_push_card,
    register_authenticated_user_push_device,
    send_authenticated_user_push_test,
    unregister_authenticated_user_push_device,
)
from channel.web.push.contracts import (
    PushContentImageRequestError,
    PushContentMutation,
    PushContentRequestError,
    UserActivityReport,
    UserActivityRequestError,
)
from channel.web.push import assets as push_assets
from channel.web.push import repository as push_repository
from channel.web.diary.styles import is_valid_diary_image_style

try:
    from common import event_log
except Exception:
    class _NoopEventLog:
        @staticmethod
        def log(*args, **kwargs):
            pass
    event_log = _NoopEventLog()

COMPLAINT_ADMIN_PASSCODE = "320e0ec38f2cc0e2ea9697a52693b1c44089f7b017e0540125bdfffa03bf298e"
REPAIR_STATUSES = {"需要修复", "已修复", "无需修复"}
COMPLAINT_FILTER_STATUSES = REPAIR_STATUSES | {"未标记"}


def _api_response(success, message, data=None, **extra):
    body = {"success": success, "message": message, "data": data}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def _auth_user():
    auth_token = web.ctx.env.get("HTTP_X_AUTH_TOKEN", "").strip()
    user = db.get_active_user_by_token(auth_token)
    if not user:
        return None
    return user


def _admin_passcode_from_request():
    header_passcode = web.ctx.env.get("HTTP_X_ADMIN_PASSCODE", "").strip()
    if header_passcode:
        return header_passcode
    try:
        params = web.input(passcode="")
        if params.passcode:
            return params.passcode.strip()
    except Exception:
        pass
    try:
        data = json.loads(web.data() or b"{}")
        return str(data.get("passcode") or "").strip()
    except Exception:
        return ""


def _admin_authorized():
    return _admin_passcode_from_request() == COMPLAINT_ADMIN_PASSCODE


def _require_admin():
    if _admin_authorized():
        return None
    web.ctx.status = '401 Unauthorized'
    return _api_response(False, "unauthorized", None)


def _optional_boolean_query(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    raise ValueError("invalid enabled")



class WebMessage(ChatMessage):
    def __init__(
        self,
        msg_id,
        content,
        ctype=ContextType.TEXT,
        from_user_id="User",
        to_user_id="Chatgpt",
        other_user_id="Chatgpt",
    ):
        self.msg_id = msg_id
        self.ctype = ctype
        self.content = content
        self.from_user_id = from_user_id
        self.to_user_id = to_user_id
        self.other_user_id = other_user_id


@singleton
class WebChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]
    _instance = None
    
    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        super().__init__()
        self.msg_id_counter = 0
        self.session_queues = {}       # session_id -> Queue (fallback polling)
        self.request_to_session = {}   # request_id -> session_id
        self.sse_queues = {}           # request_id -> Queue (SSE streaming)
        self._sse_created_at = {}       # request_id -> float creation ts (cleanup)
        self._http_server = None
        # 设备注册中心：device_id -> {lastSeen: float}
        self.device_registry = {}
        self._device_registry_lock = threading.Lock()
        self._registry_file = None   # 启动后由 startup() 确定持久化路径
        # 聊天记录队列：device_id -> deque of Message dicts（DEVICE 来源的消息，最多 100 条）
        self.chatlog_queues = {}       # device_id -> deque(maxlen=100)
        self._chatlog_lock = threading.Lock()
        # 宠物设备事件队列：device_id -> deque，每条独立，最多 50 条，超出时丢弃最旧
        self.pet_event_queues = {}     # device_id -> deque of event dict
        self._pet_event_lock = threading.Lock()
        self._pet_event_max = 50
        self._account_cleanup_started = False
        self._diary_worker_started = False
        self._proactive_push_worker_started = False


    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        return str(uuid.uuid4())

    def send(self, reply: Reply, context: Context):
        try:
            if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                logger.warning(f"Web channel doesn't support {reply.type} yet")
                return

            if reply.type == ReplyType.IMAGE_URL:
                time.sleep(0.5)

            request_id = context.get("request_id", None)
            if not request_id:
                logger.error("No request_id found in context, cannot send message")
                return

            reply_mode = context.get("reply_mode")
            _nc_value = (context.get("name_changed_holder") or {}).get("value")
            _start_time = context.get("llm_start_time")
            _latency_ms = int((time.time() - _start_time) * 1000) if _start_time else None
            event_log.log(
                "llm_done",
                request_id=request_id,
                user_id=context.get("user_id", -1),
                user_group=context.get("user_group", -1),
                phone_number=context.get("phone_number", ""),
                session_id=context.get("session_id", ""),
                device_id=context.get("device_id", ""),
                source=context.get("source", ""),
                reply_type=str(reply.type) if reply.type else None,
                reply_content=reply.content if reply.content is not None else "",
                reply_mode=reply_mode,
                latency_ms=_latency_ms,
            )

            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return

            # 如果是 DEVICE 来源的请求，将 AI 回复写入聊天记录队列
            source = context.get("source", "APP")
            device_id = context.get("device_id", "")
            if source == "DEVICE" and device_id and reply.content:
                self._push_chatlog(device_id, "assistant", reply.content)
            message_id = None
            if context.get("user_id", -1) != -1 and reply.content and not context.get("change_settings"):
                message_id = db.append_chat_message(
                    context.get("user_id"),
                    context.get("session_id", ""),
                    "assistant",
                    str(reply.content),
                    "text",
                    source,
                    request_id,
                )

            # SSE mode: push done event to SSE queue
            if request_id in self.sse_queues:
                content = reply.content if reply.content is not None else ""
                self.sse_queues[request_id].put({
                    "type": "done",
                    "content": content,
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "message_id": message_id,
                    "reply_mode": reply_mode,
                    "nameChangedSuccess": _nc_value,
                })
                logger.debug(f"SSE done sent for request {request_id}")
                return

            # Fallback: polling mode
            if session_id in self.session_queues:
                response_data = {
                    "type": str(reply.type),
                    "content": reply.content,
                    "timestamp": time.time(),
                    "request_id": request_id,
                    "reply_mode": reply_mode,
                    "nameChangedSuccess": _nc_value,
                }
                self.session_queues[session_id].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.exception(f"Error in send method: {e}")
            # Without this, a failure to push the reply back leaves the client
            # hanging and there is no llm_done in the logs to explain why.
            event_log.log_exception(
                "web_send_failed",
                e,
                request_id=context.get("request_id", "") if context else "",
                user_id=context.get("user_id", -1) if context else -1,
                phone_number=context.get("phone_number", "") if context else "",
                session_id=context.get("session_id", "") if context else "",
                reply_type=str(reply.type) if reply and reply.type else "",
            )

    def _produce_with_logging(self, context):
        """Wrap produce() — this only catches enqueue failures.
        Real LLM/tool/bridge errors are logged downstream in _handle and
        the agent stack via llm_call_failed / agent_failed / tool_exception."""
        request_id = context.get("request_id", "")
        try:
            self.produce(context)
        except Exception as e:
            if context.get("proactive_conversation_tracked"):
                from channel.web.push.conversation import conversation_activity
                conversation_activity.finish(
                    context.get("user_id"), context.get("request_id")
                )
            event_log.log_exception(
                "produce_failed",
                e,
                request_id=request_id,
                user_id=context.get("user_id", -1),
                user_group=context.get("user_group", -1),
                phone_number=context.get("phone_number", ""),
                session_id=context.get("session_id", ""),
                device_id=context.get("device_id", ""),
                source=context.get("source", ""),
            )
            logger.exception(f"[WebChannel] produce failed for request {request_id}: {e}")

    def _track_proactive_conversation(self, context):
        from channel.web.push.conversation import conversation_activity
        tracked = conversation_activity.start(
            context.get("user_id"), context.get("request_id")
        )
        context["proactive_conversation_tracked"] = tracked

    def _make_sse_callback(
        self,
        request_id: str,
        log_ctx: dict = None,
        reply_mode=None,
        name_changed_holder: dict = None,
    ):
        """Build an on_event callback that pushes agent stream events into the SSE queue."""
        log_ctx = log_ctx or {}

        def on_event(event: dict):
            if request_id not in self.sse_queues:
                return
            q = self.sse_queues[request_id]
            event_type = event.get("type")
            data = event.get("data", {})
            _nc = (name_changed_holder or {}).get("value")

            if event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    q.put({
                        "type": "delta",
                        "content": delta,
                        "reply_mode": reply_mode,
                        "nameChangedSuccess": _nc,
                    })

            elif event_type == "tool_execution_start":
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                q.put({
                    "type": "tool_start",
                    "tool": tool_name,
                    "arguments": arguments,
                    "reply_mode": reply_mode,
                    "nameChangedSuccess": _nc,
                })

            elif event_type == "tool_execution_end":
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                arguments = data.get("arguments", {})
                exec_time = data.get("execution_time", 0)
                # Truncate long results to avoid huge SSE payloads
                result_str = str(result)
                if len(result_str) > 2000:
                    sse_result = result_str[:2000] + "…"
                else:
                    sse_result = result_str
                q.put({
                    "type": "tool_end",
                    "tool": tool_name,
                    "status": status,
                    "result": sse_result,
                    "execution_time": round(exec_time, 2),
                    "reply_mode": reply_mode,
                    "nameChangedSuccess": _nc,
                })
                event_log.log(
                    "tool_call",
                    request_id=request_id,
                    tool=tool_name,
                    status=status,
                    arguments=arguments,
                    result=result_str,
                    latency_ms=int(exec_time * 1000),
                    **log_ctx,
                )

        return on_event

    def _ensure_sse_queue(self, request_id: str):
        """Create (or reuse) the SSE queue for request_id; sweep stale unconnected queues."""
        now = time.time()
        stale = [
            rid for rid, created in list(getattr(self, "_sse_created_at", {}).items())
            if now - created > 300 and rid in self.sse_queues
        ]
        for rid in stale:
            self.sse_queues.pop(rid, None)
            self._sse_created_at.pop(rid, None)
            logger.debug(f"[WebChannel] cleaned stale SSE queue {rid}")
        if request_id not in self.sse_queues:
            self.sse_queues[request_id] = Queue()
            self._sse_created_at[request_id] = now
        return self.sse_queues[request_id]

    def _save_image_from_b64(self, b64_data: str, image_type: str = "jpeg") -> str:
        """将 base64 图片解码并保存为临时文件，返回文件路径；失败返回空字符串"""
        try:
            # 兼容 data:image/jpeg;base64,<data> 格式
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            image_bytes = base64.b64decode(b64_data)
            suffix = f".{image_type.lstrip('.')}"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="webchan_img_") as f:
                f.write(image_bytes)
                return f.name
        except Exception as e:
            logger.error(f"[WebChannel] Failed to decode image: {e}")
            event_log.log_exception(
                "image_decode_failed",
                e,
                image_type=image_type,
                b64_len=len(b64_data) if b64_data else 0,
            )
            return ""

    def _push_chatlog(self, device_id: str, role: str, content: str):
        """将一条消息写入指定设备的聊天记录队列（环形缓冲，最多保留 100 条）"""
        with self._chatlog_lock:
            if device_id not in self.chatlog_queues:
                self.chatlog_queues[device_id] = deque(maxlen=100)
            self.chatlog_queues[device_id].append({
                "role": role,
                "content": content,
                "timestamp": int(time.time() * 1000)
            })

    def _get_registry_file(self) -> str:
        """返回设备注册表的持久化文件路径（~/cow/devices/registry.json）"""
        if self._registry_file:
            return self._registry_file
        from common.utils import expand_path
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        devices_dir = os.path.join(workspace_root, "devices")
        os.makedirs(devices_dir, exist_ok=True)
        self._registry_file = os.path.join(devices_dir, "registry.json")
        return self._registry_file

    def _load_device_registry(self):
        """从磁盘加载设备注册表（服务启动时调用）"""
        try:
            path = self._get_registry_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                with self._device_registry_lock:
                    self.device_registry = data
                logger.info(f"[WebChannel] Loaded {len(data)} device(s) from registry")
        except Exception as e:
            logger.warning(f"[WebChannel] Failed to load device registry: {e}")
            event_log.log_exception("device_registry_load_failed", e)

    def _save_device_registry(self):
        """将设备注册表持久化到磁盘（注册/更新时调用）"""
        try:
            path = self._get_registry_file()
            with self._device_registry_lock:
                snapshot = dict(self.device_registry)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[WebChannel] Failed to save device registry: {e}")
            event_log.log_exception("device_registry_save_failed", e)

    def register_device(self):
        """POST /api/device/register — 设备上线注册"""
        try:
            data = web.data()
            json_data = json.loads(data)
            device_id = json_data.get("deviceId", "").strip()
            if not device_id:
                return json.dumps({"success": False, "message": "deviceId is required", "data": None})
            with self._device_registry_lock:
                self.device_registry[device_id] = {"lastSeen": time.time()}
            # 持久化到磁盘（异步，避免阻塞响应）
            threading.Thread(target=self._save_device_registry, daemon=True).start()
            logger.info(f"[WebChannel] Device registered: {device_id}")
            return json.dumps({"success": True, "message": "ok", "data": None})
        except Exception as e:
            logger.exception(f"[WebChannel] register_device error: {e}")
            event_log.log_exception(
                "register_device_failed", e,
                endpoint="/api/device/register",
                device_id=locals().get("device_id", "") or "",
            )
            return json.dumps({"success": False, "message": str(e), "data": None})

    def list_devices(self):
        """GET /api/device — 拉取已注册设备列表"""
        try:
            with self._device_registry_lock:
                devices = [
                    {"deviceId": did, "lastSeen": int(info["lastSeen"] * 1000)}
                    for did, info in self.device_registry.items()
                ]
            return json.dumps({"success": True, "message": "ok", "data": devices})
        except Exception as e:
            logger.exception(f"[WebChannel] list_devices error: {e}")
            event_log.log_exception("list_devices_failed", e, endpoint="/api/device")
            return json.dumps({"success": False, "message": str(e), "data": []})

    def _validate_pet_event(self, raw) -> tuple:
        """
        校验单条宠物事件。成功返回 (True, normalized_dict)，失败返回 (False, error_message)。
        """
        if not isinstance(raw, dict):
            return False, "each event must be an object"
        et = raw.get("type")
        if et not in ("SHAKE", "TEMPERATURE_UPDATE", "HUMIDITY_UPDATE"):
            return False, "invalid event type"
        try:
            ts = raw["ts"]
            if not isinstance(ts, (int, float)):
                return False, "ts must be a number"
        except KeyError:
            return False, "ts is required"
        payload = raw.get("payload", None)
        if et == "SHAKE":
            if payload is not None and payload != {}:
                return False, "SHAKE payload must be null"
            norm = {"type": et, "ts": ts, "payload": None}
        elif et in ("TEMPERATURE_UPDATE", "HUMIDITY_UPDATE"):
            if not isinstance(payload, dict) or "value" not in payload:
                return False, f"{et} requires payload.value"
            v = payload["value"]
            if not isinstance(v, (int, float)):
                return False, "payload.value must be a number"
            norm = {"type": et, "ts": ts, "payload": {"value": v}}
        else:
            return False, "invalid event type"
        return True, norm

    def _enqueue_pet_events(self, device_id: str, events: list):
        with self._pet_event_lock:
            if device_id not in self.pet_event_queues:
                self.pet_event_queues[device_id] = deque()
            q = self.pet_event_queues[device_id]
            for ev in events:
                while len(q) >= self._pet_event_max:
                    q.popleft()
                q.append(ev)

    def send_pet_events(self):
        """POST /api/pet/event/send — 设备上报事件"""
        try:
            device_id = web.ctx.env.get("HTTP_X_DEVICE_ID", "").strip()
            if not device_id:
                logger.warning("[WebChannel] pet/event/send rejected: missing x-device-id")
                return json.dumps({"success": False, "message": "x-device-id header is required", "data": None})
            body = json.loads(web.data())
            events_in = body.get("events")
            if not isinstance(events_in, list):
                logger.warning(f"[WebChannel] pet/event/send rejected device={device_id!r}: events is not an array")
                return json.dumps({"success": False, "message": "events must be an array", "data": None})
            normalized = []
            for i, raw in enumerate(events_in):
                ok, msg_or_ev = self._validate_pet_event(raw)
                if not ok:
                    logger.warning(
                        f"[WebChannel] pet/event/send rejected device={device_id!r} events[{i}]: {msg_or_ev}"
                    )
                    return json.dumps(
                        {"success": False, "message": f"events[{i}]: {msg_or_ev}", "data": None},
                        ensure_ascii=False,
                    )
                normalized.append(msg_or_ev)
            self._enqueue_pet_events(device_id, normalized)
            logger.info(f"[WebChannel] pet/event/send ok device={device_id!r} count={len(normalized)}")
            return json.dumps({"success": True, "message": "ok", "data": None})
        except json.JSONDecodeError:
            logger.warning("[WebChannel] pet/event/send rejected: invalid JSON body")
            return json.dumps({"success": False, "message": "invalid JSON body", "data": None})
        except Exception as e:
            logger.exception(f"[WebChannel] send_pet_events error: {e}")
            event_log.log_exception(
                "send_pet_events_failed", e,
                endpoint="/api/pet/event/send",
                device_id=locals().get("device_id", "") or "",
            )
            return json.dumps({"success": False, "message": str(e), "data": None})

    def poll_pet_events(self):
        """
        GET /api/pet/event/poll — 消费事件（每条返回后从队列删除）。
        传 ts 时：先丢弃所有 ts <= 查询参数的事件，再一次性取出并删除剩余全部；
        不传 ts：从队头开始一次性取出并删除当前队列中全部事件。
        """
        try:
            device_id = web.ctx.env.get("HTTP_X_DEVICE_ID", "").strip()
            if not device_id:
                logger.warning("[WebChannel] pet/event/poll rejected: missing x-device-id")
                return json.dumps({"success": False, "message": "x-device-id header is required", "data": []})
            params = web.input(ts=None)
            ts_cutoff = None
            if params.ts not in (None, ""):
                try:
                    ts_cutoff = float(params.ts)
                except (TypeError, ValueError):
                    logger.warning(f"[WebChannel] pet/event/poll rejected device={device_id!r}: bad ts={params.ts!r}")
                    return json.dumps({"success": False, "message": "ts must be a number", "data": []})
            out = []
            with self._pet_event_lock:
                q = self.pet_event_queues.get(device_id)
                if not q:
                    logger.debug(f"[WebChannel] pet/event/poll empty device={device_id!r}")
                    return json.dumps({"success": True, "message": "ok", "data": []}, ensure_ascii=False)
                if ts_cutoff is not None:
                    while q and q[0]["ts"] <= ts_cutoff:
                        q.popleft()
                while q:
                    out.append(q.popleft())
            if out:
                logger.info(
                    f"[WebChannel] pet/event/poll device={device_id!r} ts_cutoff={ts_cutoff!r} delivered={len(out)}"
                )
            else:
                logger.debug(
                    f"[WebChannel] pet/event/poll device={device_id!r} ts_cutoff={ts_cutoff!r} delivered=0 (drained)"
                )
            return json.dumps({"success": True, "message": "ok", "data": out}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"[WebChannel] poll_pet_events error: {e}")
            event_log.log_exception(
                "poll_pet_events_failed", e,
                endpoint="/api/pet/event/poll",
                device_id=locals().get("device_id", "") or "",
            )
            return json.dumps({"success": False, "message": str(e), "data": []})

    def pull_chatlog(self):
        """GET /api/chatlog/pull — APP 消费来自设备的聊天记录（每次最多 10 条）"""
        try:
            device_id = web.ctx.env.get("HTTP_X_DEVICE_ID", "").strip()
            if not device_id:
                return json.dumps({"success": False, "message": "x-device-id header is required", "data": {"messages": []}})
            messages = []
            with self._chatlog_lock:
                q = self.chatlog_queues.get(device_id)
                if q:
                    for _ in range(10):
                        if not q:
                            break
                        messages.append(q.popleft())
            return json.dumps({"success": True, "message": "ok", "data": {"messages": messages}}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"[WebChannel] pull_chatlog error: {e}")
            event_log.log_exception(
                "pull_chatlog_failed", e,
                endpoint="/api/chatlog/pull",
                device_id=locals().get("device_id", "") or "",
            )
            return json.dumps({"success": False, "message": str(e), "data": {"messages": []}})

    def post_message(self):
        """
        Handle incoming messages from users via POST request.
        Returns a request_id for tracking this specific request.
        Supports headers:
          x-device-id : device identifier (used as session_id for memory isolation)
          source      : 'DEVICE' | 'APP'  (default: 'APP')
        """
        try:
            # 读取请求来源相关 headers
            device_id = web.ctx.env.get("HTTP_X_DEVICE_ID", "").strip()
            source = web.ctx.env.get("HTTP_SOURCE", "APP").strip().upper()
            auth_token = web.ctx.env.get("HTTP_X_AUTH_TOKEN", "").strip()
            
            if source not in ("DEVICE", "APP"):
                source = "APP"

            data = web.data()
            json_data = json.loads(data)
            
            # 优先使用 token 解析出的 user_id 作为 session
            session_id = None
            user_id = -1
            user_group = -1
            phone_number = ""
            if auth_token:
                user = db.get_user_by_token(auth_token)
                if user:
                    session_id = f"user_{user['id']}"
                    user_id = user['id']
                    user_group = user.get('user_group', -1)
                    phone_number = user.get('phone_number', '')
                else:
                    web.ctx.status = '401 Unauthorized'
                    return json.dumps({"status": "error", "message": "unauthorized", "code": 401})
                    
            if not session_id:
                # 如果没有 token 或 token 无效，回退到原逻辑
                if device_id:
                    session_id = device_id
                else:
                    session_id = json_data.get('session_id', f'session_{int(time.time())}')

            # 任何请求进来都预热磁盘缓存（冷启动时从 DB 加载，热时仅刷新 last_active）
            try:
                from agent.memory.user_cache import touch as _cache_touch
                from config import conf as _conf
                from common.utils import expand_path as _expand
                _cache_touch(_expand(_conf().get("agent_workspace", "~/cow")), session_id)
            except Exception:
                pass

            prompt = json_data.get('message', '')
            image_b64 = json_data.get('image', '')   # base64 编码的图片（可选）
            image_url_input = json_data.get('image_url', '')  # OSS/CDN URL（可选，与 image 二选一）
            image_type = json_data.get('image_type', 'jpeg')  # 图片格式，默认 jpeg
            # 兼容前端把图片 URL 直接写在 message 文本里的情况
            if not image_url_input and not image_b64 and prompt:
                import re as _re
                _url_match = _re.search(r'https?://\S+\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?', prompt, _re.IGNORECASE)
                if _url_match:
                    image_url_input = _url_match.group(0)
                    prompt = (prompt[:_url_match.start()] + " " + prompt[_url_match.end():]).strip()
            use_sse = json_data.get('stream', True)
            change_settings = bool(json_data.get('change_settings'))
            timezone     = json_data.get('timezone')
            sensor_label = json_data.get('sensor_label', '')  # 由前端从 GET /api/weather 拿到后回传
            db.record_user_timezone_async(user_id, timezone)

            request_id = self._generate_request_id()
            self.request_to_session[request_id] = session_id

            # The classifier returns a per-turn mode switch directive:
            # voice/text when a switch is requested or materially improves
            # this turn, otherwise None.
            from agent.chat.reply_mode import (
                classify_reply_mode,
                normalize_parent_reply_mode,
            )
            parent_reply_mode = normalize_parent_reply_mode(
                json_data.get("parent_reply_mode"),
            )
            reply_mode = classify_reply_mode(
                prompt,
                request_id=request_id,
                parent_reply_mode=parent_reply_mode,
            )

            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            if use_sse:
                self._ensure_sse_queue(request_id)

            msg = WebMessage(self._generate_msg_id(), prompt or image_b64 or image_url_input)
            msg.from_user_id = session_id
            message_id = None

            # ---------- 图片消息（OSS URL）----------
            if image_url_input and not image_b64:
                context = self._compose_context(ContextType.IMAGE, image_url_input, msg=msg, isgroup=False)
                if context is None:
                    if request_id in self.sse_queues:
                        del self.sse_queues[request_id]
                    return json.dumps({"status": "error", "message": "Message was filtered"})

                context["image_url"] = image_url_input
                if prompt:
                    context["image_caption"] = prompt

                context["session_id"] = session_id
                context["receiver"] = session_id
                context["request_id"] = request_id
                context["device_id"] = device_id
                context["source"] = source
                context["timezone"] = timezone
                context["sensor_label"] = sensor_label
                context["llm_start_time"] = time.time()
                context["user_id"] = user_id
                context["user_group"] = user_group
                context["phone_number"] = phone_number
                context["reply_mode"] = reply_mode
                context["parent_reply_mode"] = parent_reply_mode

                _log_ctx = {
                    "user_id": user_id,
                    "user_group": user_group,
                    "phone_number": phone_number,
                    "session_id": session_id,
                    "device_id": device_id,
                    "source": source,
                }
                event_log.log(
                    "llm_start",
                    request_id=request_id,
                    message_type="image_url",
                    image_url=image_url_input,
                    image_caption=prompt,
                    timezone=timezone,
                    sensor_label=sensor_label,
                    **_log_ctx,
                )

                context["name_changed_holder"] = {"value": None}
                if use_sse:
                    context["on_event"] = self._make_sse_callback(
                        request_id,
                        _log_ctx,
                        reply_mode,
                        context["name_changed_holder"],
                    )

                if source == "DEVICE" and device_id:
                    self._push_chatlog(device_id, "user", f"[图片]{(' ' + prompt) if prompt else ''}")
                if user_id != -1:
                    message_id = db.append_chat_message(
                        user_id,
                        session_id,
                        "user",
                        "",
                        "image_url",
                        source,
                        request_id,
                        image_url_input,
                        weather_text=sensor_label,
                    )
                    if prompt and prompt.strip():
                        db.append_chat_message(
                            user_id,
                            session_id,
                            "user",
                            prompt,
                            "text",
                            source,
                            request_id,
                            weather_text=sensor_label,
                        )

                self._track_proactive_conversation(context)
                threading.Thread(target=self._produce_with_logging, args=(context,)).start()
                return json.dumps({
                    "status": "success",
                    "request_id": request_id,
                    "stream": use_sse,
                    "message_id": message_id,
                    "reply_mode": reply_mode,
                })

            # ---------- 图片消息（base64）----------
            if image_b64:
                image_path = self._save_image_from_b64(image_b64, image_type)
                if not image_path:
                    if request_id in self.sse_queues:
                        del self.sse_queues[request_id]
                    return json.dumps({"status": "error", "message": "Invalid image data"})

                msg.content = image_path
                context = self._compose_context(ContextType.IMAGE, image_path, msg=msg, isgroup=False)
                if context is None:
                    if request_id in self.sse_queues:
                        del self.sse_queues[request_id]
                    return json.dumps({"status": "error", "message": "Message was filtered"})

                # 如果同时附带了文字，覆盖识图 prompt
                if prompt:
                    context.content = image_path
                    context["image_caption"] = prompt

                context["session_id"] = session_id
                context["receiver"] = session_id
                context["request_id"] = request_id
                context["device_id"] = device_id
                context["source"] = source
                context["timezone"]     = timezone
                context["sensor_label"] = sensor_label
                context["llm_start_time"] = time.time()
                context["user_id"] = user_id
                context["user_group"] = user_group
                context["phone_number"] = phone_number
                context["reply_mode"] = reply_mode
                context["parent_reply_mode"] = parent_reply_mode

                _log_ctx = {
                    "user_id": user_id,
                    "user_group": user_group,
                    "phone_number": phone_number,
                    "session_id": session_id,
                    "device_id": device_id,
                    "source": source,
                }
                event_log.log(
                    "llm_start",
                    request_id=request_id,
                    message_type="image_b64",
                    image_path=image_path,
                    image_caption=prompt,
                    timezone=timezone,
                    sensor_label=sensor_label,
                    **_log_ctx,
                )

                context["name_changed_holder"] = {"value": None}
                if use_sse:
                    context["on_event"] = self._make_sse_callback(
                        request_id,
                        _log_ctx,
                        reply_mode,
                        context["name_changed_holder"],
                    )


                if source == "DEVICE" and device_id:
                    self._push_chatlog(device_id, "user", f"[图片]{(' ' + prompt) if prompt else ''}")
                if user_id != -1:
                    message_id = db.append_chat_message(
                        user_id,
                        session_id,
                        "user",
                        f"[图片]{(' ' + prompt) if prompt else ''}",
                        "image_b64",
                        source,
                        request_id,
                        weather_text=sensor_label,
                    )

                self._track_proactive_conversation(context)
                threading.Thread(target=self._produce_with_logging, args=(context,)).start()
                return json.dumps({
                    "status": "success",
                    "request_id": request_id,
                    "stream": use_sse,
                    "message_id": message_id,
                    "reply_mode": reply_mode,
                })

            # ---------- 文本消息 ----------
            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message: {prompt}")

            msg.content = prompt
            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                if request_id in self.sse_queues:
                    del self.sse_queues[request_id]
                return json.dumps({"status": "error", "message": "Message was filtered"})

            context["session_id"] = session_id
            context["receiver"] = session_id
            context["request_id"] = request_id
            context["device_id"] = device_id
            context["source"] = source
            context["user_message"] = prompt
            context["timezone"]     = timezone
            context["sensor_label"] = sensor_label
            context["llm_start_time"] = time.time()
            context["user_id"] = user_id
            context["user_group"] = user_group
            context["phone_number"] = phone_number
            context["reply_mode"] = reply_mode
            context["parent_reply_mode"] = parent_reply_mode
            if change_settings:
                context["change_settings"] = True

            _log_ctx = {
                "user_id": user_id,
                "user_group": user_group,
                "phone_number": phone_number,
                "session_id": session_id,
                "device_id": device_id,
                "source": source,
            }
            event_log.log(
                "llm_start",
                request_id=request_id,
                message_type="text",
                message=prompt,
                timezone=timezone,
                sensor_label=sensor_label,
                **_log_ctx,
            )

            context["name_changed_holder"] = {"value": None}
            if use_sse:
                context["on_event"] = self._make_sse_callback(
                    request_id,
                    _log_ctx,
                    reply_mode,
                    context["name_changed_holder"],
                )

            # DEVICE 来源：将用户消息写入聊天记录队列
            if source == "DEVICE" and device_id:
                self._push_chatlog(device_id, "user", prompt)
            if user_id != -1 and not change_settings:
                message_id = db.append_chat_message(
                    user_id, session_id, "user", prompt, "text", source, request_id,
                    weather_text=sensor_label,
                )

            self._track_proactive_conversation(context)
            threading.Thread(target=self._produce_with_logging, args=(context,)).start()

            return json.dumps({
                "status": "success",
                "request_id": request_id,
                "stream": use_sse,
                "message_id": message_id,
                "reply_mode": reply_mode,
            })

        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            # Failure here means the HTTP entry blew up before producing — there
            # may not even be a request_id yet, but log whatever context we have.
            event_log.log_exception(
                "post_message_failed",
                e,
                endpoint="/message",
                request_id=locals().get("request_id", "") or "",
                user_id=locals().get("user_id", -1),
                phone_number=locals().get("phone_number", "") or "",
                session_id=locals().get("session_id", "") or "",
                source=locals().get("source", "") or "",
            )
            return json.dumps({"status": "error", "message": str(e)})

    def stream_response(self, request_id: str):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        """
        # Stream lifecycle gets one open + one close event so we can spot:
        # invalid request_id (frontend asking for something we never created),
        # timeout (5min elapsed without llm_done), disconnect (client gave up).
        if request_id not in self.sse_queues:
            event_log.log(
                "stream_open",
                request_id=request_id,
                outcome="invalid_request_id",
                endpoint="/stream",
            )
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        event_log.log("stream_open", request_id=request_id, endpoint="/stream")
        opened_at = time.time()
        q = self.sse_queues[request_id]
        timeout = 300  # 5 minutes max
        deadline = opened_at + timeout
        outcome = "unknown"

        try:
            while time.time() < deadline:
                try:
                    item = q.get(timeout=1)
                except Empty:
                    yield b": keepalive\n\n"
                    continue

                payload = json.dumps(item, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")

                if item.get("type") == "done":
                    outcome = "done"
                    break
            else:
                outcome = "timeout"
        except GeneratorExit:
            # Client disconnected mid-stream (closed tab, navigated away, etc.)
            outcome = "client_disconnect"
            raise
        except Exception as e:
            outcome = "error"
            event_log.log_exception(
                "stream_failed", e,
                request_id=request_id, endpoint="/stream",
            )
            raise
        finally:
            self.sse_queues.pop(request_id, None)
            event_log.log(
                "stream_close",
                request_id=request_id,
                endpoint="/stream",
                outcome=outcome,
                duration_ms=int((time.time() - opened_at) * 1000),
            )

    def poll_response(self):
        """
        Poll for responses using the session_id.
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')

            if not session_id or session_id not in self.session_queues:
                # High-frequency endpoint — only log the failure case (stale
                # session_id), not every empty poll. Empty polls are expected.
                event_log.log(
                    "poll_invalid_session",
                    endpoint="/poll",
                    session_id=session_id or "",
                )
                return json.dumps({"status": "error", "message": "Invalid session ID"})
            
            # 尝试从队列获取响应，不等待
            try:
                # 使用peek而不是get，这样如果前端没有成功处理，下次还能获取到
                response = self.session_queues[session_id].get(block=False)
                
                # 返回响应，包含请求ID以区分不同请求
                return json.dumps({
                    "status": "success", 
                    "has_content": True,
                    "content": response["content"],
                    "request_id": response["request_id"],
                    "timestamp": response["timestamp"],
                    "reply_mode": response.get("reply_mode"),
                    "nameChangedSuccess": response.get("nameChangedSuccess"),
                })
                
            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})
                
        except Exception as e:
            logger.exception(f"Error polling response: {e}")
            event_log.log_exception(
                "poll_response_failed", e,
                endpoint="/poll",
                session_id=locals().get("session_id", "") or "",
            )
            return json.dumps({"status": "error", "message": str(e)})

    def chat_page(self):
        """Serve the chat HTML page."""
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def startup(self):
        port = conf().get("web_port", 9899)
        db.init_db()
        self._start_account_cleanup_thread()
        self._start_diary_worker()
        self._start_proactive_push_worker()

        # 从磁盘恢复设备注册表
        self._load_device_registry()
        
        # 打印可用渠道类型提示
        logger.info("[WebChannel] 当前channel为web，可修改 config.json 配置文件中的 channel_type 字段进行切换。全部可用类型为：")
        logger.info("[WebChannel]   1. web              - 网页")
        logger.info("[WebChannel]   2. terminal         - 终端")
        logger.info("[WebChannel]   3. feishu           - 飞书")
        logger.info("[WebChannel]   4. dingtalk         - 钉钉")
        logger.info("[WebChannel]   5. wechatcom_app    - 企微自建应用")
        logger.info("[WebChannel]   6. wechatmp         - 个人公众号")
        logger.info("[WebChannel]   7. wechatmp_service - 企业公众号")
        logger.info(f"[WebChannel] 🌐 本地访问: http://localhost:{port}")
        logger.info(f"[WebChannel] 🌍 服务器访问: http://YOUR_IP:{port} (请将YOUR_IP替换为服务器IP)")
        logger.info("[WebChannel] ✅ Web对话网页已运行")
        
        # 确保静态文件目录存在
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        if not os.path.exists(static_dir):
            os.makedirs(static_dir)
            logger.debug(f"[WebChannel] Created static directory: {static_dir}")
        
        urls = (
            '/', 'RootHandler',
            '/message', 'MessageHandler',
            '/poll', 'PollHandler',
            '/stream', 'StreamHandler',
            '/chat', 'ChatHandler',
            '/complaints', 'ComplaintsPageHandler',
            '/push-contents', 'PushContentsPageHandler',
            '/config', 'ConfigHandler',
            '/api/skills', 'SkillsHandler',
            '/api/scheduler', 'SchedulerHandler',
            '/api/logs', 'LogsHandler',
            '/api/device/register', 'DeviceRegisterHandler',
            '/api/device', 'DeviceListHandler',
            '/api/chatlog/pull', 'ChatlogPullHandler',
            '/api/pet/event/poll', 'PetEventPollHandler',
            '/api/pet/event/send', 'PetEventSendHandler',
            '/api/auth/register', 'AuthRegisterHandler',
            '/api/user/profile', 'UserProfileHandler',
            '/api/user/activity', 'UserActivityHandler',
            '/api/user/account', 'UserAccountHandler',
            '/api/feedback', 'FeedbackHandler',
            '/api/admin/complaints/auth', 'ComplaintAdminAuthHandler',
            '/api/admin/complaints', 'ComplaintAdminListHandler',
            '/api/admin/complaints/comment', 'ComplaintAdminCommentHandler',
            '/api/admin/complaints/status', 'ComplaintAdminStatusHandler',
            '/api/admin/push-contents', 'PushContentCollectionHandler',
            r'/api/admin/push-contents/(\d+)', 'PushContentItemHandler',
            r'/api/admin/push-contents/(\d+)/images', 'PushContentImageCollectionHandler',
            r'/api/admin/push-contents/(\d+)/images/(\d+)', 'PushContentImageItemHandler',
            '/api/admin/diary/retry', 'DiaryDateRetryHandler',
            '/api/app/version', 'AppVersionHandler',
            '/api/push/register', 'UserPushDeviceRegisterHandler',
            '/api/push/unregister', 'UserPushDeviceUnregisterHandler',
            '/api/push/test', 'PushTestHandler',
            r'/api/push/(psh_[0-9a-f]{32})/card', 'PushCardHandler',
            '/api/chat/history', 'ChatHistoryHandler',
            '/api/diary', 'DiaryHandler',
            '/api/diary/image/style', 'DiaryImageStyleHandler',
            '/api/invite_code', 'InviteCodeHandler',
            '/api/user_behavior', 'UserBehaviorHandler',
            '/api/client/event', 'ClientEventHandler',
            '/api/weather', 'WeatherHandler',
            '/metrics', 'MetricsHandler',
            '/assets/(.*)', 'AssetsHandler',
            '/diary-images/(.*)', 'DiaryImageHandler',
        )
        web.config.debug = bool(conf().get("debug", False))
        app = web.application(urls, globals(), autoreload=False)
        app.add_processor(metrics_processor)
        
        # 完全禁用web.py的HTTP日志输出
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None
        
        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)
        
        # Build WSGI app with middleware (same as runsimple but without print)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer(("0.0.0.0", port), func)
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def _start_account_cleanup_thread(self):
        if self._account_cleanup_started:
            return
        self._account_cleanup_started = True

        def _cleanup_loop():
            from common.utils import expand_path
            workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
            while True:
                try:
                    cleaned = db.cleanup_expired_deleted_accounts(workspace_root)
                    if cleaned:
                        logger.info(f"[WebChannel] Cleaned {cleaned} expired deleted accounts")
                except Exception as e:
                    logger.warning(f"[WebChannel] Account cleanup failed: {e}")
                    event_log.log_exception("account_cleanup_failed", e)
                time.sleep(24 * 60 * 60)

        threading.Thread(
            target=_cleanup_loop,
            daemon=True,
            name="account-cleanup",
        ).start()

    def _start_diary_worker(self):
        if self._diary_worker_started:
            return
        self._diary_worker_started = True
        try:
            from channel.web.diary.worker import start_diary_worker
            start_diary_worker()
        except Exception as e:
            logger.exception("[WebChannel] Failed to start diary worker: %s", e)
            event_log.log_exception("diary_worker_start_failed", e)

    def _start_proactive_push_worker(self):
        if self._proactive_push_worker_started:
            return
        self._proactive_push_worker_started = True
        try:
            from channel.web.push.worker import start_proactive_push_worker
            start_proactive_push_worker()
        except Exception as e:
            logger.exception("[WebChannel] Failed to start proactive push worker: %s", e)
            event_log.log_exception("proactive_push_worker_start_failed", e)

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
                event_log.log_exception("http_server_stop_failed", e)
            self._http_server = None


class RootHandler:
    def GET(self):
        # 重定向到/chat
        raise web.seeother('/chat')


class MessageHandler:
    def POST(self):
        return WebChannel().post_message()


class PollHandler:
    def POST(self):
        return WebChannel().poll_response()


class StreamHandler:
    def GET(self):
        params = web.input(request_id='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')
        web.header('Access-Control-Allow-Origin', '*')

        return WebChannel().stream_response(request_id)


class ChatHandler:
    def GET(self):
        # 正常返回聊天页面
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()


class ComplaintsPageHandler:
    def GET(self):
        file_path = os.path.join(os.path.dirname(__file__), 'complaints.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()


class PushContentsPageHandler:
    def GET(self):
        file_path = os.path.join(os.path.dirname(__file__), 'push_contents.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()


class ConfigHandler:
    def GET(self):
        """Return configuration info for the web console."""
        try:
            local_config = conf()
            use_agent = local_config.get("agent", False)

            if use_agent:
                title = "CowAgent"
            else:
                title = "AI Assistant"

            return json.dumps({
                "status": "success",
                "use_agent": use_agent,
                "title": title,
                "model": local_config.get("model", ""),
                "channel_type": local_config.get("channel_type", ""),
                "agent_max_context_tokens": local_config.get("agent_max_context_tokens", ""),
                "agent_max_context_turns": local_config.get("agent_max_context_turns", ""),
                "agent_max_steps": local_config.get("agent_max_steps", ""),
            })
        except Exception as e:
            logger.exception(f"Error getting config: {e}")
            event_log.log_exception("config_get_failed", e, endpoint="/config")
            return json.dumps({"status": "error", "message": str(e)})


def _get_workspace_root():
    """Resolve the agent workspace directory."""
    from common.utils import expand_path
    return expand_path(conf().get("agent_workspace", "~/cow"))


class SkillsHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            skills = service.query()
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"[WebChannel] Skills API error: {e}")
            event_log.log_exception("skills_api_failed", e, endpoint="/api/skills")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            tasks = store.list_tasks()
            return json.dumps({"status": "success", "tasks": tasks}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"[WebChannel] Scheduler API error: {e}")
            event_log.log_exception("scheduler_api_failed", e, endpoint="/api/scheduler")
            return json.dumps({"status": "error", "message": str(e)})


class LogsHandler:
    def GET(self):
        """Stream the last N lines of run.log as SSE, then tail new lines."""
        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        from config import get_root
        log_path = os.path.join(get_root(), "run.log")

        def generate():
            if not os.path.isfile(log_path):
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                tail_lines = lines[-200:]
                chunk = ''.join(tail_lines)
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end
                    deadline = time.time() + 600  # 10 min max
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            payload = json.dumps({"type": "line", "content": line}, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception:
                return

        return generate()


class DeviceRegisterHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().register_device()


class DeviceListHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().list_devices()


class ChatlogPullHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().pull_chatlog()


class PetEventPollHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        return WebChannel().poll_pet_events()


class PetEventSendHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        return WebChannel().send_pet_events()


class AuthRegisterHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        phone_number = None
        invite_code = None
        try:
            data = json.loads(web.data())
            phone_number = data.get('phoneNumber')
            invite_code = data.get('inviteCode')
            if not phone_number or not invite_code:
                USER_AUTH_TOTAL.labels(type="missing_params").inc()
                event_log.log("auth_fail", reason="missing_params",
                              phone_number=phone_number or "", invite_code=invite_code or "")
                return json.dumps({"success": False, "message": "Missing phoneNumber or inviteCode", "data": None}, ensure_ascii=False)

            success, msg, token, action_type = db.register_or_login(phone_number, invite_code)

            USER_AUTH_TOTAL.labels(type=action_type).inc()

            if not success:
                event_log.log("auth_fail", reason=action_type, message=msg,
                              phone_number=phone_number, invite_code=invite_code)
                if action_type == "ACCOUNT_PENDING_DELETION":
                    return json.dumps({"success": False, "message": msg, "code": 4031, "data": None}, ensure_ascii=False)
                if action_type == "ACCOUNT_DELETED":
                    return json.dumps({"success": False, "message": msg, "code": 4032, "data": None}, ensure_ascii=False)
                return json.dumps({"success": False, "message": msg, "data": None}, ensure_ascii=False)

            event_log.log("auth_done", action=action_type,
                          phone_number=phone_number, invite_code=invite_code)
            return json.dumps({"success": True, "message": "Success", "data": {"token": token}}, ensure_ascii=False)
        except Exception as e:
            USER_AUTH_TOTAL.labels(type="unknown_error").inc()
            logger.exception(f"AuthRegisterHandler error: {e}")
            event_log.log_exception(
                "auth_register_failed", e,
                endpoint="/api/auth/register",
                phone_number=phone_number or "",
                invite_code=invite_code or "",
            )
            return json.dumps({"success": False, "message": "Server error", "data": None})


class UserProfileHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            profile = db.get_user_profile(user["id"])
            return _api_response(True, "Success", profile)
        except Exception as e:
            logger.exception(f"UserProfileHandler error: {e}")
            event_log.log_exception("user_profile_failed", e, endpoint="/api/user/profile")
            return _api_response(False, "Server error", None)


class UserActivityHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            request_body = json.loads(web.data() or b"{}")
            report = UserActivityReport.from_request_body(request_body)
            if not push_repository.update_user_activity(
                user["id"],
                report.timezone_profile,
                notification_enabled=report.notification_enabled,
                location=report.location,
            ):
                web.ctx.status = '404 Not Found'
                return _api_response(False, "user not found", None)
            return _api_response(True, "Success", None)
        except json.JSONDecodeError:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, "invalid JSON body", None)
        except UserActivityRequestError as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("UserActivityHandler error: %s", error)
            event_log.log_exception(
                "user_activity_failed", error, endpoint="/api/user/activity"
            )
            return _api_response(False, "Server error", None)


class UserAccountHandler:
    def DELETE(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            db.request_account_deletion(user["id"])
            return _api_response(True, "Success", None)
        except Exception as e:
            logger.exception(f"UserAccountHandler error: {e}")
            event_log.log_exception("user_account_delete_failed", e, endpoint="/api/user/account")
            return _api_response(False, "Server error", None)


class FeedbackHandler:
    VALID_TYPES = {"experience", "bug", "suggestion", "other"}

    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)

            data = json.loads(web.data() or b"{}")
            feedback_type = data.get("type")
            if feedback_type is not None and feedback_type not in self.VALID_TYPES:
                return _api_response(False, "Invalid feedback type", None)

            description = data.get("description")
            if not isinstance(description, str) or not description.strip():
                return _api_response(False, "description is required", None)
            description = description.strip()
            if len(description) > 200:
                return _api_response(False, "description exceeds 200 chars", None)

            images = data.get("images") or []
            if not isinstance(images, list) or len(images) > 3 or not all(isinstance(item, str) for item in images):
                return _api_response(False, "images must be a URL list with at most 3 items", None)

            contact = data.get("contact")
            if contact is not None:
                if not isinstance(contact, str):
                    return _api_response(False, "contact must be a string", None)
                contact = contact.strip()
                if len(contact) > 50:
                    return _api_response(False, "contact exceeds 50 chars", None)

            db.create_feedback(user["id"], feedback_type, description, images, contact)
            return _api_response(True, "Success", None)
        except json.JSONDecodeError:
            return _api_response(False, "invalid JSON body", None)
        except Exception as e:
            logger.exception(f"FeedbackHandler error: {e}")
            event_log.log_exception("feedback_failed", e, endpoint="/api/feedback")
            return _api_response(False, "Server error", None)


class ComplaintAdminAuthHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        if not _admin_authorized():
            web.ctx.status = '401 Unauthorized'
            return _api_response(False, "unauthorized", None)
        return _api_response(True, "Success", {"authorized": True})

    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        if not _admin_authorized():
            web.ctx.status = '401 Unauthorized'
            return _api_response(False, "unauthorized", None)
        return _api_response(True, "Success", {"authorized": True})


class ComplaintAdminListHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            params = web.input(keyword="", status="", order="desc", limit=30, offset=0)
            repair_status = params.status.strip() if params.status else ""
            if repair_status and repair_status not in COMPLAINT_FILTER_STATUSES:
                return _api_response(False, "Invalid repair status", None)
            data = db.list_feedbacks(
                keyword=params.keyword.strip() if params.keyword else "",
                repair_status=repair_status or None,
                order=params.order,
                limit=int(params.limit or 30),
                offset=int(params.offset or 0),
            )
            return _api_response(True, "Success", data)
        except ValueError:
            return _api_response(False, "Invalid limit or offset", None)
        except Exception as e:
            logger.exception(f"ComplaintAdminListHandler error: {e}")
            event_log.log_exception("complaint_admin_list_failed", e, endpoint="/api/admin/complaints")
            return _api_response(False, "Server error", None)


class ComplaintAdminCommentHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            data = json.loads(web.data() or b"{}")
            feedback_id = int(data.get("feedbackId") or 0)
            content = str(data.get("content") or "").strip()
            if feedback_id <= 0:
                return _api_response(False, "feedbackId is required", None)
            if not content:
                return _api_response(False, "content is required", None)
            comment = db.add_feedback_comment(feedback_id, content)
            if not comment:
                return _api_response(False, "feedback not found", None)
            return _api_response(True, "Success", comment)
        except ValueError:
            return _api_response(False, "Invalid feedbackId", None)
        except json.JSONDecodeError:
            return _api_response(False, "invalid JSON body", None)
        except Exception as e:
            logger.exception(f"ComplaintAdminCommentHandler error: {e}")
            event_log.log_exception("complaint_admin_comment_failed", e, endpoint="/api/admin/complaints/comment")
            return _api_response(False, "Server error", None)


class ComplaintAdminStatusHandler:
    def PUT(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            data = json.loads(web.data() or b"{}")
            feedback_id = int(data.get("feedbackId") or 0)
            repair_status = str(data.get("status") or "").strip()
            if feedback_id <= 0:
                return _api_response(False, "feedbackId is required", None)
            if repair_status not in REPAIR_STATUSES:
                return _api_response(False, "Invalid repair status", None)
            if not db.update_feedback_repair_status(feedback_id, repair_status):
                return _api_response(False, "feedback not found", None)
            return _api_response(True, "Success", None)
        except ValueError:
            return _api_response(False, "Invalid feedbackId", None)
        except json.JSONDecodeError:
            return _api_response(False, "invalid JSON body", None)
        except Exception as e:
            logger.exception(f"ComplaintAdminStatusHandler error: {e}")
            event_log.log_exception("complaint_admin_status_failed", e, endpoint="/api/admin/complaints/status")
            return _api_response(False, "Server error", None)


class PushContentCollectionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            params = web.input(
                pushType="", deliveryScene="", enabled="", keyword="",
                limit=30, offset=0,
            )
            push_type = str(params.pushType or "").strip().lower()
            if push_type and push_type not in ("greeting", "weather", "diary", "recall"):
                web.ctx.status = '400 Bad Request'
                return _api_response(False, "invalid pushType", None)
            enabled = _optional_boolean_query(params.enabled)
            result = push_assets.add_signed_urls_to_content_list(
                push_repository.list_contents(
                    push_type=push_type or None,
                    delivery_scene=str(params.deliveryScene or "").strip() or None,
                    enabled=enabled,
                    keyword=str(params.keyword or "").strip() or None,
                    limit=int(params.limit or 30),
                    offset=int(params.offset or 0),
                )
            )
            return _api_response(True, "Success", result)
        except ValueError as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("PushContentCollectionHandler GET error: %s", error)
            return _api_response(False, "Server error", None)

    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            mutation = PushContentMutation.from_request_body(
                json.loads(web.data() or b"{}")
            )
            content_id = push_repository.create_content(
                **mutation.as_database_fields()
            )
            return _api_response(True, "Success", {"id": content_id})
        except json.JSONDecodeError:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, "invalid JSON body", None)
        except PushContentRequestError as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except sqlite3.IntegrityError:
            web.ctx.status = '409 Conflict'
            return _api_response(False, "contentNo already exists", None)
        except Exception as error:
            logger.exception("PushContentCollectionHandler POST error: %s", error)
            return _api_response(False, "Server error", None)


class PushContentItemHandler:
    def PUT(self, content_id):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            mutation = PushContentMutation.from_request_body(
                json.loads(web.data() or b"{}")
            )
            if not push_repository.update_content(
                int(content_id), **mutation.as_database_fields()
            ):
                web.ctx.status = '404 Not Found'
                return _api_response(False, "push content not found", None)
            return _api_response(True, "Success", None)
        except json.JSONDecodeError:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, "invalid JSON body", None)
        except (ValueError, PushContentRequestError) as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except sqlite3.IntegrityError:
            web.ctx.status = '409 Conflict'
            return _api_response(False, "contentNo already exists", None)
        except Exception as error:
            logger.exception("PushContentItemHandler PUT error: %s", error)
            return _api_response(False, "Server error", None)

    def DELETE(self, content_id):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            if not push_repository.disable_content(int(content_id)):
                web.ctx.status = '404 Not Found'
                return _api_response(False, "push content not found", None)
            return _api_response(True, "Success", None)
        except ValueError as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("PushContentItemHandler DELETE error: %s", error)
            return _api_response(False, "Server error", None)


class PushContentImageCollectionHandler:
    def POST(self, content_id):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            upload = web.input(file={}).get("file")
            if not upload or not getattr(upload, "file", None):
                web.ctx.status = '400 Bad Request'
                return _api_response(False, "file is required", None)
            image_bytes = upload.file.read(push_assets.MAX_PUSH_IMAGE_BYTES + 1)
            result = push_assets.upload_image_for_content(
                int(content_id),
                getattr(upload, "filename", ""),
                image_bytes,
            )
            return _api_response(True, "Success", result)
        except (ValueError, PushContentImageRequestError) as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("PushContentImageCollectionHandler error: %s", error)
            return _api_response(False, "Server error", None)


class PushContentImageItemHandler:
    def DELETE(self, content_id, image_id):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            if not push_repository.disable_content_image(
                int(content_id), int(image_id)
            ):
                web.ctx.status = '404 Not Found'
                return _api_response(False, "push content image not found", None)
            return _api_response(True, "Success", None)
        except ValueError as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("PushContentImageItemHandler error: %s", error)
            return _api_response(False, "Server error", None)


class AppVersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(platform=None, currentVersion=None)
            if params.platform not in ("ios", "android"):
                return _api_response(False, "Invalid platform", None)
            if not params.currentVersion:
                return _api_response(False, "currentVersion is required", None)
            return _api_response(True, "Success", {
                "hasUpdate": False,
                "latestVersion": "alpha",
                "storeUrl": "",
            })
        except Exception as e:
            logger.exception(f"AppVersionHandler error: {e}")
            event_log.log_exception("app_version_failed", e, endpoint="/api/app/version")
            return _api_response(False, "Server error", None)


class UserPushDeviceRegisterHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            request_body = json.loads(web.data() or b"{}")
            register_authenticated_user_push_device(user["id"], request_body)
            return _api_response(True, "Success", None)
        except json.JSONDecodeError:
            return _api_response(False, "invalid JSON body", None)
        except PushDeviceRequestError as error:
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("UserPushDeviceRegisterHandler error: %s", error)
            event_log.log_exception(
                "push_device_register_failed", error, endpoint="/api/push/register"
            )
            return _api_response(False, "Server error", None)


class UserPushDeviceUnregisterHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            request_body = json.loads(web.data() or b"{}")
            unregister_authenticated_user_push_device(user["id"], request_body)
            return _api_response(True, "Success", None)
        except json.JSONDecodeError:
            return _api_response(False, "invalid JSON body", None)
        except PushDeviceRequestError as error:
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("UserPushDeviceUnregisterHandler error: %s", error)
            event_log.log_exception(
                "push_device_unregister_failed", error,
                endpoint="/api/push/unregister",
            )
            return _api_response(False, "Server error", None)


class PushTestHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            request_body = json.loads(web.data() or b"{}")
            result = send_authenticated_user_push_test(user["id"], request_body)
            return _api_response(True, "Success", result)
        except json.JSONDecodeError:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, "invalid JSON body", None)
        except PushTestRequestError as error:
            web.ctx.status = '400 Bad Request'
            return _api_response(False, str(error), None)
        except PushTestDeviceNotRegisteredError as error:
            web.ctx.status = '409 Conflict'
            return _api_response(False, str(error), None)
        except PushTestDeliveryError as error:
            web.ctx.status = '502 Bad Gateway'
            return _api_response(False, str(error), None)
        except Exception as error:
            logger.exception("PushTestHandler error: %s", error)
            event_log.log_exception(
                "push_test_failed", error, endpoint="/api/push/test"
            )
            return _api_response(False, "Server error", None)


class PushCardHandler:
    def GET(self, push_id):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            card = get_authenticated_user_push_card(user["id"], push_id)
            if not card:
                web.ctx.status = '404 Not Found'
                return _api_response(False, "push card not found", None)
            return _api_response(True, "Success", card)
        except Exception as error:
            logger.exception("PushCardHandler error: %s", error)
            event_log.log_exception(
                "push_card_failed", error, endpoint="/api/push/{pushId}/card"
            )
            return _api_response(False, "Server error", None)


class ChatHistoryHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            params = web.input(offset=None, limit=50)
            offset = int(params.offset) if params.offset not in (None, "", "0") else None
            limit = int(params.limit or 50)
            return _api_response(True, "Success", db.list_chat_messages(user["id"], offset, limit))
        except ValueError:
            return _api_response(False, "Invalid offset or limit", None)
        except Exception as e:
            logger.exception(f"ChatHistoryHandler error: {e}")
            event_log.log_exception("chat_history_failed", e, endpoint="/api/chat/history")
            return _api_response(False, "Server error", None)


class DiaryHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            params = web.input(ts=None)
            if params.ts in (None, ""):
                return _api_response(False, "ts is required", None)
            timestamp_ms = int(params.ts)
            detail = db.get_user_diary_detail(user["id"], timestamp_ms)
            push_repository.mark_diary_viewed(user["id"], timestamp_ms)
            return _api_response(True, "Success", detail)
        except ValueError:
            return _api_response(False, "Invalid ts", None)
        except Exception as e:
            logger.exception(f"DiaryHandler error: {e}")
            event_log.log_exception("diary_detail_failed", e, endpoint="/api/diary")
            return _api_response(False, "Server error", None)

    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return _api_response(False, "unauthorized", None)
            body = json.loads(web.data() or b"{}")
            from channel.web.diary.service import enqueue_diary_for_user, _user_timezone
            target_date = str(body.get("targetDate") or "").strip()
            if not target_date:
                local_now = datetime.now(timezone.utc).astimezone(_user_timezone(user))
                target_date = (local_now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
            mode = str(body.get("mode") or "auto").lower()
            if mode not in {"auto", "normal", "quiet"}:
                return _api_response(False, "Invalid mode", None)
            job = enqueue_diary_for_user(
                user["id"], target_date, mode=mode,
                force=bool(body.get("force", False)), run_async=True,
            )
            web.ctx.status = '202 Accepted'
            return _api_response(True, "Accepted", {
                "id": job.get("id"),
                "state": job.get("state"),
                "targetDate": target_date,
            })
        except (ValueError, TypeError) as e:
            return _api_response(False, str(e), None)
        except Exception as e:
            logger.exception("DiaryHandler POST error: %s", e)
            event_log.log_exception("diary_generate_failed", e, endpoint="/api/diary")
            return _api_response(False, "Server error", None)


class DiaryDateRetryHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        denied = _require_admin()
        if denied:
            return denied
        try:
            body = json.loads(web.data() or b"{}")
            target_date = str(body.get("targetDate") or "").strip()
            if not target_date:
                return _api_response(False, "targetDate is required", None)
            from channel.web.diary.worker import trigger_diary_date_retry
            result = trigger_diary_date_retry(target_date)
            web.ctx.status = '202 Accepted' if result.get("started") else '200 OK'
            return _api_response(True, "Accepted" if result.get("started") else "Already running", result)
        except json.JSONDecodeError:
            return _api_response(False, "invalid JSON body", None)
        except (ValueError, TypeError):
            return _api_response(False, "Invalid targetDate", None)
        except Exception as e:
            logger.exception("DiaryDateRetryHandler error: %s", e)
            event_log.log_exception(
                "diary_date_retry_failed", e, endpoint="/api/admin/diary/retry",
            )
            return _api_response(False, "Server error", None)


class DiaryImageStyleHandler:
    def PUT(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            user = _auth_user()
            if not user:
                web.ctx.status = '401 Unauthorized'
                return json.dumps({"success": False})
            body = json.loads(web.data() or b"{}")
            if not isinstance(body, dict):
                web.ctx.status = '400 Bad Request'
                return json.dumps({"success": False})
            style = body.get("style")
            if not is_valid_diary_image_style(style):
                web.ctx.status = '400 Bad Request'
                return json.dumps({"success": False})
            if not db.update_user_diary_image_style(user["id"], style):
                web.ctx.status = '500 Internal Server Error'
                return json.dumps({"success": False})
            return json.dumps({"success": True})
        except (TypeError, ValueError, json.JSONDecodeError):
            web.ctx.status = '400 Bad Request'
            return json.dumps({"success": False})
        except Exception as error:
            logger.exception("DiaryImageStyleHandler PUT error: %s", error)
            event_log.log_exception(
                "diary_image_style_update_failed", error,
                endpoint="/api/diary/image/style",
            )
            web.ctx.status = '500 Internal Server Error'
            return json.dumps({"success": False})


class InviteCodeHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            data = json.loads(web.data())
            invite_code = data.get('inviteCode')
            expire_at = data.get('expireAt')
            if not invite_code or not expire_at:
                return json.dumps({"success": False, "message": "Missing parameters", "data": None}, ensure_ascii=False)

            db.create_invite_code(invite_code, expire_at)
            return json.dumps({"success": True, "message": "Success", "data": None}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"InviteCodeHandler error: {e}")
            event_log.log_exception(
                "invite_code_create_failed", e,
                endpoint="/api/invite_code",
                invite_code=locals().get("invite_code", "") or "",
            )
            return json.dumps({"success": False, "message": "Server error", "data": None})

    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            codes = db.list_invite_codes()
            return json.dumps({"success": True, "message": "Success", "data": codes}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"InviteCodeHandler error: {e}")
            event_log.log_exception(
                "invite_code_list_failed", e, endpoint="/api/invite_code",
            )
            return json.dumps({"success": False, "message": "Server error", "data": None})


class UserBehaviorHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            data = json.loads(web.data())
            messages = data.get('messages', [])
            if not isinstance(messages, list):
                return json.dumps({"success": False, "message": "Invalid format", "data": None}, ensure_ascii=False)

            db.log_behaviors(messages)
            return json.dumps({"success": True, "message": "Success", "data": None}, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"UserBehaviorHandler error: {e}")
            event_log.log_exception(
                "user_behavior_failed", e, endpoint="/api/user_behavior",
            )
            return json.dumps({"success": False, "message": "Server error", "data": None})


class ClientEventHandler:
    """
    POST /api/client/event
    客户端上报事件（崩溃、HTTP 错误、业务异常、自定义埋点等）。
    服务端不约束 payload 结构，原样写入 events.log，由 Loki 侧解析查询。
    """
    MAX_BATCH = 50
    MAX_EVENT_BYTES = 16 * 1024
    MAX_TOTAL_BYTES = 1 * 1024 * 1024  # 单次请求总上限 1MB

    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            raw = web.data() or b""
            if len(raw) > self.MAX_TOTAL_BYTES:
                return json.dumps({"success": False, "message": "payload too large", "accepted": 0})

            data = json.loads(raw)
            events = data.get('events', [])
            if not isinstance(events, list):
                return json.dumps({"success": False, "message": "events must be a list", "accepted": 0})
            if len(events) > self.MAX_BATCH:
                return json.dumps({"success": False, "message": f"batch exceeds {self.MAX_BATCH}", "accepted": 0})

            # 服务端补齐的上下文
            auth_token = web.ctx.env.get("HTTP_X_AUTH_TOKEN", "").strip()
            server_user_id = -1
            server_phone = ""
            if auth_token:
                u = db.get_user_by_token(auth_token)
                if u:
                    server_user_id = u['id']
                    server_phone = u.get('phone_number', '')

            client_ip = (
                web.ctx.env.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                or web.ctx.env.get('HTTP_X_REAL_IP', '').strip()
                or web.ctx.env.get('REMOTE_ADDR', '')
            )

            accepted = 0
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                # 单条大小检查
                try:
                    if len(json.dumps(ev, ensure_ascii=False)) > self.MAX_EVENT_BYTES:
                        ev = {"_truncated": True, "subtype": ev.get("subtype", "unknown"),
                              "error_msg": str(ev.get("error_msg", ""))[:1000]}
                except Exception:
                    continue

                merged = dict(ev)
                # 服务端上下文（覆盖客户端伪造）
                merged["server_user_id"] = server_user_id
                merged["server_phone"] = server_phone
                merged["client_ip"] = client_ip
                merged["server_ts"] = time.time()

                event_log.log("client_event", **merged)
                accepted += 1

            return json.dumps({"success": True, "accepted": accepted})
        except Exception as e:
            logger.exception(f"ClientEventHandler error: {e}")
            event_log.log_exception(
                "client_event_failed", e, endpoint="/api/client/event",
            )
            return json.dumps({"success": False, "message": "Server error", "accepted": 0})


class AssetsHandler:
    def GET(self, file_path):  # 修改默认参数
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                logger.error(f"File not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)
            event_log.log_exception(
                "assets_serve_failed", e,
                endpoint="/assets",
                file_path=file_path or "",
            )
            raise web.notfound()


class DiaryImageHandler:
    def GET(self, file_path):
        try:
            root = os.path.abspath(os.path.join(os.path.expanduser("~/cow/data"), "diary_images"))
            full_path = os.path.abspath(os.path.normpath(os.path.join(root, file_path)))
            if os.path.commonpath([full_path, root]) != root:
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            web.header('Content-Type', mimetypes.guess_type(full_path)[0] or 'application/octet-stream')
            web.header('Cache-Control', 'public, max-age=31536000, immutable')
            with open(full_path, 'rb') as file:
                return file.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.warning("[Diary] image serve failed path=%s error=%s", file_path, e)
            raise web.notfound()


from common.expired_dict import ExpiredDict

_weather_cache = ExpiredDict(600)

class WeatherHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(lat=None, lon=None)
            lat = params.lat
            lon = params.lon

            if not lat or not lon:
                return json.dumps({"success": False, "message": "Missing lat or lon", "data": None}, ensure_ascii=False)

            try:
                grid_lat = round(float(lat), 2)
                grid_lon = round(float(lon), 2)
                cache_key = f"{grid_lat},{grid_lon}"
            except ValueError:
                return json.dumps({"success": False, "message": "Invalid lat or lon", "data": None}, ensure_ascii=False)

            cached_data = _weather_cache.get(cache_key)
            if cached_data:
                return json.dumps({"success": True, "message": "Success (cached)", "data": cached_data}, ensure_ascii=False)

            import requests
            from datetime import datetime

            api_key = str(conf().get("qweather_api_key", "") or "").strip()
            if not api_key:
                return json.dumps({"success": False, "message": "Weather API is not configured", "data": None}, ensure_ascii=False)
            api_host = str(conf().get(
                "qweather_api_host", "https://mt2x88w6bx.re.qweatherapi.com"
            ) or "").strip().rstrip("/")
            if not api_host:
                return json.dumps({"success": False, "message": "Weather API is not configured", "data": None}, ensure_ascii=False)
            if not api_host.startswith("https://"):
                api_host = "https://" + api_host
            location = f"{grid_lon},{grid_lat}"
            headers = {"X-QW-Api-Key": api_key}

            # Fetch now weather
            now_url = f"{api_host}/v7/weather/now?location={location}"
            now_res_obj = requests.get(now_url, headers=headers, timeout=10)
            now_res = now_res_obj.json()

            # Fetch 3d weather
            daily_url = f"{api_host}/v7/weather/3d?location={location}"
            daily_res_obj = requests.get(daily_url, headers=headers, timeout=10)
            daily_res = daily_res_obj.json()

            if now_res.get("code") != "200" or daily_res.get("code") != "200":
                return json.dumps({"success": False, "message": f"Weather API returned error: {now_res.get('code')}, {daily_res.get('code')}", "data": None}, ensure_ascii=False)

            now_data = now_res.get("now", {})
            daily_data = daily_res.get("daily", [])

            if not daily_data:
                return json.dumps({"success": False, "message": "No daily weather data", "data": None}, ensure_ascii=False)

            today_daily = daily_data[0]
            tomorrow_daily = daily_data[1] if len(daily_data) > 1 else None
            
            def parse_time_to_ts(time_str):
                if not time_str:
                    return 0
                try:
                    return int(datetime.fromisoformat(time_str).timestamp() * 1000)
                except Exception:
                    return 0
                    
            def safe_float(val, default=0.0):
                if val is None or val == "":
                    return default
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return default

            updated_at = int(datetime.now().timestamp() * 1000)
            obs_time = parse_time_to_ts(now_data.get("obsTime"))

            response_data = {
                "now": {
                    "updatedAt": updated_at,
                    "obsTime": obs_time,
                    "temp": safe_float(now_data.get("temp")),
                    "feelsLike": safe_float(now_data.get("feelsLike")),
                    "text": now_data.get("text", ""),
                    "wind360": safe_float(now_data.get("wind360")),
                    "windDir": now_data.get("windDir", ""),
                    "windScale": safe_float(now_data.get("windScale")),
                    "windSpeed": safe_float(now_data.get("windSpeed")),
                    "humidity": safe_float(now_data.get("humidity")),
                    "precip": safe_float(now_data.get("precip")),
                    "pressure": safe_float(now_data.get("pressure")),
                    "vis": safe_float(now_data.get("vis")),
                    "cloud": safe_float(now_data.get("cloud")) if now_data.get("cloud") else None,
                    "dew": safe_float(now_data.get("dew")) if now_data.get("dew") else None,
                    
                    "sunrise": today_daily.get("sunrise"),
                    "sunset": today_daily.get("sunset"),
                    "moonrise": today_daily.get("moonrise"),
                    "moonset": today_daily.get("moonset"),
                    "moonPhase": today_daily.get("moonPhase", ""),
                    "moonPhaseIcon": today_daily.get("moonPhaseIcon", ""),
                    "tempMax": safe_float(today_daily.get("tempMax")),
                    "tempMin": safe_float(today_daily.get("tempMin")),
                    "iconDay": today_daily.get("iconDay", ""),
                    "textDay": today_daily.get("textDay", ""),
                    "iconNight": today_daily.get("iconNight", ""),
                    "textNight": today_daily.get("textNight", ""),
                    "wind360Day": safe_float(today_daily.get("wind360Day")),
                    "windDirDay": today_daily.get("windDirDay", ""),
                    "windScaleDay": today_daily.get("windScaleDay", ""),
                    "windSpeedDay": safe_float(today_daily.get("windSpeedDay")),
                    "wind360Night": safe_float(today_daily.get("wind360Night")),
                    "windDirNight": today_daily.get("windDirNight", ""),
                    "windScaleNight": today_daily.get("windScaleNight", ""),
                    "windSpeedNight": safe_float(today_daily.get("windSpeedNight")),
                    "uvIndex": safe_float(today_daily.get("uvIndex"))
                }
            }

            if tomorrow_daily:
                response_data["tomorrow"] = {
                    "sunrise": tomorrow_daily.get("sunrise"),
                    "sunset": tomorrow_daily.get("sunset"),
                    "moonrise": tomorrow_daily.get("moonrise"),
                    "moonset": tomorrow_daily.get("moonset"),
                    "moonPhase": tomorrow_daily.get("moonPhase", ""),
                    "moonPhaseIcon": tomorrow_daily.get("moonPhaseIcon", ""),
                    "tempMax": safe_float(tomorrow_daily.get("tempMax")),
                    "tempMin": safe_float(tomorrow_daily.get("tempMin")),
                    "iconDay": tomorrow_daily.get("iconDay", ""),
                    "textDay": tomorrow_daily.get("textDay", ""),
                    "iconNight": tomorrow_daily.get("iconNight", ""),
                    "textNight": tomorrow_daily.get("textNight", ""),
                    "wind360Day": safe_float(tomorrow_daily.get("wind360Day")),
                    "windDirDay": tomorrow_daily.get("windDirDay", ""),
                    "windScaleDay": tomorrow_daily.get("windScaleDay", ""),
                    "windSpeedDay": safe_float(tomorrow_daily.get("windSpeedDay")),
                    "wind360Night": safe_float(tomorrow_daily.get("wind360Night")),
                    "windDirNight": tomorrow_daily.get("windDirNight", ""),
                    "windScaleNight": tomorrow_daily.get("windScaleNight", ""),
                    "windSpeedNight": safe_float(tomorrow_daily.get("windSpeedNight")),
                    "humidity": safe_float(tomorrow_daily.get("humidity")),
                    "precip": safe_float(tomorrow_daily.get("precip")),
                    "pressure": safe_float(tomorrow_daily.get("pressure")),
                    "vis": safe_float(tomorrow_daily.get("vis")),
                    "cloud": safe_float(tomorrow_daily.get("cloud")) if tomorrow_daily.get("cloud") else None,
                    "uvIndex": safe_float(tomorrow_daily.get("uvIndex"))
                }

            # 在天气数据拉取时就解析传感器标签（由 enable_sensor_label 开关控制）
            if conf().get("enable_sensor_label", False):
                try:
                    from bridge.agent_bridge import _weather_to_sensor_label
                    response_data["sensor_label"] = _weather_to_sensor_label(response_data)
                except Exception as _se:
                    logger.warning(f"[WeatherHandler] sensor_label failed: {_se}")
                    response_data["sensor_label"] = ""
            else:
                response_data["sensor_label"] = ""

            _weather_cache[cache_key] = response_data
            return json.dumps({"success": True, "message": "Success", "data": response_data}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"WeatherHandler error: {e}", exc_info=True)
            event_log.log_exception(
                "weather_failed", e,
                endpoint="/api/weather",
                lat=locals().get("lat", "") or "",
                lon=locals().get("lon", "") or "",
            )
            return json.dumps({"success": False, "message": str(e), "data": None}, ensure_ascii=False)
