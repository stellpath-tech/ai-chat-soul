# encoding:utf-8

import copy
import json
import logging
import os
import pickle
import sys

from common.log import logger

# 将所有可用配置项写在字典里，键名统一使用小写。
# 这里的值仅用于提示配置格式，程序实际以 config.json（及环境变量）为准。
available_setting = {
    "open_ai_api_key": "",
    "open_ai_api_base": "https://api.openai.com/v1",
    "claude_api_base": "https://api.anthropic.com/v1",
    "gemini_api_base": "https://generativelanguage.googleapis.com",
    "proxy": "",
    "model": "gpt-3.5-turbo",
    "bot_type": "",
    "use_azure_chatgpt": False,
    "azure_deployment_id": "",
    "azure_api_version": "",

    "single_chat_prefix": ["bot", "@bot"],
    "single_chat_reply_prefix": "[bot] ",
    "single_chat_reply_suffix": "",
    "group_chat_prefix": ["@bot"],
    "no_need_at": False,
    "group_chat_reply_prefix": "",
    "group_chat_reply_suffix": "",
    "group_chat_keyword": [],
    "group_at_off": False,
    "group_name_white_list": ["ChatGPT测试群", "ChatGPT测试群2"],
    "group_name_keyword_white_list": [],
    "group_chat_in_one_session": ["ChatGPT测试群"],
    "group_shared_session": True,
    "nick_name_black_list": [],
    "group_welcome_msg": "",
    "trigger_by_self": False,

    "text_to_image": "dall-e-2",
    "dalle3_image_style": "vivid",
    "dalle3_image_quality": "hd",
    "azure_openai_dalle_api_base": "",
    "azure_openai_dalle_api_key": "",
    "azure_openai_dalle_deployment_id": "",
    "image_proxy": True,
    "image_create_prefix": ["画", "看", "找"],
    "concurrency_in_session": 1,
    "image_create_size": "256x256",
    "group_chat_exit_group": False,

    "expires_in_seconds": 3600,
    "character_desc": "你是ChatGPT，一个由OpenAI训练的大型语言模型。",
    "conversation_max_tokens": 1000,
    "rate_limit_chatgpt": 20,
    "rate_limit_dalle": 50,
    "temperature": 0.9,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "request_timeout": 180,
    "timeout": 120,

    "baidu_wenxin_model": "eb-instant",
    "baidu_wenxin_api_key": "",
    "baidu_wenxin_secret_key": "",
    "baidu_wenxin_prompt_enabled": False,

    "xunfei_app_id": "",
    "xunfei_api_key": "",
    "xunfei_api_secret": "",
    "xunfei_domain": "",
    "xunfei_spark_url": "",

    "claude_api_cookie": "",
    "claude_uuid": "",
    "claude_api_key": "",

    "qwen_access_key_id": "",
    "qwen_access_key_secret": "",
    "qwen_agent_key": "",
    "qwen_app_id": "",
    "qwen_node_id": "",
    "dashscope_api_key": "",
    "gemini_api_key": "",

    "wework_smart": True,

    "speech_recognition": True,
    "group_speech_recognition": False,
    "image_recognition": True,
    "image_recognition_prompt": "请描述这张图片的主要内容。",
    "image_use_agent": True,
    "image_agent_prompt": "请结合当前人设与会话上下文，对这张图片给出有温度的反馈。",
    "voice_reply_voice": False,
    "always_reply_voice": False,
    "voice_to_text": "openai",
    "text_to_voice": "openai",
    "text_to_voice_model": "tts-1",
    "tts_voice_id": "alloy",

    "baidu_app_id": "",
    "baidu_api_key": "",
    "baidu_secret_key": "",
    "baidu_dev_pid": 1536,

    "azure_voice_api_key": "",
    "azure_voice_region": "japaneast",

    "xi_api_key": "",
    "xi_voice_id": "",

    "chat_time_module": False,
    "chat_start_time": "00:00",
    "chat_stop_time": "24:00",

    "translate": "baidu",
    "baidu_translate_app_id": "",
    "baidu_translate_app_key": "",

    "hot_reload": False,
    "wechaty_puppet_service_token": "",

    "wechatmp_token": "",
    "wechatmp_port": 8080,
    "wechatmp_app_id": "",
    "wechatmp_app_secret": "",
    "wechatmp_aes_key": "",

    "wechatcom_corp_id": "",
    "wechatcomapp_token": "",
    "wechatcomapp_port": 9898,
    "wechatcomapp_secret": "",
    "wechatcomapp_agent_id": "",
    "wechatcomapp_aes_key": "",

    "feishu_port": 80,
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_token": "",
    "feishu_bot_name": "",
    "feishu_event_mode": "websocket",

    "dingtalk_client_id": "",
    "dingtalk_client_secret": "",
    "dingtalk_card_enabled": False,

    "clear_memory_commands": ["#清除记忆"],
    "reset_commands": ["重置", "#重置"],
    "reset_opening_message": "重置好啦✨我是满仓，要先吃点小饼干吗？",

    "channel_type": "",
    "subscribe_msg": "",
    "debug": False,
    "appdata_dir": "",

    "plugin_trigger_prefix": "$",
    "use_global_plugin_config": False,
    "max_media_send_count": 3,
    "media_send_interval": 1,

    "zhipu_ai_api_key": "",
    "zhipu_ai_api_base": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot_api_key": "",
    "moonshot_base_url": "https://api.moonshot.cn/v1",

    "ark_api_key": "",
    "ark_base_url": "https://ark.cn-beijing.volces.com/api/v3",

    "modelscope_api_key": "",
    "modelscope_base_url": "https://api-inference.modelscope.cn/v1/chat/completions",

    "use_linkai": False,
    "linkai_api_key": "",
    "linkai_app_code": "",
    "linkai_api_base": "https://api.link-ai.tech",
    "cloud_host": "client.link-ai.tech",

    "minimax_api_key": "",
    "Minimax_group_id": "",
    "Minimax_base_url": "",

    "web_port": 9899,
    "agent": True,
    "agent_workspace": "~/cow",
    "agent_max_context_tokens": 50000,
    "agent_max_context_turns": 30,
    "agent_max_steps": 15,
}
class Config(dict):
    def __init__(self, d=None):
        super().__init__()
        if d is None:
            d = {}
        for k, v in d.items():
            self[k] = v
        # user_datas: 鐢ㄦ埛鏁版嵁锛宬ey涓虹敤鎴峰悕锛寁alue涓虹敤鎴锋暟鎹紝涔熸槸dict
        self.user_datas = {}

    def __getitem__(self, key):
        # 璺宠繃浠ヤ笅鍒掔嚎寮€澶寸殑娉ㄩ噴瀛楁
        if not key.startswith("_") and key not in available_setting:
            logger.warning("[Config] key '{}' not in available_setting, may not take effect".format(key))
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        # 璺宠繃浠ヤ笅鍒掔嚎寮€澶寸殑娉ㄩ噴瀛楁
        if not key.startswith("_") and key not in available_setting:
            logger.warning("[Config] key '{}' not in available_setting, may not take effect".format(key))
        return super().__setitem__(key, value)

    def get(self, key, default=None):
        # 璺宠繃浠ヤ笅鍒掔嚎寮€澶寸殑娉ㄩ噴瀛楁
        if key.startswith("_"):
            return super().get(key, default)
        
        # 濡傛灉key涓嶅湪available_setting涓紝鐩存帴杩斿洖default
        if key not in available_setting:
            return super().get(key, default)
        
        try:
            return self[key]
        except KeyError as e:
            return default
        except Exception as e:
            raise e

    # Make sure to return a dictionary to ensure atomic
    def get_user_data(self, user) -> dict:
        if self.user_datas.get(user) is None:
            self.user_datas[user] = {}
        return self.user_datas[user]

    def load_user_datas(self):
        try:
            with open(os.path.join(get_appdata_dir(), "user_datas.pkl"), "rb") as f:
                self.user_datas = pickle.load(f)
                logger.debug("[Config] User datas loaded.")
        except FileNotFoundError as e:
            logger.debug("[Config] User datas file not found, ignore.")
        except Exception as e:
            logger.warning("[Config] User datas error: {}".format(e))
            self.user_datas = {}

    def save_user_datas(self):
        try:
            with open(os.path.join(get_appdata_dir(), "user_datas.pkl"), "wb") as f:
                pickle.dump(self.user_datas, f)
                logger.info("[Config] User datas saved.")
        except Exception as e:
            logger.info("[Config] User datas error: {}".format(e))


