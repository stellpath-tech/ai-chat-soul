import sys
import time
import web
import json
import uuid
import base64
import tempfile
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
import channel.web.database as db
from channel.web.metrics import MetricsHandler, metrics_processor, USER_AUTH_TOTAL


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

            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return

            # 如果是 DEVICE 来源的请求，将 AI 回复写入聊天记录队列
            source = context.get("source", "APP")
            device_id = context.get("device_id", "")
            if source == "DEVICE" and device_id and reply.content:
                self._push_chatlog(device_id, "assistant", reply.content)

            # SSE mode: push done event to SSE queue
            if request_id in self.sse_queues:
                content = reply.content if reply.content is not None else ""
                self.sse_queues[request_id].put({
                    "type": "done",
                    "content": content,
                    "request_id": request_id,
                    "timestamp": time.time()
                })
                logger.debug(f"SSE done sent for request {request_id}")
                return

            # Fallback: polling mode
            if session_id in self.session_queues:
                response_data = {
                    "type": str(reply.type),
                    "content": reply.content,
                    "timestamp": time.time(),
                    "request_id": request_id
                }
                self.session_queues[session_id].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.error(f"Error in send method: {e}")

    def _make_sse_callback(self, request_id: str):
        """Build an on_event callback that pushes agent stream events into the SSE queue."""
        def on_event(event: dict):
            if request_id not in self.sse_queues:
                return
            q = self.sse_queues[request_id]
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    q.put({"type": "delta", "content": delta})

            elif event_type == "tool_execution_start":
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                q.put({"type": "tool_start", "tool": tool_name, "arguments": arguments})

            elif event_type == "tool_execution_end":
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                # Truncate long results to avoid huge SSE payloads
                result_str = str(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "…"
                q.put({
                    "type": "tool_end",
                    "tool": tool_name,
                    "status": status,
                    "result": result_str,
                    "execution_time": round(exec_time, 2)
                })

        return on_event

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
            logger.error(f"[WebChannel] register_device error: {e}")
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
            logger.error(f"[WebChannel] list_devices error: {e}")
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
            logger.error(f"[WebChannel] send_pet_events error: {e}")
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
            logger.error(f"[WebChannel] poll_pet_events error: {e}")
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
            logger.error(f"[WebChannel] pull_chatlog error: {e}")
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
            if auth_token:
                user = db.get_user_by_token(auth_token)
                if user:
                    session_id = f"user_{user['id']}"
                else:
                    web.ctx.status = '401 Unauthorized'
                    return json.dumps({"status": "error", "message": "unauthorized", "code": 401})
                    
            if not session_id:
                # 如果没有 token 或 token 无效，回退到原逻辑
                if device_id:
                    session_id = device_id
                else:
                    session_id = json_data.get('session_id', f'session_{int(time.time())}')
                    
            prompt = json_data.get('message', '')
            image_b64 = json_data.get('image', '')   # base64 编码的图片（可选）
            image_url_input = json_data.get('image_url', '')  # OSS/CDN URL（可选，与 image 二选一）
            image_type = json_data.get('image_type', 'jpeg')  # 图片格式，默认 jpeg
            use_sse = json_data.get('stream', True)
            timezone     = json_data.get('timezone')
            sensor_label = json_data.get('sensor_label', '')  # 由前端从 GET /api/weather 拿到后回传

            request_id = self._generate_request_id()
            self.request_to_session[request_id] = session_id

            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            if use_sse:
                self.sse_queues[request_id] = Queue()

            msg = WebMessage(self._generate_msg_id(), prompt or image_b64 or image_url_input)
            msg.from_user_id = session_id

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

                if use_sse:
                    context["on_event"] = self._make_sse_callback(request_id)

                if source == "DEVICE" and device_id:
                    self._push_chatlog(device_id, "user", f"[图片]{(' ' + prompt) if prompt else ''}")

                threading.Thread(target=self.produce, args=(context,)).start()
                return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

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

                if use_sse:
                    context["on_event"] = self._make_sse_callback(request_id)


                if source == "DEVICE" and device_id:
                    self._push_chatlog(device_id, "user", f"[图片]{(' ' + prompt) if prompt else ''}")

                threading.Thread(target=self.produce, args=(context,)).start()
                return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

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

            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            # DEVICE 来源：将用户消息写入聊天记录队列
            if source == "DEVICE" and device_id:
                self._push_chatlog(device_id, "user", prompt)

            threading.Thread(target=self.produce, args=(context,)).start()

            return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def stream_response(self, request_id: str):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        """
        if request_id not in self.sse_queues:
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        q = self.sse_queues[request_id]
        timeout = 300  # 5 minutes max
        deadline = time.time() + timeout

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
                    break
        finally:
            self.sse_queues.pop(request_id, None)

    def poll_response(self):
        """
        Poll for responses using the session_id.
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')
            
            if not session_id or session_id not in self.session_queues:
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
                    "timestamp": response["timestamp"]
                })
                
            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})
                
        except Exception as e:
            logger.error(f"Error polling response: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def chat_page(self):
        """Serve the chat HTML page."""
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def startup(self):
        port = conf().get("web_port", 9899)
        db.init_db()

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
            '/api/invite_code', 'InviteCodeHandler',
            '/api/user_behavior', 'UserBehaviorHandler',
            '/api/weather', 'WeatherHandler',
            '/metrics', 'MetricsHandler',
            '/assets/(.*)', 'AssetsHandler',
        )
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

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
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
            logger.error(f"Error getting config: {e}")
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
            logger.error(f"[WebChannel] Skills API error: {e}")
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
            logger.error(f"[WebChannel] Scheduler API error: {e}")
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
        try:
            data = json.loads(web.data())
            phone_number = data.get('phoneNumber')
            invite_code = data.get('inviteCode')
            if not phone_number or not invite_code:
                USER_AUTH_TOTAL.labels(type="missing_params").inc()
                return json.dumps({"success": False, "message": "Missing phoneNumber or inviteCode", "data": None}, ensure_ascii=False)
            
            success, msg, token, action_type = db.register_or_login(phone_number, invite_code)
            
            USER_AUTH_TOTAL.labels(type=action_type).inc()
            
            if not success:
                return json.dumps({"success": False, "message": msg, "data": None}, ensure_ascii=False)
            
            return json.dumps({"success": True, "message": "Success", "data": {"token": token}}, ensure_ascii=False)
        except Exception as e:
            USER_AUTH_TOTAL.labels(type="unknown_error").inc()
            logger.error(f"AuthRegisterHandler error: {e}")
            return json.dumps({"success": False, "message": "Server error", "data": None})


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
            logger.error(f"InviteCodeHandler error: {e}")
            return json.dumps({"success": False, "message": "Server error", "data": None})

    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            codes = db.list_invite_codes()
            return json.dumps({"success": True, "message": "Success", "data": codes}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"InviteCodeHandler error: {e}")
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
            logger.error(f"UserBehaviorHandler error: {e}")
            return json.dumps({"success": False, "message": "Server error", "data": None})


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

        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)  # 添加更详细的错误信息
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

            api_key = conf().get("qweather_api_key", "1c50ee817a4d4bb2b338043de8c6410d")
            location = f"{grid_lon},{grid_lat}"
            headers = {"X-QW-Api-Key": api_key}

            # Fetch now weather
            now_url = f"https://mt2x88w6bx.re.qweatherapi.com/v7/weather/now?location={location}"
            now_res_obj = requests.get(now_url, headers=headers, timeout=10)
            now_res = now_res_obj.json()

            # Fetch 3d weather
            daily_url = f"https://mt2x88w6bx.re.qweatherapi.com/v7/weather/3d?location={location}"
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
            return json.dumps({"success": False, "message": str(e), "data": None}, ensure_ascii=False)

