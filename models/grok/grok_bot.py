# encoding:utf-8

import requests
from models.moonshot.moonshot_bot import MoonshotBot
from models.session_manager import SessionManager
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf, load_config
from .grok_session import GrokSession


class GrokBot(MoonshotBot):
    """
    xAI Grok bot (OpenAI-compatible /chat/completions style)
    """

    def __init__(self):
        super().__init__()
        self.sessions = SessionManager(GrokSession, model=conf().get("model") or "grok-2-latest")
        self.args = {
            "model": conf().get("model") or "grok-2-latest",
            "temperature": conf().get("temperature", 0.3),
            "top_p": conf().get("top_p", 1.0),
        }
        self.api_key = conf().get("grok_api_key")
        self.base_url = conf().get("grok_api_base", "https://api.x.ai/v1")
        self.proxy = conf().get("proxy")
        if self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url.rsplit("/chat/completions", 1)[0]
        if self.base_url.endswith("/"):
            self.base_url = self.base_url.rstrip("/")

    def reply(self, query, context=None):
        if context.type != ContextType.TEXT:
            return Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))

        logger.info("[GROK] query={}".format(query))
        session_id = context["session_id"]
        reply = None
        clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
        if query in clear_memory_commands:
            self.sessions.clear_session(session_id)
            reply = Reply(ReplyType.INFO, "记忆已清除")
        elif query == "#清除所有":
            self.sessions.clear_all_session()
            reply = Reply(ReplyType.INFO, "所有人记忆已清除")
        elif query == "#更新配置":
            load_config()
            reply = Reply(ReplyType.INFO, "配置已更新")
        if reply:
            return reply

        session = self.sessions.session_query(query, session_id)
        model = context.get("grok_model")
        new_args = self.args.copy()
        if model:
            new_args["model"] = model

        reply_content = self.reply_text(session, args=new_args)
        if reply_content["completion_tokens"] == 0 and len(reply_content["content"]) > 0:
            return Reply(ReplyType.ERROR, reply_content["content"])
        elif reply_content["completion_tokens"] > 0:
            self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
            return Reply(ReplyType.TEXT, reply_content["content"])
        return Reply(ReplyType.ERROR, reply_content["content"])

    def reply_text(self, session: GrokSession, args=None, retry_count: int = 0) -> dict:
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key
            }
            body = args.copy()
            body["messages"] = session.messages
            res = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            )
            if res.status_code == 200:
                response = res.json()
                return {
                    "total_tokens": response["usage"]["total_tokens"],
                    "completion_tokens": response["usage"]["completion_tokens"],
                    "content": response["choices"][0]["message"]["content"]
                }
            response = res.json()
            error = response.get("error", {})
            logger.error(f"[GROK] chat failed, status_code={res.status_code}, msg={error.get('message')}, type={error.get('type')}")
            if res.status_code == 401:
                return {"completion_tokens": 0, "content": "授权失败，请检查 Grok API Key 是否正确"}
            if res.status_code == 429:
                return {"completion_tokens": 0, "content": "请求过于频繁，请稍后再试"}
            return {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}
        except Exception as e:
            logger.exception(e)
            return {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}

