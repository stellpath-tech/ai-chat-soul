"""
Agent Initializer - Handles agent initialization logic
"""

import os
import datetime
import time
from typing import Optional, List

from agent.protocol import Agent
from agent.tools import ToolManager
from common.log import logger
from common.utils import expand_path


class AgentInitializer:
    """
    Handles agent initialization including:
    - Workspace setup
    - Memory system initialization  
    - Tool loading
    - System prompt building
    """
    
    def __init__(self, bridge, agent_bridge):
        """
        Initialize agent initializer
        
        Args:
            bridge: COW bridge instance
            agent_bridge: AgentBridge instance (for create_agent method)
        """
        self.bridge = bridge
        self.agent_bridge = agent_bridge
    
    def initialize_agent(self, session_id: Optional[str] = None) -> Agent:
        """
        Initialize agent for a session
        
        Args:
            session_id: Session ID (None for default agent)
        
        Returns:
            Initialized agent instance
        """
        from config import conf
        
        # Get workspace from config
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        
        # For device sessions, give each device its own isolated working directory.
        # Tools (edit/read/write) will use this as cwd, so MEMORY.md and daily notes
        # are written under the device's workspace rather than the global one.
        if session_id:
            agent_workspace = os.path.join(workspace_root, "devices", session_id)
            os.makedirs(agent_workspace, exist_ok=True)
        else:
            agent_workspace = workspace_root
        
        # Migrate API keys
        self._migrate_config_to_env(workspace_root)
        
        # Load environment variables
        self._load_env_file()
        
        # Initialize workspace templates inside the agent's working directory
        from agent.prompt import ensure_workspace, load_context_files, PromptBuilder
        workspace_files = ensure_workspace(agent_workspace, create_templates=True)
        
        if session_id is None:
            logger.info(f"[AgentInitializer] Workspace initialized at: {agent_workspace}")
        else:
            logger.info(f"[AgentInitializer] Device workspace: {agent_workspace}")
        
        # Load tools (cwd = agent_workspace so file edits go to the right place)
        tools = self._load_tools(agent_workspace, session_id)
        
        # Initialize scheduler if needed
        self._initialize_scheduler(tools, session_id)
        
        # Skills are isolated in the agent's specific workspace
        skill_manager = self._initialize_skill_manager(agent_workspace, session_id)

        # Check if first conversation
        from agent.prompt.workspace import is_first_conversation, mark_conversation_started
        is_first = is_first_conversation(agent_workspace)

        # Build system prompt from AGENT.md only
        from agent.prompt.builder import build_companion_system_prompt
        agent_files = load_context_files(agent_workspace, ["AGENT.md"])
        system_prompt = build_companion_system_prompt(agent_files)

        runtime_info = self._get_runtime_info(agent_workspace)

        if is_first:
            mark_conversation_started(agent_workspace)
        
        # Get cost control parameters
        from config import conf
        max_steps = conf().get("agent_max_steps", 20)
        max_context_tokens = conf().get("agent_max_context_tokens", 50000)
        
        # Create agent
        agent = self.agent_bridge.create_agent(
            system_prompt=system_prompt,
            tools=tools,
            max_steps=max_steps,
            output_mode="logger",
            workspace_dir=agent_workspace,
            skill_manager=skill_manager,
            enable_skills=True,
            max_context_tokens=max_context_tokens,
            runtime_info=runtime_info
        )

        return agent
    
    def _load_env_file(self):
        """Load environment variables from .env file"""
        env_file = expand_path("~/.cow/.env")
        if os.path.exists(env_file):
            try:
                from dotenv import load_dotenv
                load_dotenv(env_file, override=True)
            except ImportError:
                logger.warning("[AgentInitializer] python-dotenv not installed")
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to load .env file: {e}")
    
    def _load_tools(self, workspace_root: str, session_id: Optional[str] = None):
        """Load all tools"""
        tool_manager = ToolManager()
        tool_manager.load_tools()

        tools = []
        file_config = {"cwd": workspace_root}
        
        for tool_name in tool_manager.tool_classes.keys():
            try:
                # Skip web_search if no API key is available
                if tool_name == "web_search":
                    from agent.tools.web_search.web_search import WebSearch
                    if not WebSearch.is_available():
                        logger.debug("[AgentInitializer] WebSearch skipped - no BOCHA_API_KEY or LINKAI_API_KEY")
                        continue

                # Special handling for EnvConfig tool
                if tool_name == "env_config":
                    from agent.tools import EnvConfig
                    tool = EnvConfig({"agent_bridge": self.agent_bridge})
                else:
                    tool = tool_manager.create_tool(tool_name)

                if tool:
                    # Apply workspace config to file operation tools
                    if tool_name in ['read', 'write', 'edit', 'bash', 'grep', 'find', 'ls']:
                        tool.config = file_config
                        tool.cwd = file_config.get("cwd", getattr(tool, 'cwd', None))
                    tools.append(tool)
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to load tool {tool_name}: {e}")

        if session_id is None:
            logger.info(f"[AgentInitializer] Loaded {len(tools)} tools: {[t.name for t in tools]}")
        
        return tools
    
    def _initialize_scheduler(self, tools: List, session_id: Optional[str] = None):
        """Initialize scheduler service if needed"""
        if not self.agent_bridge.scheduler_initialized:
            try:
                from agent.tools.scheduler.integration import init_scheduler
                if init_scheduler(self.agent_bridge):
                    self.agent_bridge.scheduler_initialized = True
                    if session_id is None:
                        logger.info("[AgentInitializer] Scheduler service initialized")
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to initialize scheduler: {e}")
        
        # Inject scheduler dependencies
        if self.agent_bridge.scheduler_initialized:
            try:
                from agent.tools.scheduler.integration import get_task_store, get_scheduler_service
                from agent.tools import SchedulerTool
                from config import conf
                
                task_store = get_task_store()
                scheduler_service = get_scheduler_service()
                
                for tool in tools:
                    if isinstance(tool, SchedulerTool):
                        tool.task_store = task_store
                        tool.scheduler_service = scheduler_service
                        if not tool.config:
                            tool.config = {}
                        tool.config["channel_type"] = conf().get("channel_type", "unknown")
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to inject scheduler dependencies: {e}")
    
    def _initialize_skill_manager(self, workspace_root: str, session_id: Optional[str] = None):
        """Initialize skill manager"""
        try:
            from agent.skills import SkillManager
            skill_manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            return skill_manager
        except Exception as e:
            logger.warning(f"[AgentInitializer] Failed to initialize SkillManager: {e}")
            return None
    
    def _get_runtime_info(self, workspace_root: str):
        """Get runtime information with dynamic time support"""
        from config import conf

        runtime_info = {
            "model": conf().get("model", "unknown"),
            "workspace": workspace_root,
            "channel": conf().get("channel_type", "unknown"),
        }

        def get_current_time():
            """Get current time dynamically - called each time system prompt is accessed"""
            # Check for request-specific timezone info injected by AgentBridge
            tz_info = runtime_info.get("request_timezone")

            if tz_info and "tz_offset_min" in tz_info:
                try:
                    offset_min = tz_info["tz_offset_min"]
                    # Offset in minutes (e.g. East 8 is 480)
                    tz = datetime.timezone(datetime.timedelta(minutes=offset_min))
                    now = datetime.datetime.now(tz)

                    # Determine timezone name
                    tz_iana = tz_info.get("tz_iana", "")
                    if tz_iana and "TimezoneInfo" in tz_iana:
                        # Extract "Asia/Shanghai" from "TimezoneInfo(Asia/Shanghai, ...)"
                        import re
                        match = re.search(r'TimezoneInfo\(([^,)]+)', tz_iana)
                        timezone_name = match.group(1) if match else tz_iana
                    elif tz_iana:
                        timezone_name = tz_iana
                    else:
                        hours = offset_min // 60
                        minutes = abs(offset_min) % 60
                        timezone_name = f"UTC{hours:+03d}:{minutes:02d}" if minutes else f"UTC{hours:+03d}"
                except Exception as e:
                    logger.warning(f"Failed to use request timezone: {e}")
                    now = datetime.datetime.now()
                    timezone_name = "UTC"
            else:
                now = datetime.datetime.now()
                # Get local timezone info
                try:
                    offset = -time.timezone if not time.daylight else -time.altzone
                    hours = offset // 3600
                    minutes = (offset % 3600) // 60
                    timezone_name = f"UTC{hours:+03d}:{minutes:02d}" if minutes else f"UTC{hours:+03d}"
                except Exception:
                    timezone_name = "UTC"

            # Chinese weekday mapping
            weekday_map = {
                'Monday': '星期一', 'Tuesday': '星期二', 'Wednesday': '星期三',
                'Thursday': '星期四', 'Friday': '星期五', 'Saturday': '星期六', 'Sunday': '星期日'
            }
            weekday_zh = weekday_map.get(now.strftime("%A"), now.strftime("%A"))

            return {
                'time': now.strftime("%Y-%m-%d %H:%M:%S"),
                'weekday': weekday_zh,
                'timezone': timezone_name
            }

        runtime_info["_get_current_time"] = get_current_time
        return runtime_info
    
    def _migrate_config_to_env(self, workspace_root: str):
        """Migrate API keys from config.json to .env file"""
        from config import conf
        
        key_mapping = {
            "open_ai_api_key": "OPENAI_API_KEY",
            "open_ai_api_base": "OPENAI_API_BASE",
            "gemini_api_key": "GEMINI_API_KEY",
            "claude_api_key": "CLAUDE_API_KEY",
            "linkai_api_key": "LINKAI_API_KEY",
        }
        
        env_file = expand_path("~/.cow/.env")
        
        # Read existing env vars
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
                logger.warning(f"[AgentInitializer] Failed to read .env file: {e}")
        
        # Check which keys need migration
        keys_to_migrate = {}
        for config_key, env_key in key_mapping.items():
            if env_key in existing_env_vars:
                continue
            value = conf().get(config_key, "")
            if value and value.strip():
                keys_to_migrate[env_key] = value.strip()
        
        # Write new keys
        if keys_to_migrate:
            try:
                env_dir = os.path.dirname(env_file)
                if not os.path.exists(env_dir):
                    os.makedirs(env_dir, exist_ok=True)
                if not os.path.exists(env_file):
                    open(env_file, 'a').close()
                
                with open(env_file, 'a', encoding='utf-8') as f:
                    f.write('\n# Auto-migrated from config.json\n')
                    for key, value in keys_to_migrate.items():
                        f.write(f'{key}={value}\n')
                        os.environ[key] = value
                
                logger.info(f"[AgentInitializer] Migrated {len(keys_to_migrate)} API keys to .env: {list(keys_to_migrate.keys())}")
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to migrate API keys: {e}")
