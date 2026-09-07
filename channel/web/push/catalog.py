"""The delivery scenes shared by the scheduler, content editor and validation."""

GREETING_PERIODS = (
    ("morning", "早间问候", (
        ("0700", "刚刚醒来的早上"), ("0730", "早餐与出门准备"),
        ("0800", "通勤与一天开启"), ("0830", "工作与学习"), ("0900", "元气加油"),
    )),
    ("noon", "午间问候", (
        ("1100", "开始想午饭"), ("1130", "准备收尾去吃饭"),
        ("1200", "午饭时间"), ("1230", "饭后缓一缓"), ("1300", "午休与重新出发"),
    )),
    ("evening", "晚间问候", (
        ("1800", "下班与一天收尾"), ("1830", "晚霞时刻"), ("1900", "晚饭时间"),
        ("1930", "饭后放松"), ("2000", "属于自己的夜晚"), ("2030", "放慢节奏"),
        ("2100", "夜晚安顿"),
    )),
)

GREETING_WINDOWS = {key: [window for window, _ in scenes] for key, _, scenes in GREETING_PERIODS}

WEATHER_SCENES = (
    ("SHOWER", "骤雨 / 短时强降水", ("骤雨", "短时强降水")),
    ("THUNDER", "雷雨 / 雷电", ("雷电", "雷雨", "雷暴")),
    ("HEAVY_RAIN", "暴雨 / 持续强降水", ("暴雨", "强降水")),
    ("HAIL", "冰雹 / 强对流", ("冰雹", "强对流")),
    ("TYPHOON", "台风", ("台风", "热带气旋")),
    ("COLD", "寒潮 / 大幅降温", ("寒潮", "强降温", "低温")),
    ("HEAT", "高温 / 热浪", ("高温", "热浪")),
    ("GALE", "大风 / 阵风", ("大风", "阵风")),
    ("SNOW", "暴雪 / 大雪", ("暴雪", "大雪")),
    ("ICE", "冻雨 / 道路结冰", ("冻雨", "道路结冰", "结冰")),
    ("FOG", "大雾 / 低能见度", ("大雾", "浓雾", "低能见度")),
    ("DUST", "沙尘暴 / 扬沙", ("沙尘暴", "扬沙", "沙尘")),
)
# Preserve the established matching priority for compound weather descriptions.
WEATHER_SCENE_KEYWORDS = [
    ("WEATHER_" + key, next(words for name, _, words in WEATHER_SCENES if name == key))
    for key in ("TYPHOON", "HAIL", "THUNDER", "SHOWER", "HEAVY_RAIN", "COLD",
                "HEAT", "GALE", "SNOW", "ICE", "FOG", "DUST")
]


def content_categories():
    greeting_groups = []
    for key, label, windows in GREETING_PERIODS:
        scenes = []
        for window, description in windows:
            hour, minute = int(window[:2]), int(window[2:])
            scenes.append({
                "value": "GREETING_" + window,
                "label": "{:02d}:{:02d}–{:02d}:{:02d}".format(hour, minute, hour, minute + 29),
                "description": description,
                "contentPrefix": {"morning": "AM", "noon": "NOON", "evening": "PM"}[key] + "-" + window,
            })
        greeting_groups.append({"label": label, "scenes": scenes})
    return [
        {"type": "greeting", "label": "日常问候", "sceneLabel": "发送时间段",
         "description": "先选早、午、晚的时间段，再维护这个时间段的文案。",
         "groups": greeting_groups},
        {"type": "weather", "label": "天气预警", "sceneLabel": "预警类型",
         "description": "按预警类型准备提醒，正文先说清防范建议，再表达满仓的关心。",
         "groups": [{"label": "重大天气变化", "scenes": [
             {"value": "WEATHER_" + key, "label": label, "description": "官方预警触发 · 每人每天最多一条",
              "contentPrefix": "W-" + key.replace("_", "-")}
             for key, label, _ in WEATHER_SCENES
         ]}]},
        {"type": "diary", "label": "日记完成", "sceneLabel": "触发场景",
         "description": "日记生成完成时使用这一组文案，按钮进入当天日记。",
         "groups": [{"label": "完成提醒", "scenes": [
             {"value": "DIARY_READY", "label": "日记写完", "description": "23:30 前完成后发送",
              "contentPrefix": "DIARY"}
         ]}]},
        {"type": "recall", "label": "用户召回", "sceneLabel": "未活跃天数",
         "description": "按未活跃时长分别维护召回文案，每个节点最多发送一次。",
         "groups": [{"label": "召回节点", "scenes": [
             {"value": "RECALL_{:02d}".format(day), "label": "{} 天未活跃".format(day),
              "description": description + " · 本地时间 20:00", "contentPrefix": "RECALL-{:02d}".format(day)}
             for day, description in ((7, "轻轻问候"), (15, "邀请回来坐坐"), (30, "最后一次常规召回"))
         ]}]},
    ]


def find_content_scene(push_type, delivery_scene):
    for category in content_categories():
        if category["type"] == push_type:
            for group in category["groups"]:
                for scene in group["scenes"]:
                    if scene["value"] == delivery_scene:
                        return scene
    return None
