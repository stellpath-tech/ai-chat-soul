import base64
import hashlib
import hmac
import os
from email.utils import formatdate
from urllib.parse import quote

import requests

from config import conf


def decode_image_base64(value):
    value = str(value or "")
    if "," in value and value.lstrip().startswith("data:"):
        value = value.split(",", 1)[1]
    return base64.b64decode(value)


def store_diary_image(user_id, diary_date, image_id, image_bytes, content_type="image/png"):
    backend = str(conf().get("diary_image_storage", "local") or "local").lower()
    extension = ".jpg" if content_type == "image/jpeg" else ".png"
    private_prefix = hashlib.sha256(
        "{}:{}".format(user_id, diary_date).encode("utf-8")
    ).hexdigest()[:20]
    object_key = "diary/{}/{}{}".format(private_prefix, image_id, extension)
    if backend == "oss":
        return _put_oss(object_key, image_bytes, content_type)
    return _put_local(object_key, image_bytes)


def _put_local(object_key, image_bytes):
    root = os.path.join(os.path.expanduser("~/cow/data"), "diary_images")
    relative_path = object_key.replace("/", os.sep)
    full_path = os.path.abspath(os.path.join(root, relative_path))
    if os.path.commonpath([full_path, os.path.abspath(root)]) != os.path.abspath(root):
        raise ValueError("invalid diary image path")
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as file:
        file.write(image_bytes)
    base_url = str(conf().get("diary_public_base_url", "") or "").rstrip("/")
    public_path = "/diary-images/{}".format(object_key)
    return "{}{}".format(base_url, public_path) if base_url else public_path


def _put_oss(object_key, image_bytes, content_type):
    access_key_id = str(conf().get("diary_oss_access_key_id", "") or "")
    access_key_secret = str(conf().get("diary_oss_access_key_secret", "") or "")
    bucket = str(conf().get("diary_oss_bucket", "") or "")
    endpoint = str(conf().get("diary_oss_endpoint", "") or "").strip().rstrip("/")
    if not all((access_key_id, access_key_secret, bucket, endpoint)):
        raise ValueError("diary OSS configuration is incomplete")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint

    endpoint_host = endpoint.split("://", 1)[1]
    encoded_key = quote(object_key, safe="/-_.~")
    upload_url = "{}://{}.{}{}{}".format(
        endpoint.split("://", 1)[0], bucket, endpoint_host,
        "/" if not encoded_key.startswith("/") else "", encoded_key,
    )
    date_header = formatdate(usegmt=True)
    canonical_resource = "/{}/{}".format(bucket, object_key)
    string_to_sign = "PUT\n\n{}\n{}\n{}".format(content_type, date_header, canonical_resource)
    signature = base64.b64encode(hmac.new(
        access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()).decode("ascii")
    response = requests.put(
        upload_url,
        data=image_bytes,
        headers={
            "Authorization": "OSS {}:{}".format(access_key_id, signature),
            "Content-Type": content_type,
            "Date": date_header,
        },
        timeout=(10, 120),
    )
    response.raise_for_status()

    public_base = str(conf().get("diary_oss_public_base_url", "") or "").rstrip("/")
    if public_base:
        return "{}/{}".format(public_base, encoded_key)
    return upload_url
