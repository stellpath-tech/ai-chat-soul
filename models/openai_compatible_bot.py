# encoding:utf-8

"""
OpenAI-Compatible Bot Base Class

Provides a common implementation for bots that are compatible with OpenAI's API format.
This includes: OpenAI, LinkAI, Azure OpenAI, and many third-party providers.
"""

import json
import httpx
import openai
from common.log import logger


def _is_connection_error(exc: Exception) -> bool:
    """SSL / TCP 连接类错误：代理挂掉时立即失败，无需重试，直接触发 fallback。"""
    name = type(exc).__name__
    keywords = ("Connection", "Timeout", "SSL", "Network", "Transport",
                "RemoteProtocol", "ReadTimeout", "ConnectError", "APIConnection")
    if any(k in name for k in keywords):
        return True
    module = getattr(type(exc), "__module__", "") or ""
    if "httpx" in module or "ssl" in module:
        return True
    # openai SDK 把底层连接错误包装成 APIConnectionError
    if isinstance(exc, openai.APIConnectionError):
        return True
    return False


class OpenAICompatibleBot:
    """
    Base class for OpenAI-compatible bots.
    
    Provides common tool calling implementation that can be inherited by:
    - ChatGPTBot
    - LinkAIBot  
    - OpenAIBot
    - AzureChatGPTBot
    - Other OpenAI-compatible providers
    
    Subclasses only need to override get_api_config() to provide their specific API settings.
    """
    
    def get_api_config(self):
        """
        Get API configuration for this bot.
        
        Subclasses should override this to provide their specific config.
        
        Returns:
            dict: {
                'api_key': str,
                'api_base': str (optional),
                'model': str,
                'default_temperature': float,
                'default_top_p': float,
                'default_frequency_penalty': float,
                'default_presence_penalty': float,
            }
        """
        raise NotImplementedError("Subclasses must implement get_api_config()")
    
    def call_with_tools(self, messages, tools=None, stream=False, **kwargs):
        """
        Call OpenAI-compatible API with tool support for agent integration
        
        This method handles:
        1. Format conversion (Claude format → OpenAI format)
        2. System prompt injection
        3. API calling with proper configuration
        4. Error handling
        
        Args:
            messages: List of messages (may be in Claude format from agent)
            tools: List of tool definitions (may be in Claude format from agent)
            stream: Whether to use streaming
            **kwargs: Additional parameters (max_tokens, temperature, system, etc.)
            
        Returns:
            Formatted response in OpenAI format or generator for streaming
        """
        try:
            # Get API configuration from subclass
            api_config = self.get_api_config()
            
            # Convert messages from Claude format to OpenAI format
            messages = self._convert_messages_to_openai_format(messages)
            
            # Convert tools from Claude format to OpenAI format
            if tools:
                tools = self._convert_tools_to_openai_format(tools)
            
            # Handle system prompt (OpenAI uses system message, Claude uses separate parameter)
            system_prompt = kwargs.get('system')
            if system_prompt:
                # Add system message at the beginning if not already present
                if not messages or messages[0].get('role') != 'system':
                    messages = [{"role": "system", "content": system_prompt}] + messages
                else:
                    # Replace existing system message
                    messages[0] = {"role": "system", "content": system_prompt}
            
            # Build request parameters
            request_params = {
                "model": kwargs.get("model", api_config.get('model', 'gpt-3.5-turbo')),
                "messages": messages,
                "temperature": kwargs.get("temperature", api_config.get('default_temperature', 0.9)),
                "top_p": kwargs.get("top_p", api_config.get('default_top_p', 1.0)),
                "frequency_penalty": kwargs.get("frequency_penalty", api_config.get('default_frequency_penalty', 0.0)),
                "presence_penalty": kwargs.get("presence_penalty", api_config.get('default_presence_penalty', 0.0)),
                "stream": stream
            }
            
            # Add max_tokens if specified
            if kwargs.get("max_tokens"):
                request_params["max_tokens"] = kwargs["max_tokens"]

            # Disable thinking mode for Qwen3 hybrid models (DashScope compatible-mode).
            # These default to thinking ON, but we discard reasoning_content downstream,
            # so the thinking tokens are pure cost + latency. Pass enable_thinking=False
            # via extra_body. Allow override through kwargs/api_config when needed.
            _model_name = str(request_params.get("model", ""))
            if _model_name.startswith(("qwen", "qwq", "qvq")):
                _enable_thinking = kwargs.get("enable_thinking",
                                              api_config.get("enable_thinking", False))
                request_params["extra_body"] = {"enable_thinking": _enable_thinking}

            # Add tools if provided
            if tools:
                request_params["tools"] = tools
                request_params["tool_choice"] = kwargs.get("tool_choice", "auto")
            
            # Make API call with proper configuration
            api_key = api_config.get('api_key')
            api_base = api_config.get('api_base')
            
            if stream:
                return self._handle_stream_response(request_params, api_key, api_base)
            else:
                return self._handle_sync_response(request_params, api_key, api_base)
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[{self.__class__.__name__}] call_with_tools error: {error_msg}")
            if stream:
                def error_generator():
                    yield {
                        "error": True,
                        "message": error_msg,
                        "status_code": 500
                    }
                return error_generator()
            else:
                return {
                    "error": True,
                    "message": error_msg,
                    "status_code": 500
                }
    
    # openai v1.x has openai.OpenAI; v0.x does not
    _OPENAI_V1 = hasattr(openai, "OpenAI")

    def _make_client(self, api_key, api_base):
        """创建 openai v1.x 客户端（仅 v1.x 可用）"""
        _timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=10.0)
        # 非 OpenAI 官方地址（如国内豆包 Ark）直连，不走系统代理
        # trust_env=False 才能真正禁用 HTTP_PROXY/ALL_PROXY 等环境变量代理；proxy=None 无效
        if api_base and "openai.com" not in api_base:
            http_client = httpx.Client(timeout=_timeout, trust_env=False)
        else:
            http_client = httpx.Client(timeout=_timeout)
        kwargs = {"http_client": http_client, "max_retries": 0}
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["base_url"] = api_base
        return openai.OpenAI(**kwargs)

    def _handle_sync_response(self, request_params, api_key, api_base):
        """Handle synchronous OpenAI API response"""
        try:
            if self._OPENAI_V1:
                client = self._make_client(api_key, api_base)
                response = client.chat.completions.create(**request_params)
                return response.model_dump()
            else:
                kwargs = dict(request_params)
                if api_key:
                    kwargs["api_key"] = api_key
                if api_base:
                    kwargs["api_base"] = api_base
                kwargs.setdefault("request_timeout", 20)
                return openai.ChatCompletion.create(**kwargs)
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] sync response error: {e}")
            return {"error": True, "message": str(e), "status_code": 500}

    def _handle_stream_response(self, request_params, api_key, api_base):
        """Handle streaming OpenAI API response"""
        try:
            if self._OPENAI_V1:
                client = self._make_client(api_key, api_base)
                stream = client.chat.completions.create(**request_params)
                for chunk in stream:
                    yield chunk.model_dump()
            else:
                kwargs = dict(request_params)
                if api_key:
                    kwargs["api_key"] = api_key
                if api_base:
                    kwargs["api_base"] = api_base
                kwargs.setdefault("request_timeout", 20)  # v0.x 超时，防止代理挂死
                stream = openai.ChatCompletion.create(**kwargs)
                for chunk in stream:
                    yield chunk
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] stream response error: {e}")
            if _is_connection_error(e):
                raise  # 连接/SSL 错误直接抛出，由上层 fallback 处理
            yield {"error": True, "message": str(e), "status_code": 500}
    
    def _convert_tools_to_openai_format(self, tools):
        """
        Convert tools from Claude format to OpenAI format
        
        Claude format: {name, description, input_schema}
        OpenAI format: {type: "function", function: {name, description, parameters}}
        """
        if not tools:
            return None
        
        openai_tools = []
        for tool in tools:
            # Check if already in OpenAI format
            if 'type' in tool and tool['type'] == 'function':
                openai_tools.append(tool)
            else:
                # Convert from Claude format
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "parameters": tool.get("input_schema", {})
                    }
                })
        
        return openai_tools
    
    def _convert_messages_to_openai_format(self, messages):
        """
        Convert messages from Claude format to OpenAI format
        
        Claude uses content blocks with types like 'tool_use', 'tool_result'
        OpenAI uses 'tool_calls' in assistant messages and 'tool' role for results
        """
        if not messages:
            return []
        
        openai_messages = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            
            # Handle string content (already in correct format)
            if isinstance(content, str):
                openai_messages.append(msg)
                continue
            
            # Handle list content (Claude format with content blocks)
            if isinstance(content, list):
                # Check if this is a tool result message (user role with tool_result blocks)
                if role == "user" and any(block.get("type") == "tool_result" for block in content):
                    # Separate text content and tool_result blocks
                    text_parts = []
                    tool_results = []

                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            tool_results.append(block)

                    # First, add tool result messages (must come immediately after assistant with tool_calls)
                    for block in tool_results:
                        tool_call_id = block.get("tool_use_id") or ""
                        if not tool_call_id:
                            logger.warning(f"[OpenAICompatible] tool_result missing tool_use_id, using empty string")
                        # Ensure content is a string (some providers require string content)
                        result_content = block.get("content", "")
                        if not isinstance(result_content, str):
                            result_content = json.dumps(result_content, ensure_ascii=False)
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result_content
                        })

                    # Then, add text content as a separate user message if present
                    if text_parts:
                        openai_messages.append({
                            "role": "user",
                            "content": " ".join(text_parts)
                        })

                # Check if this is an assistant message with tool_use blocks
                elif role == "assistant":
                    # Separate text content and tool_use blocks
                    text_parts = []
                    tool_calls = []

                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_id = block.get("id") or ""
                            if not tool_id:
                                logger.warning(f"[OpenAICompatible] tool_use missing id for '{block.get('name')}'")
                            tool_calls.append({
                                "id": tool_id,
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {}))
                                }
                            })

                    # Build OpenAI format assistant message
                    openai_msg = {
                        "role": "assistant",
                        "content": " ".join(text_parts) if text_parts else None
                    }

                    if tool_calls:
                        openai_msg["tool_calls"] = tool_calls

                    openai_messages.append(openai_msg)
                else:
                    # Other list content, keep as is
                    openai_messages.append(msg)
            else:
                # Other formats, keep as is
                openai_messages.append(msg)
        
        return openai_messages
