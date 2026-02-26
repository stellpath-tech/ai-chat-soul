import os
import re
import shutil
import threading
import time
from asyncio import CancelledError
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from bridge.context import *
from bridge.reply import *
from bridge.bridge import Bridge
from channel.channel import Channel
from common.dequeue import Dequeue
from common import memory
from common.utils import expand_path
from plugins import *

try:
    from voice.audio_convert import any_to_wav
except Exception as e:
    pass

handler_pool = ThreadPoolExecutor(max_workers=8)  # 澶勭悊娑堟伅鐨勭嚎绋嬫睜


# 鎶借薄绫? 瀹冨寘鍚簡涓庢秷鎭€氶亾鏃犲叧鐨勯€氱敤澶勭悊閫昏緫
class ChatChannel(Channel):
    name = None  # 鐧诲綍鐨勭敤鎴峰悕
    user_id = None  # 鐧诲綍鐨勭敤鎴穒d
    futures = {}  # 璁板綍姣忎釜session_id鎻愪氦鍒扮嚎绋嬫睜鐨刦uture瀵硅薄, 鐢ㄤ簬閲嶇疆浼氳瘽鏃舵妸娌℃墽琛岀殑future鍙栨秷鎺夛紝姝ｅ湪鎵ц鐨勪笉浼氳鍙栨秷
    sessions = {}  # 鐢ㄤ簬鎺у埗骞跺彂锛屾瘡涓猻ession_id鍚屾椂鍙兘鏈変竴涓猚ontext鍦ㄥ鐞?
    lock = threading.Lock()  # 鐢ㄤ簬鎺у埗瀵箂essions鐨勮闂?

    def __init__(self):
        _thread = threading.Thread(target=self.consume)
        _thread.setDaemon(True)
        _thread.start()

    # 鏍规嵁娑堟伅鏋勯€燾ontext锛屾秷鎭唴瀹圭浉鍏崇殑瑙﹀彂椤瑰啓鍦ㄨ繖閲?
    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        # context棣栨浼犲叆鏃讹紝origin_ctype鏄疦one,
        # 寮曞叆鐨勮捣鍥犳槸锛氬綋杈撳叆璇煶鏃讹紝浼氬祵濂楃敓鎴愪袱涓猚ontext锛岀涓€姝ヨ闊宠浆鏂囨湰锛岀浜屾閫氳繃鏂囨湰鐢熸垚鏂囧瓧鍥炲銆?
        # origin_ctype鐢ㄤ簬绗簩姝ユ枃鏈洖澶嶆椂锛屽垽鏂槸鍚﹂渶瑕佸尮閰嶅墠缂€锛屽鏋滄槸绉佽亰鐨勮闊筹紝灏变笉闇€瑕佸尮閰嶅墠缂€
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype
        # context棣栨浼犲叆鏃讹紝receiver鏄疦one锛屾牴鎹被鍨嬭缃畆eceiver
        first_in = "receiver" not in context
        # 缇ゅ悕鍖归厤杩囩▼锛岃缃畇ession_id鍜宺eceiver
        if first_in:  # context棣栨浼犲叆鏃讹紝receiver鏄疦one锛屾牴鎹被鍨嬭缃畆eceiver
            config = conf()
            cmsg = context["msg"]
            user_data = conf().get_user_data(cmsg.from_user_id)
            context["openai_api_key"] = user_data.get("openai_api_key")
            context["gpt_model"] = user_data.get("gpt_model")
            if context.get("isgroup", False):
                group_name = cmsg.other_user_nickname
                group_id = cmsg.other_user_id

                group_name_white_list = config.get("group_name_white_list", [])
                group_name_keyword_white_list = config.get("group_name_keyword_white_list", [])
                if any(
                    [
                        group_name in group_name_white_list,
                        "ALL_GROUP" in group_name_white_list,
                        check_contain(group_name, group_name_keyword_white_list),
                    ]
                ):
                    # Check global group_shared_session config first
                    group_shared_session = conf().get("group_shared_session", True)
                    if group_shared_session:
                        # All users in the group share the same session
                        session_id = group_id
                    else:
                        # Check group-specific whitelist (legacy behavior)
                        group_chat_in_one_session = conf().get("group_chat_in_one_session", [])
                        session_id = cmsg.actual_user_id
                        if any(
                            [
                                group_name in group_chat_in_one_session,
                                "ALL_GROUP" in group_chat_in_one_session,
                            ]
                        ):
                            session_id = group_id
                else:
                    logger.debug(f"No need reply, groupName not in whitelist, group_name={group_name}")
                    return None
                context["session_id"] = session_id
                context["receiver"] = group_id
            else:
                context["session_id"] = cmsg.other_user_id
                context["receiver"] = cmsg.other_user_id
            e_context = PluginManager().emit_event(EventContext(Event.ON_RECEIVE_MESSAGE, {"channel": self, "context": context}))
            context = e_context["context"]
            if e_context.is_pass() or context is None:
                return context
            if cmsg.from_user_id == self.user_id and not config.get("trigger_by_self", True):
                logger.debug("[chat_channel]self message skipped")
                return None

        # 娑堟伅鍐呭鍖归厤杩囩▼锛屽苟澶勭悊content
        if ctype == ContextType.TEXT:
            if first_in and "銆峔n- - - - - - -" in content:  # 鍒濇鍖归厤 杩囨护寮曠敤娑堟伅
                logger.debug(content)
                logger.debug("[chat_channel]reference query skipped")
                return None

            nick_name_black_list = conf().get("nick_name_black_list", [])
            if context.get("isgroup", False):  # 缇よ亰
                # 鏍￠獙鍏抽敭瀛?
                match_prefix = check_prefix(content, conf().get("group_chat_prefix"))
                match_contain = check_contain(content, conf().get("group_chat_keyword"))
                flag = False
                if context["msg"].to_user_id != context["msg"].actual_user_id:
                    if match_prefix is not None or match_contain is not None:
                        flag = True
                        if match_prefix:
                            content = content.replace(match_prefix, "", 1).strip()
                    if context["msg"].is_at:
                        nick_name = context["msg"].actual_user_nickname
                        if nick_name and nick_name in nick_name_black_list:
                            # 榛戝悕鍗曡繃婊?
                            logger.warning(f"[chat_channel] Nickname {nick_name} in In BlackList, ignore")
                            return None

                        logger.info("[chat_channel]receive group at")
                        if not conf().get("group_at_off", False):
                            flag = True
                        self.name = self.name if self.name is not None else ""  # 閮ㄥ垎娓犻亾self.name鍙兘娌℃湁璧嬪€?
                        pattern = f"@{re.escape(self.name)}(\u2005|\u0020)"
                        subtract_res = re.sub(pattern, r"", content)
                        if isinstance(context["msg"].at_list, list):
                            for at in context["msg"].at_list:
                                pattern = f"@{re.escape(at)}(\u2005|\u0020)"
                                subtract_res = re.sub(pattern, r"", subtract_res)
                        if subtract_res == content and context["msg"].self_display_name:
                            # 鍓嶇紑绉婚櫎鍚庢病鏈夊彉鍖栵紝浣跨敤缇ゆ樀绉板啀娆＄Щ闄?
                            pattern = f"@{re.escape(context['msg'].self_display_name)}(\u2005|\u0020)"
                            subtract_res = re.sub(pattern, r"", content)
                        content = subtract_res
                if not flag:
                    if context["origin_ctype"] == ContextType.VOICE:
                        logger.info("[chat_channel]receive group voice, but checkprefix didn't match")
                    return None
            else:  # 鍗曡亰
                nick_name = context["msg"].from_user_nickname
                if nick_name and nick_name in nick_name_black_list:
                    # 榛戝悕鍗曡繃婊?
                    logger.warning(f"[chat_channel] Nickname '{nick_name}' in In BlackList, ignore")
                    return None

                match_prefix = check_prefix(content, conf().get("single_chat_prefix", [""]))
                if match_prefix is not None:  # 鍒ゆ柇濡傛灉鍖归厤鍒拌嚜瀹氫箟鍓嶇紑锛屽垯杩斿洖杩囨护鎺夊墠缂€+绌烘牸鍚庣殑鍐呭
                    content = content.replace(match_prefix, "", 1).strip()
                elif context["origin_ctype"] == ContextType.VOICE:  # 濡傛灉婧愭秷鎭槸绉佽亰鐨勮闊虫秷鎭紝鍏佽涓嶅尮閰嶅墠缂€锛屾斁瀹芥潯浠?
                    pass
                else:
                    logger.info("[chat_channel]receive single chat msg, but checkprefix didn't match")
                    return None
            content = content.strip()
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix",[""]))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            if "desire_rtype" not in context and conf().get("always_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        elif context.type == ContextType.VOICE:
            if "desire_rtype" not in context and conf().get("voice_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        return context

    def _handle(self, context: Context):
        if context is None or not context.content:
            return
        logger.debug("[chat_channel] handling context: {}".format(context))
        # reply鐨勬瀯寤烘楠?
        reply = self._generate_reply(context)

        logger.debug("[chat_channel] decorating reply: {}".format(reply))

        # reply鐨勫寘瑁呮楠?
        if reply and reply.content:
            reply = self._decorate_reply(context, reply)

            # reply鐨勫彂閫佹楠?
            self._send_reply(context, reply)

    def _generate_reply(self, context: Context, reply: Reply = Reply()) -> Reply:
        e_context = PluginManager().emit_event(
            EventContext(
                Event.ON_HANDLE_CONTEXT,
                {"channel": self, "context": context, "reply": reply},
            )
        )
        reply = e_context["reply"]
        if not e_context.is_pass():
            logger.debug("[chat_channel] type={}, content={}".format(context.type, context.content))
            if context.type == ContextType.TEXT or context.type == ContextType.IMAGE_CREATE:  # 鏂囧瓧鍜屽浘鐗囨秷鎭?
                if context.type == ContextType.TEXT:
                    reset_reply = self._handle_reset_command(context)
                    if reset_reply:
                        return reset_reply
                context["channel"] = e_context["channel"]
                reply = super().build_reply_content(context.content, context)
            elif context.type == ContextType.VOICE:  # 璇煶娑堟伅
                cmsg = context["msg"]
                cmsg.prepare()
                file_path = context.content
                wav_path = os.path.splitext(file_path)[0] + ".wav"
                try:
                    any_to_wav(file_path, wav_path)
                except Exception as e:  # 杞崲澶辫触锛岀洿鎺ヤ娇鐢╩p3锛屽浜庢煇浜沘pi锛宮p3涔熷彲浠ヨ瘑鍒?
                    logger.warning("[chat_channel]any to wav error, use raw path. " + str(e))
                    wav_path = file_path
                # 璇煶璇嗗埆
                reply = super().build_voice_to_text(wav_path)
                # 鍒犻櫎涓存椂鏂囦欢
                try:
                    os.remove(file_path)
                    if wav_path != file_path:
                        os.remove(wav_path)
                except Exception as e:
                    pass
                    # logger.warning("[chat_channel]delete temp file error: " + str(e))

                if reply.type == ReplyType.TEXT:
                    new_context = self._compose_context(ContextType.TEXT, reply.content, **context.kwargs)
                    if new_context:
                        reply = self._generate_reply(new_context)
                    else:
                        return
            elif context.type == ContextType.IMAGE:  # 图片消息：先准备文件，再缓存并尝试触发识图
                cmsg = context.get("msg")
                if cmsg:
                    cmsg.prepare()
                image_path = os.path.abspath(context.content)
                context.content = image_path
                memory.USER_IMAGE_CACHE[context["session_id"]] = {
                    "path": image_path,
                    "msg": context.get("msg")
                }
                if conf().get("image_recognition", True):
                    use_agent_for_image = conf().get("image_use_agent", True) and conf().get("agent", False)
                    if use_agent_for_image:
                        # Stage 1: use vision-capable model adapter to parse image content.
                        base_vision_reply = Bridge().fetch_reply_content(image_path, context)
                        if base_vision_reply and base_vision_reply.type != ReplyType.ERROR:
                            # Stage 2: let agent rephrase with persona + conversation context.
                            prompt = conf().get(
                                "image_agent_prompt",
                                "请结合当前人设与会话上下文，对这张图片给出有温度的反馈。"
                            )
                            agent_context = Context(
                                ContextType.TEXT,
                                f"{prompt}\n\n图片识别结果：\n{base_vision_reply.content}",
                                dict(context.kwargs)
                            )
                            agent_context["origin_ctype"] = ContextType.IMAGE
                            agent_context["image_feedback_mode"] = True
                            vision_reply = super().build_reply_content(agent_context.content, agent_context)
                        else:
                            vision_reply = base_vision_reply
                    else:
                        vision_reply = Bridge().fetch_reply_content(image_path, context)
                    if vision_reply and vision_reply.type != ReplyType.ERROR:
                        reply = vision_reply
                    else:
                        # Fallback for text-only model adapters that support image marker in text.
                        prompt = conf().get("image_recognition_prompt", "请描述这张图片的主要内容。")
                        fallback_context = Context(ContextType.TEXT, f"{prompt}\n[图片: {image_path}]", dict(context.kwargs))
                        fallback_context["origin_ctype"] = ContextType.IMAGE
                        fallback_reply = super().build_reply_content(fallback_context.content, fallback_context)
                        if fallback_reply and fallback_reply.type != ReplyType.ERROR:
                            reply = fallback_reply
                        else:
                            reply = Reply(
                                ReplyType.ERROR,
                                "当前模型未开启识图能力，请切换支持视觉的模型或配置对应API Key。"
                            )
                else:
                    return
            elif context.type == ContextType.SHARING:  # 鍒嗕韩淇℃伅锛屽綋鍓嶆棤榛樿閫昏緫
                pass
            elif context.type == ContextType.FUNCTION or context.type == ContextType.FILE:  # 鏂囦欢娑堟伅鍙婂嚱鏁拌皟鐢ㄧ瓑锛屽綋鍓嶆棤榛樿閫昏緫
                pass
            else:
                logger.warning("[chat_channel] unknown context type: {}".format(context.type))
                return
        return reply

    def _handle_reset_command(self, context: Context):
        query = (context.content or "").strip()
        reset_commands = set(conf().get("reset_commands", ["重置", "#重置"]))
        if query not in reset_commands:
            return None

        session_id = context.get("session_id")
        self._clear_short_term_memory(session_id)
        self._clear_long_term_memory()

        opening = conf().get(
            "reset_opening_message",
            "重置好啦✨我是满仓，要先吃点小饼干吗？"
        )
        logger.info(f"[chat_channel] reset command handled, session_id={session_id}")
        return Reply(ReplyType.TEXT, opening)

    def _clear_short_term_memory(self, session_id):
        try:
            if session_id:
                self.cancel_session(session_id)
                memory.USER_IMAGE_CACHE.pop(session_id, None)
        except Exception as e:
            logger.warning(f"[chat_channel] cancel session failed: {e}")

        try:
            bridge = Bridge()
            chat_bot = bridge.get_bot("chat")
            if hasattr(chat_bot, "sessions") and hasattr(chat_bot.sessions, "clear_session") and session_id:
                chat_bot.sessions.clear_session(session_id)
        except Exception as e:
            logger.warning(f"[chat_channel] clear bot session failed: {e}")

        try:
            if conf().get("agent", False) and session_id:
                bridge = Bridge()
                bridge.get_agent_bridge().clear_session(session_id)
        except Exception as e:
            logger.warning(f"[chat_channel] clear agent session failed: {e}")

    def _clear_long_term_memory(self):
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        memory_file = os.path.join(workspace_root, "MEMORY.md")
        memory_dir = os.path.join(workspace_root, "memory")

        try:
            os.makedirs(workspace_root, exist_ok=True)
            with open(memory_file, "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            logger.warning(f"[chat_channel] clear MEMORY.md failed: {e}")

        try:
            if os.path.isdir(memory_dir):
                shutil.rmtree(memory_dir, ignore_errors=True)
            os.makedirs(memory_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"[chat_channel] clear memory directory failed: {e}")

        try:
            from agent.memory.summarizer import create_memory_files_if_needed
            create_memory_files_if_needed(Path(workspace_root))
        except Exception as e:
            logger.warning(f"[chat_channel] re-create memory files failed: {e}")

    def _decorate_reply(self, context: Context, reply: Reply) -> Reply:
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_DECORATE_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            desire_rtype = context.get("desire_rtype")
            if not e_context.is_pass() and reply and reply.type:
                if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                    logger.error("[chat_channel]reply type not support: " + str(reply.type))
                    reply.type = ReplyType.ERROR
                    reply.content = "涓嶆敮鎸佸彂閫佺殑娑堟伅绫诲瀷: " + str(reply.type)

                if reply.type == ReplyType.TEXT:
                    reply_text = reply.content
                    if desire_rtype == ReplyType.VOICE and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                        reply = super().build_text_to_voice(reply.content)
                        return self._decorate_reply(context, reply)
                    if context.get("isgroup", False):
                        if not context.get("no_need_at", False):
                            reply_text = "@" + context["msg"].actual_user_nickname + "\n" + reply_text.strip()
                        reply_text = conf().get("group_chat_reply_prefix", "") + reply_text + conf().get("group_chat_reply_suffix", "")
                    else:
                        reply_text = conf().get("single_chat_reply_prefix", "") + reply_text + conf().get("single_chat_reply_suffix", "")
                    reply.content = reply_text
                elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
                    reply.content = "[" + str(reply.type) + "]\n" + reply.content
                elif reply.type == ReplyType.IMAGE_URL or reply.type == ReplyType.VOICE or reply.type == ReplyType.IMAGE or reply.type == ReplyType.FILE or reply.type == ReplyType.VIDEO or reply.type == ReplyType.VIDEO_URL:
                    pass
                else:
                    logger.error("[chat_channel] unknown reply type: {}".format(reply.type))
                    return
            if desire_rtype and desire_rtype != reply.type and reply.type not in [ReplyType.ERROR, ReplyType.INFO]:
                logger.warning("[chat_channel] desire_rtype: {}, but reply type: {}".format(context.get("desire_rtype"), reply.type))
            return reply

    def _send_reply(self, context: Context, reply: Reply):
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_SEND_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            if not e_context.is_pass() and reply and reply.type:
                logger.debug("[chat_channel] sending reply: {}, context: {}".format(reply, context))
                
                # 濡傛灉鏄枃鏈洖澶嶏紝灏濊瘯鎻愬彇骞跺彂閫佸浘鐗?
                if reply.type == ReplyType.TEXT:
                    self._extract_and_send_images(reply, context)
                # 濡傛灉鏄浘鐗囧洖澶嶄絾甯︽湁鏂囨湰鍐呭锛屽厛鍙戞枃鏈啀鍙戝浘鐗?
                elif reply.type == ReplyType.IMAGE_URL and hasattr(reply, 'text_content') and reply.text_content:
                    # 鍏堝彂閫佹枃鏈?
                    text_reply = Reply(ReplyType.TEXT, reply.text_content)
                    self._send(text_reply, context)
                    # 鐭殏寤惰繜鍚庡彂閫佸浘鐗?
                    time.sleep(0.3)
                    self._send(reply, context)
                else:
                    self._send(reply, context)
    
    def _extract_and_send_images(self, reply: Reply, context: Context):
        """
        浠庢枃鏈洖澶嶄腑鎻愬彇鍥剧墖/瑙嗛URL骞跺崟鐙彂閫?
        鏀寔鏍煎紡锛歔鍥剧墖: /path/to/image.png], [瑙嗛: /path/to/video.mp4], ![](url), <img src="url">
        鏈€澶氬彂閫?涓獟浣撴枃浠?
        """
        content = reply.content
        media_items = []  # [(url, type), ...]
        
        # 姝ｅ垯鎻愬彇鍚勭鏍煎紡鐨勫獟浣揢RL
        patterns = [
            (r'\[鍥剧墖:\s*([^\]]+)\]', 'image'),   # [鍥剧墖: /path/to/image.png]
            (r'\[瑙嗛:\s*([^\]]+)\]', 'video'),   # [瑙嗛: /path/to/video.mp4]
            (r'!\[.*?\]\(([^\)]+)\)', 'image'),   # ![alt](url) - 榛樿鍥剧墖
            (r'<img[^>]+src=["\']([^"\']+)["\']', 'image'),  # <img src="url">
            (r'<video[^>]+src=["\']([^"\']+)["\']', 'video'),  # <video src="url">
            (r'https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp)', 'image'),  # 鐩存帴鐨勫浘鐗嘦RL
            (r'https?://[^\s]+\.(?:mp4|avi|mov|wmv|flv)', 'video'),  # 鐩存帴鐨勮棰慤RL
        ]
        
        for pattern, media_type in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                media_items.append((match, media_type))
        
        # 鍘婚噸锛堜繚鎸侀『搴忥級骞堕檺鍒舵渶澶?涓?
        seen = set()
        unique_items = []
        for url, mtype in media_items:
            if url not in seen:
                seen.add(url)
                unique_items.append((url, mtype))
        media_items = unique_items[:5]
        
        if media_items:
            logger.info(f"[chat_channel] Extracted {len(media_items)} media item(s) from reply")
            
            # 鍏堝彂閫佹枃鏈紙淇濇寔鍘熸枃鏈笉鍙橈級
            logger.info(f"[chat_channel] Sending text content before media: {reply.content[:100]}...")
            self._send(reply, context)
            logger.info(f"[chat_channel] Text sent, now sending {len(media_items)} media item(s)")
            
            # 鐒跺悗閫愪釜鍙戦€佸獟浣撴枃浠?
            for i, (url, media_type) in enumerate(media_items):
                try:
                    # 鍒ゆ柇鏄湰鍦版枃浠惰繕鏄疷RL
                    if url.startswith(('http://', 'https://')):
                        # 缃戠粶璧勬簮
                        if media_type == 'video':
                            # 瑙嗛浣跨敤 FILE 绫诲瀷鍙戦€?
                            media_reply = Reply(ReplyType.FILE, url)
                            media_reply.file_name = os.path.basename(url)
                        else:
                            # 鍥剧墖浣跨敤 IMAGE_URL 绫诲瀷
                            media_reply = Reply(ReplyType.IMAGE_URL, url)
                    elif os.path.exists(url):
                        # 鏈湴鏂囦欢
                        if media_type == 'video':
                            # 瑙嗛浣跨敤 FILE 绫诲瀷锛岃浆鎹负 file:// URL
                            media_reply = Reply(ReplyType.FILE, f"file://{url}")
                            media_reply.file_name = os.path.basename(url)
                        else:
                            # 鍥剧墖浣跨敤 IMAGE_URL 绫诲瀷锛岃浆鎹负 file:// URL
                            media_reply = Reply(ReplyType.IMAGE_URL, f"file://{url}")
                    else:
                        logger.warning(f"[chat_channel] Media file not found or invalid URL: {url}")
                        continue
                    
                    # 鍙戦€佸獟浣撴枃浠讹紙娣诲姞灏忓欢杩熼伩鍏嶉鐜囬檺鍒讹級
                    if i > 0:
                        time.sleep(0.5)
                    self._send(media_reply, context)
                    logger.info(f"[chat_channel] Sent {media_type} {i+1}/{len(media_items)}: {url[:50]}...")
                    
                except Exception as e:
                    logger.error(f"[chat_channel] Failed to send {media_type} {url}: {e}")
        else:
            # 娌℃湁濯掍綋鏂囦欢锛屾甯稿彂閫佹枃鏈?
                self._send(reply, context)

    def _send(self, reply: Reply, context: Context, retry_cnt=0):
        try:
            self.send(reply, context)
        except Exception as e:
            logger.error("[chat_channel] sendMsg error: {}".format(str(e)))
            if isinstance(e, NotImplementedError):
                return
            logger.exception(e)
            if retry_cnt < 2:
                time.sleep(3 + 3 * retry_cnt)
                self._send(reply, context, retry_cnt + 1)

    def _success_callback(self, session_id, **kwargs):  # 绾跨▼姝ｅ父缁撴潫鏃剁殑鍥炶皟鍑芥暟
        logger.debug("Worker return success, session_id = {}".format(session_id))

    def _fail_callback(self, session_id, exception, **kwargs):  # 绾跨▼寮傚父缁撴潫鏃剁殑鍥炶皟鍑芥暟
        logger.exception("Worker return exception: {}".format(exception))

    def _thread_pool_callback(self, session_id, **kwargs):
        def func(worker: Future):
            try:
                worker_exception = worker.exception()
                if worker_exception:
                    self._fail_callback(session_id, exception=worker_exception, **kwargs)
                else:
                    self._success_callback(session_id, **kwargs)
            except CancelledError as e:
                logger.info("Worker cancelled, session_id = {}".format(session_id))
            except Exception as e:
                logger.exception("Worker raise exception: {}".format(e))
            with self.lock:
                self.sessions[session_id][1].release()

        return func

    def produce(self, context: Context):
        session_id = context["session_id"]
        with self.lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = [
                    Dequeue(),
                    threading.BoundedSemaphore(conf().get("concurrency_in_session", 4)),
                ]
            if context.type == ContextType.TEXT and context.content.startswith("#"):
                self.sessions[session_id][0].putleft(context)  # 浼樺厛澶勭悊绠＄悊鍛戒护
            else:
                self.sessions[session_id][0].put(context)

    # 娑堣垂鑰呭嚱鏁帮紝鍗曠嫭绾跨▼锛岀敤浜庝粠娑堟伅闃熷垪涓彇鍑烘秷鎭苟澶勭悊
    def consume(self):
        while True:
            with self.lock:
                session_ids = list(self.sessions.keys())
            for session_id in session_ids:
                with self.lock:
                    context_queue, semaphore = self.sessions[session_id]
                if semaphore.acquire(blocking=False):  # 绛夌嚎绋嬪鐞嗗畬姣曟墠鑳藉垹闄?
                    if not context_queue.empty():
                        context = context_queue.get()
                        logger.debug("[chat_channel] consume context: {}".format(context))
                        future: Future = handler_pool.submit(self._handle, context)
                        future.add_done_callback(self._thread_pool_callback(session_id, context=context))
                        with self.lock:
                            if session_id not in self.futures:
                                self.futures[session_id] = []
                            self.futures[session_id].append(future)
                    elif semaphore._initial_value == semaphore._value + 1:  # 闄や簡褰撳墠锛屾病鏈変换鍔″啀鐢宠鍒颁俊鍙烽噺锛岃鏄庢墍鏈変换鍔￠兘澶勭悊瀹屾瘯
                        with self.lock:
                            self.futures[session_id] = [t for t in self.futures[session_id] if not t.done()]
                            assert len(self.futures[session_id]) == 0, "thread pool error"
                            del self.sessions[session_id]
                    else:
                        semaphore.release()
            time.sleep(0.2)

    # 鍙栨秷session_id瀵瑰簲鐨勬墍鏈変换鍔★紝鍙兘鍙栨秷鎺掗槦鐨勬秷鎭拰宸叉彁浜ょ嚎绋嬫睜浣嗘湭鎵ц鐨勪换鍔?
    def cancel_session(self, session_id):
        with self.lock:
            if session_id in self.sessions:
                for future in self.futures[session_id]:
                    future.cancel()
                cnt = self.sessions[session_id][0].qsize()
                if cnt > 0:
                    logger.info("Cancel {} messages in session {}".format(cnt, session_id))
                self.sessions[session_id][0] = Dequeue()

    def cancel_all_session(self):
        with self.lock:
            for session_id in self.sessions:
                for future in self.futures[session_id]:
                    future.cancel()
                cnt = self.sessions[session_id][0].qsize()
                if cnt > 0:
                    logger.info("Cancel {} messages in session {}".format(cnt, session_id))
                self.sessions[session_id][0] = Dequeue()


def check_prefix(content, prefix_list):
    if not prefix_list:
        return None
    for prefix in prefix_list:
        if content.startswith(prefix):
            return prefix
    return None


def check_contain(content, keyword_list):
    if not keyword_list:
        return None
    for ky in keyword_list:
        if content.find(ky) != -1:
            return True
    return None
