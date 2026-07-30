import json
import os


_V29_PATH = os.path.join(os.path.dirname(__file__), "v29.json")
with open(_V29_PATH, "r", encoding="utf-8") as _file:
    _V29 = json.load(_file)

if int(_V29.get("version", 0)) != 29 or _V29.get("templateCategory") != "diary":
    raise RuntimeError("invalid diary v29 prompt bundle")


PRODUCT_DIARY_SYSTEM_PROMPT = str(_V29.get("systemPrompt") or "").strip()
KEY_MOMENT_SYSTEM_PROMPT = str(_V29.get("keyMomentPrompt") or "").strip()
IMAGE_POSITIVE_PROMPT = str(_V29.get("imagePositive") or "").strip()
IMAGE_NEGATIVE_PROMPT = str(_V29.get("imageNegative") or "").strip()
REFERENCE_IMAGE = str(_V29.get("referenceImage") or "").strip()

if not all((PRODUCT_DIARY_SYSTEM_PROMPT, KEY_MOMENT_SYSTEM_PROMPT,
            IMAGE_POSITIVE_PROMPT, IMAGE_NEGATIVE_PROMPT, REFERENCE_IMAGE)):
    raise RuntimeError("diary v29 prompt bundle is incomplete")
