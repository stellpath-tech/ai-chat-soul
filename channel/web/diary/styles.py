import json
import os


DEFAULT_DIARY_IMAGE_STYLE = "warm_healing"

DIARY_IMAGE_STYLE_NAMES = {
    "warm_healing": "温暖治愈",
    "wool_felt": "绒毡童话",
    "pixel_art": "像素漫游",
    "clay_art": "软陶物语",
    "lego_style": "积木奇境",
    "chinese_ink": "水墨写意",
}

DIARY_IMAGE_STYLES = frozenset(DIARY_IMAGE_STYLE_NAMES)
_STYLE_DIR = os.path.join(os.path.dirname(__file__), "prompt_styles")
_STYLE_PROMPTS = {}


def is_valid_diary_image_style(style):
    return isinstance(style, str) and style in DIARY_IMAGE_STYLES


def normalize_diary_image_style(style):
    return style if is_valid_diary_image_style(style) else DEFAULT_DIARY_IMAGE_STYLE


def get_diary_image_prompt(style):
    style = normalize_diary_image_style(style)
    if style not in _STYLE_PROMPTS:
        path = os.path.join(_STYLE_DIR, style + ".json")
        with open(path, "r", encoding="utf-8") as file:
            bundle = json.load(file)
        if bundle.get("style") != style:
            raise RuntimeError("invalid diary image style bundle: {}".format(style))
        positive = str(bundle.get("imagePositive") or "").strip()
        negative = str(bundle.get("imageNegative") or "").strip()
        if not positive or not negative:
            raise RuntimeError("incomplete diary image style bundle: {}".format(style))
        _STYLE_PROMPTS[style] = {
            "style": style,
            "display_name": DIARY_IMAGE_STYLE_NAMES[style],
            "source": str(bundle.get("source") or "").strip(),
            "positive_prompt": positive,
            "negative_prompt": negative,
        }
    return dict(_STYLE_PROMPTS[style])
