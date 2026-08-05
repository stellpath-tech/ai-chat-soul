"""
Agent Bridge - Integrates Agent system with existing COW bridge
"""

import os
import time
from typing import Optional, List

from agent.protocol import Agent, LLMModel, LLMRequest
from agent.session.store import SessionStore
from bridge.agent_event_handler import AgentEventHandler
from bridge.agent_initializer import AgentInitializer
from bridge.bridge import Bridge
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from common import const
from common.log import logger

try:
    from common import event_log
except Exception:
    class _NoopEventLog:
        @staticmethod
        def log(*args, **kwargs): pass
        @staticmethod
        def log_exception(*args, **kwargs): pass
    event_log = _NoopEventLog()
from common.utils import expand_path
from models.openai_compatible_bot import OpenAICompatibleBot
from agent.utils.hehe_harness import (
    apply_consecutive_hehe_harness,
    last_assistant_text,
    replace_last_assistant_text,
)


_REQUEST_RETRY_DELAY_SECONDS = 15
_REQUEST_MAX_RETRIES = 1
_REQUEST_RETRYABLE_KEYWORDS = (
    "ssl", "eof", "connection", "timeout", "timed out", "network",
    "transport", "remoteprotocol", "readtimeout", "connecterror",
    "apiconnection", "rate limit", "overloaded", "unavailable", "busy",
    "429", "500", "502", "503", "504", "512",
)

def _is_retryable_request_error(msg: str) -> bool:
    """Return whether a failed provider request is safe to retry once."""
    lower = msg.lower()
    return any(k in lower for k in _REQUEST_RETRYABLE_KEYWORDS)


def _chunk_has_response_payload(chunk: dict) -> bool:
    """Detect content that could already have reached the user or tool runner."""
    if not isinstance(chunk, dict):
        return False
    choices = chunk.get("choices") or []
    if not choices:
        return False
    delta = choices[0].get("delta") or {}
    return bool(delta.get("content") or delta.get("tool_calls"))


def _image_to_data_url(image_path: str) -> str:
    import base64

    ext = os.path.splitext(image_path)[1].lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _build_direct_image_message(query: str, image_path: str = None, image_url: str = None) -> List[dict]:
    from config import conf

    detail = conf().get("image_agent_openai_detail", "auto")
    if image_url:
        url = image_url  # 公开 URL 直接传给模型，无需服务器下载
    else:
        url = _image_to_data_url(image_path)
    image_block = {
        "type": "image_url",
        "image_url": {"url": url},
    }
    if detail in {"auto", "low", "high"}:
        image_block["image_url"]["detail"] = detail

    content = []
    if query and query.strip():
        content.append({"type": "text", "text": query.strip()})
    content.append(image_block)
    return content


_sensor_label_cache: dict = {}  # (temp_bucket, humidity_bucket, text) -> label str


def _weather_to_sensor_label(weather_data: dict) -> str:
    """Call Doubao text model to convert raw weather data to sensor label(s).

    Returns lines like:
        温度感知：舒适
        湿度感知：潮湿
    """
    now = weather_data.get("now", {})
    temp     = now.get("temp", 0)
    feels    = now.get("feelsLike", temp)
    humidity = now.get("humidity", 0)
    text     = now.get("text", "")

    cache_key = (round(float(temp) / 2) * 2, round(float(humidity) / 10) * 10, text)
    if cache_key in _sensor_label_cache:
        return _sensor_label_cache[cache_key]

    from config import conf
    api_key   = conf().get("ark_api_key", "")
    base_url  = conf().get("ark_base_url", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model     = conf().get("doubao_text_model", "doubao-lite-32k")

    prompt = "\n".join([
        "根据天气数据选出适用的传感器标签，每行一个，格式为[类别:标签]，不要解释。",
        "",
        f"温度:{temp}C 体感:{feels}C 湿度:{humidity}% 天气:{text}",
        "",
        "温度标签(必选一个): 温度感知:寒冷 / 温度感知:凉爽 / 温度感知:舒适 / 温度感知:温暖 / 温度感知:炎热",
        "湿度标签(湿度>=75才选): 湿度感知:潮湿",
        "只输出标签行，用换行分隔，不要方括号。",
    ])

    import requests
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 32, "temperature": 0},
        timeout=10,
    )
    resp.raise_for_status()
    label = resp.json()["choices"][0]["message"]["content"].strip()
    _sensor_label_cache[cache_key] = label
    return label


