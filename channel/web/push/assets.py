import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import quote

import requests

from config import conf
from channel.web.push.contracts import PushContentImageRequestError
from channel.web.push import repository


MAX_PUSH_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class _PushAssetOssConfig:
    access_key_id: str
    access_key_secret: str
    bucket: str
    endpoint: str
    read_url_ttl_seconds: int

    @classmethod
    def from_runtime(cls):
        access_key_id = str(
            conf().get("push_asset_oss_access_key_id", "") or ""
        ).strip()
        access_key_secret = str(
            conf().get("push_asset_oss_access_key_secret", "") or ""
        ).strip()
        bucket = str(
            conf().get("push_asset_oss_bucket", "ommo-app-assets-dev") or ""
        ).strip()
        endpoint = str(
            conf().get(
                "push_asset_oss_endpoint",
                "oss-cn-wulanchabu.aliyuncs.com",
            ) or ""
        ).strip()
        if not all((access_key_id, access_key_secret, bucket, endpoint)):
            raise PushContentImageRequestError(
                "push asset OSS configuration is incomplete"
            )
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        return cls(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            bucket=bucket,
            endpoint=endpoint.rstrip("/"),
            read_url_ttl_seconds=max(
                60,
                int(conf().get("push_asset_oss_read_url_ttl_seconds", 86400)),
            ),
        )


def upload_image_for_content(content_id, filename, image_bytes, http_put=None):
    content = repository.get_content(content_id)
    if not content:
        raise PushContentImageRequestError("push content not found")
    image_type, extension = _detect_image_type(filename, image_bytes)
    if not image_bytes:
        raise PushContentImageRequestError("image file is empty")
    if len(image_bytes) > MAX_PUSH_IMAGE_BYTES:
        raise PushContentImageRequestError("image file exceeds 10MB")

    version = "{}-{}".format(int(time.time() * 1000), uuid.uuid4().hex[:8])
    object_key = "push-cards/{}/{}/{}.{}".format(
        content["push_type"],
        str(content["content_no"]).lower(),
        version,
        extension,
    )
    config = _PushAssetOssConfig.from_runtime()
    _put_private_oss_object(
        config,
        object_key,
        image_bytes,
        image_type,
        http_put=http_put,
    )
    result = repository.add_content_image(
        content_id,
        object_key,
        hashlib.sha256(image_bytes).hexdigest(),
        len(image_bytes),
    )
    if not result:
        raise PushContentImageRequestError("push content not found")
    result.pop("objectKey", None)
    result["imageUrl"] = create_image_read_url(object_key, config=config)
    return result


def add_signed_urls_to_content_list(content_list, config=None):
    result = dict(content_list or {})
    items = []
    resolved_config = config
    for content in result.get("items") or []:
        item = dict(content)
        images = []
        for image in item.get("images") or []:
            image_item = dict(image)
            object_key = image_item.pop("objectKey", "")
            if resolved_config is None:
                resolved_config = _PushAssetOssConfig.from_runtime()
            image_item["imageUrl"] = create_image_read_url(
                object_key,
                config=resolved_config,
            )
            images.append(image_item)
        item["images"] = images
        items.append(item)
    result["items"] = items
    return result


def _detect_image_type(filename, image_bytes):
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise PushContentImageRequestError(
        "file must be a valid PNG, JPG, JPEG or WEBP image"
    )


def _put_private_oss_object(config, object_key, image_bytes, content_type, http_put=None):
    endpoint_scheme, endpoint_host = config.endpoint.split("://", 1)
    encoded_key = quote(object_key, safe="/-_.~")
    upload_url = "{}://{}.{}/{}".format(
        endpoint_scheme,
        config.bucket,
        endpoint_host,
        encoded_key,
    )
    date_header = formatdate(usegmt=True)
    content_md5 = base64.b64encode(hashlib.md5(image_bytes).digest()).decode("ascii")
    oss_headers = "x-oss-forbid-overwrite:true\n"
    canonical_resource = "/{}/{}".format(config.bucket, object_key)
    string_to_sign = "PUT\n{}\n{}\n{}\n{}{}".format(
        content_md5,
        content_type,
        date_header,
        oss_headers,
        canonical_resource,
    )
    signature = base64.b64encode(hmac.new(
        config.access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()).decode("ascii")
    put = http_put or requests.put
    response = put(
        upload_url,
        data=image_bytes,
        headers={
            "Authorization": "OSS {}:{}".format(config.access_key_id, signature),
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-MD5": content_md5,
            "Content-Type": content_type,
            "Date": date_header,
            "x-oss-forbid-overwrite": "true",
        },
        timeout=(10, 120),
    )
    response.raise_for_status()


def create_image_read_url(object_key, config=None, now=None):
    object_key = str(object_key or "").strip().lstrip("/")
    if not object_key:
        return ""
    config = config or _PushAssetOssConfig.from_runtime()
    expires = int(now if now is not None else time.time()) + int(
        config.read_url_ttl_seconds
    )
    canonical_resource = "/{}/{}".format(config.bucket, object_key)
    string_to_sign = "GET\n\n\n{}\n{}".format(expires, canonical_resource)
    signature = base64.b64encode(hmac.new(
        config.access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()).decode("ascii")
    endpoint_scheme, endpoint_host = config.endpoint.split("://", 1)
    encoded_key = quote(object_key, safe="/-_.~")
    base_url = "{}://{}.{}/{}".format(
        endpoint_scheme,
        config.bucket,
        endpoint_host,
        encoded_key,
    )
    return "{}?{}".format(base_url, "&".join((
        "OSSAccessKeyId=" + quote(config.access_key_id, safe=""),
        "Expires=" + str(expires),
        "Signature=" + quote(signature, safe=""),
    )))