config = Config()


def drag_sensitive(config):
    try:
        if isinstance(config, str):
            conf_dict: dict = json.loads(config)
            conf_dict_copy = copy.deepcopy(conf_dict)
            for key in conf_dict_copy:
                if "key" in key or "secret" in key:
                    if isinstance(conf_dict_copy[key], str):
                        conf_dict_copy[key] = conf_dict_copy[key][0:3] + "*" * 5 + conf_dict_copy[key][-3:]
            return json.dumps(conf_dict_copy, indent=4)

        elif isinstance(config, dict):
            config_copy = copy.deepcopy(config)
            for key in config:
                if "key" in key or "secret" in key:
                    if isinstance(config_copy[key], str):
                        config_copy[key] = config_copy[key][0:3] + "*" * 5 + config_copy[key][-3:]
            return config_copy
    except Exception as e:
        logger.exception(e)
        return config
    return config


def load_config():
    global config

    # 鎵撳嵃 ASCII Logo
    logger.info("  ____                _                    _   ")
    logger.info(" / ___|_____      __ / \\   __ _  ___ _ __ | |_ ")
    logger.info("| |   / _ \\ \\ /\\ / // _ \\ / _` |/ _ \\ '_ \\| __|")
    logger.info("| |__| (_) \\ V  V // ___ \\ (_| |  __/ | | | |_ ")
    logger.info(" \\____\\___/ \\_/\\_//_/   \\_\\__, |\\___|_| |_|\\__|")
    logger.info("                          |___/                 ")
    logger.info("")
    config_path = os.environ.get("CONFIG_PATH", "").strip()
    if not config_path:
        for idx, arg in enumerate(sys.argv):
            if arg == "--config" and idx + 1 < len(sys.argv):
                config_path = sys.argv[idx + 1].strip()
                break
            if arg.startswith("--config="):
                config_path = arg.split("=", 1)[1].strip()
                break

    if config_path:
        config_path = os.path.abspath(os.path.expanduser(config_path))
        logger.info(f"[INIT] use config from CONFIG_PATH/--config: {config_path}")
    else:
        config_path = "./config.json"
        if not os.path.exists(config_path):
            logger.info("閰嶇疆鏂囦欢涓嶅瓨鍦紝灏嗕娇鐢╟onfig-template.json妯℃澘")
            config_path = "./config-template.json"

    config_str = read_file(config_path)
    logger.debug("[INIT] config str: {}".format(drag_sensitive(config_str)))

    # 灏唈son瀛楃涓插弽搴忓垪鍖栦负dict绫诲瀷
    config = Config(json.loads(config_str))

    # override config with environment variables.
    # Some online deployment platforms (e.g. Railway) deploy project from github directly. So you shouldn't put your secrets like api key in a config file, instead use environment variables to override the default config.
    for name, value in os.environ.items():
        name = name.lower()
        # 璺宠繃浠ヤ笅鍒掔嚎寮€澶寸殑娉ㄩ噴瀛楁
        if name.startswith("_"):
            continue
        if name in available_setting:
            logger.info("[INIT] override config by environ args: {}={}".format(name, value))
            try:
                config[name] = eval(value)
            except:
                if value == "false":
                    config[name] = False
                elif value == "true":
                    config[name] = True
                else:
                    config[name] = value

    if config.get("debug", False):
        logger.setLevel(logging.DEBUG)
        logger.debug("[INIT] set log level to DEBUG")

    logger.info("[INIT] load config: {}".format(drag_sensitive(config)))

    # 鎵撳嵃绯荤粺鍒濆鍖栦俊鎭?
    logger.info("[INIT] ========================================")
    logger.info("[INIT] System Initialization")
    logger.info("[INIT] ========================================")
    logger.info("[INIT] Channel: {}".format(config.get("channel_type", "unknown")))
    logger.info("[INIT] Model: {}".format(config.get("model", "unknown")))

    # Agent妯″紡淇℃伅
    if config.get("agent", False):
        workspace = config.get("agent_workspace", "~/cow")
        logger.info("[INIT] Mode: Agent (workspace: {})".format(workspace))
    else:
        logger.info("[INIT] Mode: Chat (鍦╟onfig.json涓缃?\"agent\":true 鍙惎鐢ˋgent妯″紡)")

    logger.info("[INIT] Debug: {}".format(config.get("debug", False)))
    logger.info("[INIT] ========================================")

    config.load_user_datas()