def _describe_image_with_doubao(image_path: str) -> str:
    """Call Doubao VL to create a compact visual memory for follow-up turns."""
    from config import conf

    vl_model = conf().get("doubao_vl_model", "doubao-seed-2-0-mini-260215")
    api_key = conf().get("ark_api_key", "")
    base_url = conf().get("ark_base_url", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    prompt = conf().get(
        "image_agent_memory_prompt",
        (
            "请为这张图片生成一段可用于后续对话追问的精准视觉记忆。"
            "用中文，120-220字，尽量客观具体。覆盖：主体和场景、空间布局、"
            "装潢/材质/色彩/灯光、人物数量和动作、可见文字或招牌、桌面物品、"
            "氛围，以及不确定但可能相关的细节。不要寒暄，不要编造看不清的内容。"
        ),
    )

    import requests
    payload = {
        "model": vl_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _image_to_data_url(image_path)}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": conf().get("image_agent_memory_max_tokens", 280),
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _describe_image_with_doubao_url(image_url: str) -> str:
    """Pass public OSS URL directly to Doubao VL (no server-side download)."""
    import requests
    from config import conf

    vl_model = conf().get("doubao_vl_model", "doubao-seed-2-0-mini-260215")
    api_key = conf().get("ark_api_key", "")
    base_url = conf().get("ark_base_url", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    prompt = conf().get(
        "image_agent_memory_prompt",
        (
            "请为这张图片生成一段可用于后续对话追问的精准视觉记忆。"
            "用中文，120-220字，尽量客观具体。覆盖：主体和场景、空间布局、"
            "装潢/材质/色彩/灯光、人物数量和动作、可见文字或招牌、桌面物品、"
            "氛围，以及不确定但可能相关的细节。不要寒暄，不要编造看不清的内容。"
        ),
    )

    payload = {
        "model": vl_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": conf().get("image_agent_memory_max_tokens", 280),
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def add_openai_compatible_support(bot_instance):
    """
    Dynamically add OpenAI-compatible tool calling support to a bot instance.
    
    This allows any bot to gain tool calling capability without modifying its code,
    as long as it uses OpenAI-compatible API format.
    
    Note: Some bots like ZHIPUAIBot have native tool calling support and don't need enhancement.
    """
    if hasattr(bot_instance, 'call_with_tools'):
        # Bot already has tool calling support (e.g., ZHIPUAIBot)
        logger.debug(f"[AgentBridge] {type(bot_instance).__name__} already has native tool calling support")
        return bot_instance

    # Create a temporary mixin class that combines the bot with OpenAI compatibility
    class EnhancedBot(bot_instance.__class__, OpenAICompatibleBot):
        """Dynamically enhanced bot with OpenAI-compatible tool calling"""

        def get_api_config(self):
            """
            Infer API config from common configuration patterns.
            Most OpenAI-compatible bots use similar configuration.
            """
            from config import conf

            return {
                'api_key': conf().get("open_ai_api_key"),
                'api_base': conf().get("open_ai_api_base"),
                'model': conf().get("model", "gpt-3.5-turbo"),
                'default_temperature': conf().get("temperature", 0.9),
                'default_top_p': conf().get("top_p", 1.0),
                'default_frequency_penalty': conf().get("frequency_penalty", 0.0),
                'default_presence_penalty': conf().get("presence_penalty", 0.0),
            }

    # Change the bot's class to the enhanced version
    bot_instance.__class__ = EnhancedBot
    logger.info(
        f"[AgentBridge] Enhanced {bot_instance.__class__.__bases__[0].__name__} with OpenAI-compatible tool calling")

    return bot_instance


class AgentLLMModel(LLMModel):
    """
    LLM Model adapter that uses COW's existing bot infrastructure
    """

    def __init__(self, bridge: Bridge, bot_type: str = "chat"):
        # Get model name directly from config
        from config import conf
        model_name = conf().get("model", const.GPT_41)
        super().__init__(model=model_name)
        self.bridge = bridge
        self.bot_type = bot_type
        self._bot = None
        self._use_linkai = conf().get("use_linkai", False) and conf().get("linkai_api_key")
    
    @property
    def bot(self):
        """Lazy load the bot and enhance it with tool calling if needed"""
        if self._bot is None:
            # If use_linkai is enabled, use LinkAI bot directly
            if self._use_linkai:
                self._bot = self.bridge.find_chat_bot(const.LINKAI)
            else:
                self._bot = self.bridge.get_bot(self.bot_type)
                # Automatically add tool calling support if not present
                self._bot = add_openai_compatible_support(self._bot)
            
            # Log bot info
            bot_name = type(self._bot).__name__
        return self._bot

    def call(self, request: LLMRequest):
        """
        Call the model using COW's bot infrastructure
        """
        try:
            # For non-streaming calls, we'll use the existing reply method
            # This is a simplified implementation
            if hasattr(self.bot, 'call_with_tools'):
                # Use tool-enabled call if available
                kwargs = {
                    'messages': request.messages,
                    'tools': getattr(request, 'tools', None),
                    'stream': False,
                    'model': self.model  # Pass model parameter
                }
                # Only pass max_tokens if it's explicitly set
                if request.max_tokens is not None:
                    kwargs['max_tokens'] = request.max_tokens
                
                # Extract system prompt if present
                system_prompt = getattr(request, 'system', None)
                if system_prompt:
                    kwargs['system'] = system_prompt
                
                response = self.bot.call_with_tools(**kwargs)
                return self._format_response(response)
            else:
                # Fallback to regular call
                # This would need to be implemented based on your specific needs
                raise NotImplementedError("Regular call not implemented yet")
                
        except Exception as e:
            logger.error(f"AgentLLMModel call error: {e}")
            raise
    
    def _stream_with_model(self, request: LLMRequest, model_id: str, bot=None):
        """Inner generator: call bot with a specific model_id."""
        b = bot if bot is not None else self.bot
        if not hasattr(b, 'call_with_tools'):
            raise NotImplementedError(f"Bot {type(b).__name__} does not support call_with_tools.")
        system_prompt = getattr(request, 'system', None)
        kwargs = {
            'messages': request.messages,
            'tools': getattr(request, 'tools', None),
            'stream': True,
            'model': model_id,
        }
        if request.max_tokens is not None:
            kwargs['max_tokens'] = request.max_tokens
        if system_prompt:
            kwargs['system'] = system_prompt
        for chunk in b.call_with_tools(**kwargs):
            formatted = self._format_stream_chunk(chunk)
            if isinstance(formatted, dict) and formatted.get('error'):
                msg = formatted.get('message', 'API error')
                # Raise every provider error here so call_stream owns the one
                # sequential retry and never leaks two competing responses.
                raise Exception(msg)
            yield formatted

    def _make_fallback_bot(self):
        """Build an OpenAI-compatible bot pointed at the fallback API endpoint."""
        from config import conf
        from models.openai_compatible_bot import OpenAICompatibleBot
        _api_key  = conf().get("fallback_api_key", "") or conf().get("ark_api_key", "")
        _api_base = conf().get("fallback_api_base", "") or conf().get("ark_base_url", "")

        class FallbackBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    'api_key': _api_key,
                    'api_base': _api_base,
                    'model': conf().get("fallback_model", ""),
                    'default_temperature': conf().get("temperature", 0.9),
                    'default_top_p': conf().get("top_p", 1.0),
                    'default_frequency_penalty': conf().get("frequency_penalty", 0.0),
                    'default_presence_penalty': conf().get("presence_penalty", 0.0),
                }

        return FallbackBot()

    def call_stream(self, request: LLMRequest):
        """
        Stream from the primary model with one sequential retry after 15 seconds.

        A retry is only allowed before any text or tool call has been emitted.
        This prevents a late/partial first response from being combined with a
        second response. Fallback runs only after the primary retry is exhausted.
        """
        from config import conf
        fallback_model = conf().get("fallback_model", "")
        primary_error = None
        primary_error_is_retryable = False

        for attempt in range(_REQUEST_MAX_RETRIES + 1):
            emitted_payload = False
            try:
                for chunk in self._stream_with_model(request, self.model):
                    if _chunk_has_response_payload(chunk):
                        emitted_payload = True
                    yield chunk
                return
            except Exception as e:
                primary_error = e
                primary_error_is_retryable = _is_retryable_request_error(str(e))
                logger.error(
                    f"AgentLLMModel call_stream error "
                    f"(model={self.model}, attempt={attempt + 1}): {e}",
                    exc_info=True,
                )

                if emitted_payload:
                    logger.warning(
                        "[AgentLLMModel] partial response already emitted; "
                        "skip retry and fallback to prevent conflicting responses"
                    )
                    raise RuntimeError(f"[PARTIAL_RESPONSE_ABORT] {e}") from e

                can_retry = (
                    attempt < _REQUEST_MAX_RETRIES
                    and primary_error_is_retryable
                )
                if can_retry:
                    logger.warning(
                        f"[AgentLLMModel] retrying primary model once in "
                        f"{_REQUEST_RETRY_DELAY_SECONDS}s"
                    )
                    time.sleep(_REQUEST_RETRY_DELAY_SECONDS)
                    continue
                break

        if fallback_model and primary_error_is_retryable:
            logger.warning(f"[AgentLLMModel] switching to fallback model: {fallback_model!r}")
            try:
                fallback_bot = self._make_fallback_bot()
                yield from self._stream_with_model(request, fallback_model, bot=fallback_bot)
                return
            except Exception as fallback_error:
                logger.error(
                    f"[AgentLLMModel] fallback model also failed: {fallback_error}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"[REQUEST_RETRY_EXHAUSTED] {fallback_error}"
                ) from fallback_error

        raise RuntimeError(f"[REQUEST_RETRY_EXHAUSTED] {primary_error}") from primary_error
    
    def _format_response(self, response):
        """Format Claude response to our expected format"""
        # This would need to be implemented based on Claude's response format
        return response
    
    def _format_stream_chunk(self, chunk):
        """Format Claude stream chunk to our expected format"""
        # This would need to be implemented based on Claude's stream format
        return chunk


class AgentBridge:
    """
    Bridge class that integrates super Agent with COW
    Manages multiple agent instances per session for conversation isolation
    """
    
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.agents = {}             # session_id -> Agent instance (内存)
        self.default_agent = None
        self.agent: Optional[Agent] = None
        self.scheduler_initialized = False
        self._session_last_active: dict[str, float] = {}  # session_id -> timestamp

        from config import conf
        self.workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))

        # Create helper instances
        self.initializer = AgentInitializer(bridge, self)

        # 一次性初始化共享资源（system_prompt / model / tools / skill_manager）
        self._init_shared_resources()

        # 统一驱逐线程：idle > 180s → flush UserCache → 清出 agents dict
        self._start_eviction_thread()

    @staticmethod
    def _sanitize_image_blocks_for_history(messages: List[dict]) -> None:
        """Replace inline image blocks after a run so persisted history stays small."""
        for msg in messages or []:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            sanitized = []
            changed = False
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    sanitized.append({"type": "text", "text": "[image omitted after this turn]"})
                    changed = True
                else:
                    sanitized.append(block)
            if changed:
                msg["content"] = sanitized

    def create_agent(self, system_prompt: str, tools: List = None, **kwargs) -> Agent:
        """
        Create the super agent with COW integration
        
        Args:
            system_prompt: System prompt
            tools: List of tools (optional)
            **kwargs: Additional agent parameters
            
        Returns:
            Agent instance
        """
        # Create LLM model that uses COW's bot infrastructure
        model = AgentLLMModel(self.bridge)
        
        # Default tools if none provided
        if tools is None:
            # Use ToolManager to load all available tools
            from agent.tools import ToolManager
            tool_manager = ToolManager()
            tool_manager.load_tools()
            
            tools = []
            for tool_name in tool_manager.tool_classes.keys():
                try:
                    tool = tool_manager.create_tool(tool_name)
                    if tool:
                        tools.append(tool)
                except Exception as e:
                    logger.warning(f"[AgentBridge] Failed to load tool {tool_name}: {e}")
        
        # Create agent instance
        agent = Agent(
            system_prompt=system_prompt,
            description=kwargs.get("description", "AI Super Agent"),
            model=model,
            tools=tools,
            max_steps=kwargs.get("max_steps", 15),
            output_mode=kwargs.get("output_mode", "logger"),
            workspace_dir=kwargs.get("workspace_dir"),  # Pass workspace for skills loading
            enable_skills=kwargs.get("enable_skills", True),  # Enable skills by default
            max_context_tokens=kwargs.get("max_context_tokens"),
            context_reserve_tokens=kwargs.get("context_reserve_tokens"),
            runtime_info=kwargs.get("runtime_info")  # Pass runtime_info for dynamic time updates
        )

        # Log skill loading details
        if agent.skill_manager:
            logger.debug(f"[AgentBridge] SkillManager initialized with {len(agent.skill_manager.skills)} skills")

        return agent
    
    def get_agent(self, session_id: str = None) -> Optional[Agent]:
        """
        Get agent instance for the given session
        
        Args:
            session_id: Session identifier (e.g., user_id). If None, returns default agent.
        
        Returns:
            Agent instance for this session
        """
        # If no session_id, use default agent (backward compatibility)
        if session_id is None:
            if self.default_agent is None:
                self._init_default_agent()
            return self.default_agent
        
        # Check if agent exists for this session
        if session_id not in self.agents:
            self._init_agent_for_session(session_id)

        import time
        self._session_last_active[session_id] = time.time()
        return self.agents[session_id]
    
    def _init_shared_resources(self):
        """一次性初始化所有 session 共享的资源（启动时调用一次）。"""
        from config import conf
        from agent.prompt import load_context_files
        from agent.prompt.builder import build_companion_system_prompt

        # API key 迁移 / env 加载只需做一次
        self.initializer._migrate_config_to_env(self.workspace_root)
        self.initializer._load_env_file()

        # system_prompt：从根 workspace 的 AGENT.md 读取，所有 session 共享
        from agent.prompt import ensure_workspace
        ensure_workspace(self.workspace_root, create_templates=True)
        agent_files = load_context_files(self.workspace_root, ["AGENT.md"])
        self._shared_system_prompt = build_companion_system_prompt(agent_files)

        # model：无状态，所有 session 共享同一实例
        self._shared_model = AgentLLMModel(self.bridge)

        # tools：以根 workspace 为 cwd 加载，session 内通过 workspace_dir 覆盖
        self._shared_tools = self.initializer._load_tools(self.workspace_root, session_id=None)

        # skill_manager：全局技能，不区分 session
        self._shared_skill_manager = self.initializer._initialize_skill_manager(
            self.workspace_root, session_id=None
        )

        # runtime_info 模板（时区 / 传感器在每次请求时动态注入）
        self._shared_runtime_info = self.initializer._get_runtime_info(self.workspace_root)
        from config import conf
        self._shared_runtime_info["_max_steps"] = conf().get("agent_max_steps", 20)
        self._shared_runtime_info["_max_context_tokens"] = conf().get("agent_max_context_tokens", 50000)

        logger.info("[AgentBridge] Shared resources initialized")

    def _start_eviction_thread(self):
        """每 60s 扫一次，把 idle > 180s 的 session flush 到 DB 并从内存清除。"""
        import threading, time

        def _loop():
            while True:
                time.sleep(60)
                try:
                    self._evict_stale()
                except Exception as e:
                    logger.warning(f"[AgentBridge] eviction error: {e}")

        threading.Thread(target=_loop, daemon=True, name="agent-session-evict").start()

    def _evict_stale(self):
        import time
        from agent.memory.user_cache import flush as cache_flush
        now = time.time()
        stale = [sid for sid, t in list(self._session_last_active.items()) if now - t > 180]
        for session_id in stale:
            try:
                cache_flush(self.workspace_root, session_id)
            except Exception as e:
                logger.warning(f"[AgentBridge] flush failed for {session_id}: {e}")
            self.agents.pop(session_id, None)
            self._session_last_active.pop(session_id, None)
            logger.info(f"[AgentBridge] Evicted idle session: {session_id}")

    def _init_default_agent(self):
        """Initialize default super agent (backward compat)."""
        agent = self.initializer.initialize_agent(session_id=None)
        self.default_agent = agent

    def _init_agent_for_session(self, session_id: str):
        """新 session：复用共享资源，只初始化 per-session 的部分。"""
        import time
        from agent.memory.user_cache import get as cache_get
        from agent.memory.thing_memory.store import _get_cached

        # 确保 device workspace 目录存在
        device_workspace = os.path.join(self.workspace_root, "devices", session_id)
        os.makedirs(device_workspace, exist_ok=True)

        # runtime_info 是 per-request 的，复制共享模板
        runtime_info = dict(self._shared_runtime_info)

        # 用共享资源创建 Agent，只有 workspace_dir 是 per-session 的
        agent = Agent(
            system_prompt=self._shared_system_prompt,
            model=self._shared_model,
            tools=self._shared_tools,
            skill_manager=self._shared_skill_manager,
            enable_skills=True,
            workspace_dir=device_workspace,
            max_steps=self._shared_runtime_info.get("_max_steps", 20),
            max_context_tokens=self._shared_runtime_info.get("_max_context_tokens", 50000),
            output_mode="logger",
            runtime_info=runtime_info,
        )

        # 从磁盘缓存恢复对话（文件 > DB）
        cached = cache_get(self.workspace_root, session_id)
        if cached and cached.get("conversation"):
            agent.messages = cached["conversation"]
            logger.info(f"[AgentBridge] Restored {len(agent.messages)} messages for session={session_id}")

        self.agents[session_id] = agent
        self._session_last_active[session_id] = time.time()

        # 预热记忆缓存
        try:
            _get_cached(self.workspace_root, session_id)
        except Exception:
            pass
    
    def agent_reply(self, query: str, context: Context = None, 
                   on_event=None, clear_history: bool = False) -> Reply:
        """
        Use super agent to reply to a query
        
        Args:
            query: User query
            context: COW context (optional, contains session_id for user isolation)
            on_event: Event callback (optional)
            clear_history: Whether to clear conversation history
            
        Returns:
            Reply object
        """
        try:
            # Extract session_id from context for user isolation
            session_id = None
            if context:
                session_id = context.kwargs.get("session_id") or context.get("session_id")
            
            # Get agent for this session (will auto-initialize if needed)
            agent = self.get_agent(session_id=session_id)
            if not agent:
                return Reply(ReplyType.ERROR, "Failed to initialize super agent")

            # Inject request-specific timezone into agent's runtime_info
            if context and context.get("timezone"):
                agent.runtime_info["request_timezone"] = context.get("timezone")
            else:
                agent.runtime_info.pop("request_timezone", None)

            # 每轮从磁盘缓存读取最新昵称注入 runtime_info
            if session_id:
                try:
                    from agent.memory.user_cache import get as _cache_get
                    _cached = _cache_get(self.workspace_root, session_id)
                    _nick = _cached.get("nickname") if _cached else None
                    if _nick:
                        agent.runtime_info["user_nickname"] = _nick
                    else:
                        agent.runtime_info.pop("user_nickname", None)
                except Exception:
                    pass

            # 每轮从 user 表读取曾用名（used_nickname）注入 runtime_info（session_id = user_{id}）
            _user_former = []
            if session_id and session_id.startswith("user_"):
                try:
                    import json as _json
                    from agent.memory.thing_memory.store import _conn, db_path as _dbpath
                    _uid = int(session_id[5:])
                    with _conn(_dbpath(self.workspace_root)) as _dbconn:
                        _row = _dbconn.execute(
                            "SELECT used_nickname FROM user WHERE id=?", (_uid,)
                        ).fetchone()
                    if _row:
                        _user_former = _json.loads(_row["used_nickname"] or "[]")
                except Exception:
                    pass
            agent.runtime_info["user_former_names"] = _user_former

            # 前端在 GET /api/weather 时已拿到 sensor_label，直接注入（由 enable_sensor_label 开关控制）
            from config import conf as _conf
            if _conf().get("enable_sensor_label", False):
                sensor_label = context.get("sensor_label") if context else None
                if sensor_label:
                    agent.runtime_info["sensor_label"] = sensor_label
                else:
                    agent.runtime_info.pop("sensor_label", None)
            else:
                agent.runtime_info.pop("sensor_label", None)

            previous_assistant_text = last_assistant_text(agent.messages)
            
            # Create event handler for logging and channel communication
            event_handler = AgentEventHandler(context=context, original_callback=on_event)
            
            # Filter tools based on context
            original_tools = agent.tools
            filtered_tools = original_tools
            is_image_feedback = bool(
                context
                and (
                    context.get("origin_ctype") == ContextType.IMAGE
                    or context.get("type") == ContextType.IMAGE
                )
            )
            
            # If this is a scheduled task execution, exclude scheduler tool to prevent recursion
            if context and context.get("is_scheduled_task"):
                filtered_tools = [tool for tool in agent.tools if tool.name != "scheduler"]
                agent.tools = filtered_tools
                logger.info(f"[AgentBridge] Scheduled task execution: excluded scheduler tool ({len(filtered_tools)}/{len(original_tools)} tools)")
            elif is_image_feedback:
                # In image feedback flow, disable `send` to avoid "帮你发送图片" mis-trigger.
                disabled_tools = {"send", "bash", "read"}
                filtered_tools = [tool for tool in agent.tools if tool.name not in disabled_tools]
                agent.tools = filtered_tools
                logger.info(f"[AgentBridge] Image feedback mode: excluded tools {sorted(disabled_tools)} ({len(filtered_tools)}/{len(original_tools)} tools)")
            else:
                # Attach context to scheduler tool if present
                if context and agent.tools:
                    for tool in agent.tools:
                        if tool.name == "scheduler":
                            try:
                                from agent.tools.scheduler.integration import attach_scheduler_to_tool
                                attach_scheduler_to_tool(tool, context)
                            except Exception as e:
                                logger.warning(f"[AgentBridge] Failed to attach context to scheduler: {e}")
                            break
            
            append_system = context.get("append_system_prompt") if context else None

            # Image feedback supports two multimodal paths:
            # - doubao_summary: describe via Doubao VL, then pass text to the main model.
            # - openai_direct: pass image_url blocks directly to the main model.
            image_url_ctx = context.get("image_url") if context else None
            image_path = context.get("image_path") if context else None
            if image_url_ctx:
                # OSS/CDN URL path: no file I/O, pass URL directly
                from config import conf
                image_mode = str(conf().get("image_agent_multimodal_mode", "doubao_summary") or "doubao_summary").lower()
                if image_mode in {"openai", "openai_direct", "direct", "gpt4o", "gpt-4o",
                                  "both", "openai_with_summary", "direct_with_summary"}:
                    user_message = _build_direct_image_message(query, image_url=image_url_ctx)
                    try:
                        desc = _describe_image_with_doubao_url(image_url_ctx)
                        logger.info(f"[AgentBridge] Doubao image memory (url): {desc}")
                        user_message.insert(0, {"type": "text", "text": f"[image memory: {desc}]"})
                    except Exception as e:
                        logger.warning(f"[AgentBridge] Doubao image memory failed: {e}, using direct image input only")
                    logger.info("[AgentBridge] Image feedback via OSS URL (no base64)")
                else:
                    try:
                        desc = _describe_image_with_doubao_url(image_url_ctx)
                        logger.info(f"[AgentBridge] Doubao image memory (url): {desc}")
                        wrapped = f"[{desc}]"
                        user_message = f"{query}\n{wrapped}".strip() if query and query.strip() else wrapped
                    except Exception as e:
                        logger.warning(f"[AgentBridge] Doubao image memory failed: {e}, falling back to text-only")
                        user_message = query or ""
            elif image_path and os.path.exists(image_path):
                from config import conf

                image_mode = str(conf().get("image_agent_multimodal_mode", "doubao_summary") or "doubao_summary").lower()
                if image_mode in {"openai", "openai_direct", "direct", "gpt4o", "gpt-4o"}:
                    user_message = _build_direct_image_message(query, image_path=image_path)
                    try:
                        desc = _describe_image_with_doubao(image_path)
                        logger.info(f"[AgentBridge] Doubao image memory: {desc}")
                        user_message.insert(0, {"type": "text", "text": f"[image memory: {desc}]"})
                    except Exception as e:
                        logger.warning(f"[AgentBridge] Doubao image memory failed: {e}, using direct image input only")
                    logger.info("[AgentBridge] Image feedback using direct OpenAI-compatible image input")
                elif image_mode in {"both", "openai_with_summary", "direct_with_summary"}:
                    user_message = _build_direct_image_message(query, image_path=image_path)
                    try:
                        desc = _describe_image_with_doubao(image_path)
                        logger.info(f"[AgentBridge] Doubao image memory: {desc}")
                        user_message.insert(0, {"type": "text", "text": f"[image memory: {desc}]"})
                    except Exception as e:
                        logger.warning(f"[AgentBridge] Doubao image memory failed: {e}, using direct image input only")
                else:
                    try:
                        desc = _describe_image_with_doubao(image_path)
                        logger.info(f"[AgentBridge] Doubao image memory: {desc}")
                        wrapped = f"[{desc}]"
                        user_message = f"{query}\n{wrapped}".strip() if query and query.strip() else wrapped
                    except Exception as e:
                        logger.warning(f"[AgentBridge] Doubao image memory failed: {e}, falling back to text-only")
                        user_message = query or ""
            else:
                user_message = query

            # Input redline filter
            if _conf().get("redline_input_filter_enabled", True):
                from agent.utils.redline import filter_input
                if isinstance(user_message, str):
                    user_message, _in_trigger = filter_input(user_message)
                    if _in_trigger:
                        logger.info(f"[Redline] input blocked: type={_in_trigger.type} matched={_in_trigger.matched!r}")
                elif isinstance(user_message, list):
                    for block in user_message:
                        if isinstance(block, dict) and block.get("type") == "text":
                            block["text"], _in_trigger = filter_input(block.get("text", ""))
                            if _in_trigger:
                                logger.info(f"[Redline] input blocked: type={_in_trigger.type} matched={_in_trigger.matched!r}")
            # ThingMemory: inject relevant memories into system prompt
            if _conf().get("thing_memory_enabled", True) and session_id:
                try:
                    from agent.memory.thing_memory import get_memory_block
                    _tm_workspace = expand_path(_conf().get("agent_workspace", "~/cow"))
                    _mem_block = get_memory_block(_tm_workspace, session_id, session_id)
                    if _mem_block:
                        append_system = f"{append_system}\n\n{_mem_block}".strip() if append_system else _mem_block
                        logger.debug(f"[ThingMemory] injected {len(_mem_block)} chars for session={session_id}")
                except Exception as _tme:
                    logger.warning(f"[ThingMemory] get_memory_block error: {_tme}")

            # Keep this after all other per-turn blocks so the selected reply
            # mode is the final sentence in the system prompt.
            if context and "parent_reply_mode" in context:
                from agent.chat.reply_mode import append_reply_mode_instruction
                append_system = append_reply_mode_instruction(
                    append_system,
                    context.get("reply_mode"),
                    context.get("parent_reply_mode"),
                )

            try:
                # Use agent's run_stream method with event handler
                response = agent.run_stream(
                    user_message=user_message,
                    on_event=event_handler.handle_event,
                    clear_history=clear_history,
                    append_system=append_system,
                )
            finally:
                # Restore original tools
                if (context and context.get("is_scheduled_task")) or is_image_feedback:
                    agent.tools = original_tools

                # Log execution summary
                event_handler.log_summary()

            # Output redline filter — retry once if triggered
            if _conf().get("redline_output_filter_enabled", True):
                from agent.utils.redline import filter_output
                _, _out_trigger = filter_output(response)
                if _out_trigger:
                    logger.info(f"[Redline] output triggered: type={_out_trigger.type} matched={_out_trigger.matched!r}, retrying")
                    _RETRY_MSG = "[用户发来了满仓完全看不懂的内容]"
                    try:
                        response = agent.run_stream(
                            user_message=_RETRY_MSG,
                            on_event=event_handler.handle_event,
                            clear_history=False,
                            append_system=append_system,
                        )
                        _, _retry_trigger = filter_output(response)
                        if _retry_trigger:
                            logger.warning(f"[Redline] retry also triggered: type={_retry_trigger.type}")
                    except Exception as _re:
                        logger.warning(f"[Redline] retry failed: {_re}")

            response, _hehe_changed = apply_consecutive_hehe_harness(
                response,
                previous_assistant_text,
            )
            if _hehe_changed:
                replace_last_assistant_text(agent.messages, response)
                logger.info("[HeheHarness] Removed repeated 嘿嘿 from final bridge response")

            self._sanitize_image_blocks_for_history(agent.messages)

            # ThingMemory: fire-and-forget extraction
            if _conf().get("thing_memory_enabled", True) and session_id:
                try:
                    from agent.memory.thing_memory import fire_extract
                    _tm_workspace = expand_path(_conf().get("agent_workspace", "~/cow"))
                    # 优先用提取器专用 key/base，没有则回退到主模型的配置
                    _tm_api_key = (_conf().get("thing_memory_extractor_api_key")
                                   or _conf().get("open_ai_api_key", ""))
                    _tm_api_base = (_conf().get("thing_memory_extractor_api_base")
                                    or _conf().get("open_ai_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
                    _tm_model = _conf().get("thing_memory_extractor_model", "qwen3.5-flash")
                    _tm_user_msgs = []
                    for _m in agent.messages[-10:]:
                        if _m.get("role") == "user":
                            _c = _m.get("content", "")
                            if isinstance(_c, str):
                                _tm_user_msgs.append(_c)
                            elif isinstance(_c, list):
                                _tm_user_msgs.append(
                                    " ".join(p.get("text", "") for p in _c if isinstance(p, dict) and p.get("type") == "text")
                                )
                    if _tm_user_msgs:
                        fire_extract(_tm_workspace, session_id, session_id, _tm_user_msgs, _tm_api_key, _tm_api_base, _tm_model)
                except Exception as _tme:
                    logger.warning(f"[ThingMemory] fire_extract error: {_tme}")

            # 写磁盘备份（flush 到 DB 由驱逐线程负责）
            if session_id:
                try:
                    from agent.memory.user_cache import update_conversation
                    update_conversation(self.workspace_root, session_id, agent.messages)
                except Exception as _uce:
                    logger.debug(f"[UserCache] update_conversation failed: {_uce}")
            
            # Check if there are files to send (from read tool)
            if hasattr(agent, 'stream_executor') and hasattr(agent.stream_executor, 'files_to_send'):
                files_to_send = agent.stream_executor.files_to_send
                if files_to_send:
                    # Send the first file (for now, handle one file at a time)
                    file_info = files_to_send[0]
                    logger.info(f"[AgentBridge] Sending file: {file_info.get('path')}")
                    
                    # Clear files_to_send for next request
                    agent.stream_executor.files_to_send = []
                    
                    # Return file reply based on file type
                    return self._create_file_reply(file_info, response, context)
            
            return Reply(ReplyType.TEXT, response)
            
        except Exception as e:
            logger.exception(f"Agent reply error: {e}")
            # log_exception merges thread-local context (request_id, user_id, ...)
            # bound earlier in chat_channel._handle, so request correlation works.
            event_log.log_exception("agent_failed", e, stage="agent_reply")
            return Reply(ReplyType.ERROR, f"Agent error: {str(e)}")

    def _create_file_reply(self, file_info: dict, text_response: str, context: Context = None) -> Reply:
        """
        Create a reply for sending files
        
        Args:
            file_info: File metadata from read tool
            text_response: Text response from agent
            context: Context object
            
        Returns:
            Reply object for file sending
        """
        file_type = file_info.get("file_type", "file")
        file_path = file_info.get("path")
        
        # For images, use IMAGE_URL type (channel will handle upload)
        if file_type == "image":
            # Convert local path to file:// URL for channel processing
            file_url = f"file://{file_path}"
            logger.info(f"[AgentBridge] Sending image: {file_url}")
            reply = Reply(ReplyType.IMAGE_URL, file_url)
            # Attach text message if present (for channels that support text+image)
            if text_response:
                reply.text_content = text_response  # Store accompanying text
            return reply
        
        # For all file types (document, video, audio), use FILE type
        if file_type in ["document", "video", "audio"]:
            file_url = f"file://{file_path}"
            logger.info(f"[AgentBridge] Sending {file_type}: {file_url}")
            reply = Reply(ReplyType.FILE, file_url)
            reply.file_name = file_info.get("file_name", os.path.basename(file_path))
            # Attach text message if present
            if text_response:
                reply.text_content = text_response
            return reply
        
        # For other unknown file types, return text with file info
        message = text_response or file_info.get("message", "文件已准备")
        message += f"\n\n[文件: {file_info.get('file_name', file_path)}]"
        return Reply(ReplyType.TEXT, message)
    
    def _migrate_config_to_env(self, workspace_root: str):
        """
        Migrate API keys from config.json to .env file if not already set
        
        Args:
            workspace_root: Workspace directory path (not used, kept for compatibility)
        """
        from config import conf
        import os
        
        # Mapping from config.json keys to environment variable names
        key_mapping = {
            "open_ai_api_key": "OPENAI_API_KEY",
            "open_ai_api_base": "OPENAI_API_BASE",
            "gemini_api_key": "GEMINI_API_KEY",
            "claude_api_key": "CLAUDE_API_KEY",
            "linkai_api_key": "LINKAI_API_KEY",
            "grok_api_key": "GROK_API_KEY",
        }
        
        # Use fixed secure location for .env file
        env_file = expand_path("~/.cow/.env")
        
        # Read existing env vars from .env file
        existing_env_vars = {}
        if os.path.exists(env_file):
            try:
                with open(env_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, _ = line.split('=', 1)
                            existing_env_vars[key.strip()] = True
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to read .env file: {e}")
        
        # Check which keys need to be migrated
        keys_to_migrate = {}
        for config_key, env_key in key_mapping.items():
            # Skip if already in .env file
            if env_key in existing_env_vars:
                continue
            
            # Get value from config.json
            value = conf().get(config_key, "")
            if value and value.strip():  # Only migrate non-empty values
                keys_to_migrate[env_key] = value.strip()
        
        # Log summary if there are keys to skip
        if existing_env_vars:
            logger.debug(f"[AgentBridge] {len(existing_env_vars)} env vars already in .env")
        
        # Write new keys to .env file
        if keys_to_migrate:
            try:
                # Ensure ~/.cow directory and .env file exist
                env_dir = os.path.dirname(env_file)
                if not os.path.exists(env_dir):
                    os.makedirs(env_dir, exist_ok=True)
                if not os.path.exists(env_file):
                    open(env_file, 'a').close()
                
                # Append new keys
                with open(env_file, 'a', encoding='utf-8') as f:
                    f.write('\n# Auto-migrated from config.json\n')
                    for key, value in keys_to_migrate.items():
                        f.write(f'{key}={value}\n')
                        # Also set in current process
                        os.environ[key] = value
                
                logger.info(f"[AgentBridge] Migrated {len(keys_to_migrate)} API keys from config.json to .env: {list(keys_to_migrate.keys())}")
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to migrate API keys: {e}")
    
    def clear_session(self, session_id: str):
        """
        Clear a specific session's agent and conversation history

        Args:
            session_id: Session identifier to clear
        """
        if session_id in self.agents:
            logger.info(f"[AgentBridge] Clearing session: {session_id}")
            del self.agents[session_id]
        self._session_last_active.pop(session_id, None)
    
    def clear_all_sessions(self):
        """Clear all agent sessions"""
        logger.info(f"[AgentBridge] Clearing all sessions ({len(self.agents)} total)")
        for session_id in list(self.agents.keys()):
            self.session_store.delete(session_id)
        self.agents.clear()
        self.default_agent = None
    
    def refresh_all_skills(self) -> int:
        """
        Refresh skills and conditional tools in all agent instances after
        environment variable changes. This allows hot-reload without restarting.

        Returns:
            Number of agent instances refreshed
        """
        import os
        from dotenv import load_dotenv
        from config import conf

        # Reload environment variables from .env file
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        env_file = os.path.join(workspace_root, '.env')

        if os.path.exists(env_file):
            load_dotenv(env_file, override=True)
            logger.info(f"[AgentBridge] Reloaded environment variables from {env_file}")

        refreshed_count = 0

        # Collect all agent instances to refresh
        agents_to_refresh = []
        if self.default_agent:
            agents_to_refresh.append(("default", self.default_agent))
        for session_id, agent in self.agents.items():
            agents_to_refresh.append((session_id, agent))

        for label, agent in agents_to_refresh:
            # Refresh skills
            if hasattr(agent, 'skill_manager') and agent.skill_manager:
                agent.skill_manager.refresh_skills()

            # Refresh conditional tools (e.g. web_search depends on API keys)
            self._refresh_conditional_tools(agent)

            refreshed_count += 1

        if refreshed_count > 0:
            logger.info(f"[AgentBridge] Refreshed skills & tools in {refreshed_count} agent instance(s)")

        return refreshed_count

    @staticmethod
    def _refresh_conditional_tools(agent):
        """
        Add or remove conditional tools based on current environment variables.
        For example, web_search should only be present when BOCHA_API_KEY or
        LINKAI_API_KEY is set.
        """
        try:
            from agent.tools.web_search.web_search import WebSearch

            has_tool = any(t.name == "web_search" for t in agent.tools)
            available = WebSearch.is_available()

            if available and not has_tool:
                # API key was added - inject the tool
                tool = WebSearch()
                tool.model = agent.model
                agent.tools.append(tool)
                logger.info("[AgentBridge] web_search tool added (API key now available)")
            elif not available and has_tool:
                # API key was removed - remove the tool
                agent.tools = [t for t in agent.tools if t.name != "web_search"]
                logger.info("[AgentBridge] web_search tool removed (API key no longer available)")
        except Exception as e:
            logger.debug(f"[AgentBridge] Failed to refresh conditional tools: {e}")