def get_root():
    return os.path.dirname(os.path.abspath(__file__))


def read_file(path):
    with open(path, mode="r", encoding="utf-8") as f:
        return f.read()


def conf():
    return config


def get_appdata_dir():
    data_path = os.path.join(get_root(), conf().get("appdata_dir", ""))
    if not os.path.exists(data_path):
        logger.info("[INIT] data path not exists, create it: {}".format(data_path))
        os.makedirs(data_path)
    return data_path


def subscribe_msg():
    trigger_prefix = conf().get("single_chat_prefix", [""])[0]
    msg = conf().get("subscribe_msg", "")
    return msg.format(trigger_prefix=trigger_prefix)


# global plugin config
plugin_config = {}


def write_plugin_config(pconf: dict):
    """
    鍐欏叆鎻掍欢鍏ㄥ眬閰嶇疆
    :param pconf: 鍏ㄩ噺鎻掍欢閰嶇疆
    """
    global plugin_config
    for k in pconf:
        plugin_config[k.lower()] = pconf[k]

def remove_plugin_config(name: str):
    """
    绉婚櫎寰呴噸鏂板姞杞界殑鎻掍欢鍏ㄥ眬閰嶇疆
    :param name: 寰呴噸杞界殑鎻掍欢鍚?
    """
    global plugin_config
    plugin_config.pop(name.lower(), None)


def pconf(plugin_name: str) -> dict:
    """
    鏍规嵁鎻掍欢鍚嶇О鑾峰彇閰嶇疆
    :param plugin_name: 鎻掍欢鍚嶇О
    :return: 璇ユ彃浠剁殑閰嶇疆椤?
    """
    return plugin_config.get(plugin_name.lower())


# 鍏ㄥ眬閰嶇疆锛岀敤浜庡瓨鏀惧叏灞€鐢熸晥鐨勭姸鎬?
global_config = {"admin_users": []}

