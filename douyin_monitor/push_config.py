"""Validate notification destinations and the global delivery selection."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


CHANNEL_TYPES = {
    "serverchan": "Server酱³",
    "pushplus": "PushPlus",
    "bark": "Bark",
    "feishu": "飞书",
    "dingtalk": "钉钉",
    "ntfy": "ntfy",
    "webhook": "通用 Webhook",
}


def _field(key, label, default="", *, secret=False, required=False, kind="text"):
    return {
        "key": key, "label": label, "default": default,
        "secret": secret, "required": required, "kind": kind,
    }


CHANNEL_FIELDS = {
    "serverchan": (
        _field("push_url", "完整推送 URL", secret=True),
        _field("sendkey", "SendKey（兼容旧配置）", secret=True),
        _field("uid", "UID（兼容旧配置）"),
    ),
    "pushplus": (
        _field("token", "Token", secret=True, required=True),
        _field("topic", "群组编码"),
    ),
    "bark": (
        _field("server_url", "服务地址", "https://api.day.app", required=True),
        _field("device_key", "设备 Key", secret=True, required=True),
        _field("group", "消息分组"),
    ),
    "feishu": (
        _field("webhook_url", "机器人 Webhook URL", secret=True, required=True),
        _field("secret", "签名密钥", secret=True),
        _field("keyword", "安全关键词"),
    ),
    "dingtalk": (
        _field("webhook_url", "机器人 Webhook URL", secret=True, required=True),
        _field("secret", "签名密钥", secret=True),
        _field("keyword", "安全关键词"),
    ),
    "ntfy": (
        _field("server_url", "服务地址", "https://ntfy.sh", required=True),
        _field("topic", "主题", required=True),
        _field("token", "访问令牌", secret=True),
    ),
    "webhook": (
        _field("url", "Webhook URL", secret=True, required=True),
        _field("headers", "请求头（JSON 对象）", {}, secret=True, kind="json"),
    ),
}

CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SERVERCHAN_URL_PATTERN = re.compile(
    r"https://([\w-]+)\.push\.ft07\.com/send/([\w-]+)\.send/?"
)
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
PROTECTED_HEADERS = {
    "host", "content-type", "content-length", "transfer-encoding", "connection",
    "keep-alive", "te", "trailer", "upgrade",
}


def _text(value: Any, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label}不能包含换行或控制字符")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{label}不能为空")
    return result


def _url(value: str, label: str, *, allow_http=False, base=False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme in ({"https", "http"} if allow_http else {"https"})
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and "\\" not in value
            and not any(char in value for char in '\"<>')
            and not any(char.isspace() for char in value)
            and (port is None or 1 <= port <= 65535)
            and (not base or not parsed.query)
        )
    except ValueError:
        valid = False
    if not valid:
        scheme = "HTTP 或 HTTPS" if allow_http else "HTTPS"
        raise ValueError(f"{label}必须是有效的 {scheme} 地址，且不能含账号凭据或片段")
    if parsed.scheme == "http" and parsed.hostname in {"api.day.app", "ntfy.sh"}:
        raise ValueError(f"{label}的公共服务必须使用 HTTPS")
    return value.rstrip("/") if base else value


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Webhook 请求头必须是对象")
    headers = {}
    names = set()
    for name, content in value.items():
        if not isinstance(name, str) or not HEADER_NAME_PATTERN.fullmatch(name):
            raise ValueError("Webhook 请求头名称格式无效")
        lower = name.lower()
        if lower in PROTECTED_HEADERS or lower.startswith("proxy-"):
            raise ValueError("Webhook 请求头不能覆盖受保护的请求字段")
        if lower in names:
            raise ValueError("Webhook 请求头名称重复")
        content = _text(content, "Webhook 请求头内容")
        try:
            content.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError("Webhook 请求头内容必须能使用 Latin-1 编码") from None
        names.add(lower)
        headers[name] = content
    return headers


def normalize_channel(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return one complete destination without echoing credentials in errors."""
    if not isinstance(mapping, Mapping):
        raise ValueError("推送渠道必须是对象")
    channel_type = mapping.get("type")
    if not isinstance(channel_type, str) or channel_type not in CHANNEL_TYPES:
        raise ValueError("推送渠道类型无效")
    channel_id = _text(mapping.get("id", ""), "推送渠道 ID", required=True)
    if not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise ValueError("推送渠道 ID 格式无效")
    name = _text(mapping.get("name", ""), "推送渠道名称") or CHANNEL_TYPES[channel_type]
    if len(name) > 80:
        raise ValueError("推送渠道名称不能超过 80 个字符")
    raw = mapping.get("config", {})
    if not isinstance(raw, Mapping):
        raise ValueError("推送渠道 config 必须是对象")
    fields = CHANNEL_FIELDS[channel_type]
    if set(raw) - {field["key"] for field in fields}:
        raise ValueError("推送渠道 config 包含不支持的字段")
    config = {}
    for field in fields:
        value = raw.get(field["key"], field["default"])
        config[field["key"]] = (
            _headers(value) if field["kind"] == "json"
            else _text(value, field["label"], required=field["required"])
        )
    if channel_type == "serverchan":
        if config["push_url"]:
            match = SERVERCHAN_URL_PATTERN.fullmatch(config["push_url"])
            if not match:
                raise ValueError("无法解析推送 URL 格式")
            config.update(push_url=config["push_url"].rstrip("/"), sendkey="", uid="")
        elif not config["sendkey"]:
            raise ValueError("Server酱³ 必须填写完整推送 URL 或 SendKey")
        elif not re.fullmatch(r"[\w-]+", config["sendkey"]) or (
            config["uid"] and not re.fullmatch(r"[\w-]+", config["uid"])
        ):
            raise ValueError("Server酱³ SendKey 或 UID 格式无效")
    elif channel_type in {"bark", "ntfy"}:
        config["server_url"] = _url(config["server_url"], "服务地址", allow_http=True, base=True)
        if channel_type == "ntfy" and not re.fullmatch(r"[A-Za-z0-9_-]+", config["topic"]):
            raise ValueError("ntfy 主题只能包含字母、数字、下划线和短横线")
    elif channel_type in {"feishu", "dingtalk"}:
        config["webhook_url"] = _url(config["webhook_url"], "机器人 Webhook URL")
    elif channel_type == "webhook":
        config["url"] = _url(config["url"], "Webhook URL", allow_http=True)
    return {"id": channel_id, "type": channel_type, "name": name, "config": config}


def _url_identity(value: str) -> tuple:
    parsed = urlsplit(value)
    return (
        parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
        parsed.path, tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
    )


def _destination(channel: Mapping[str, Any]) -> tuple:
    channel_type = channel["type"]
    config = channel["config"]
    if channel_type == "serverchan":
        if config["push_url"]:
            match = SERVERCHAN_URL_PATTERN.fullmatch(config["push_url"])
            return channel_type, match.group(1).lower(), match.group(2)
        return channel_type, config["uid"].lower(), config["sendkey"]
    if channel_type == "pushplus":
        return channel_type, config["token"], config["topic"]
    if channel_type in {"bark", "ntfy"}:
        return channel_type, _url_identity(config["server_url"]), config[
            "device_key" if channel_type == "bark" else "topic"
        ]
    return channel_type, _url_identity(config["url" if channel_type == "webhook" else "webhook_url"])


def normalize_push_config(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only push settings; an explicit empty list stays empty."""
    if not isinstance(mapping, Mapping):
        raise ValueError("配置根节点必须是对象")
    raw_channels = mapping.get("channels", [])
    if "channels" not in mapping:
        push_url = str(mapping.get("push_url") or "")
        sendkey = str(mapping.get("sendkey") or "")
        uid = str(mapping.get("push_uid") or "")
        if push_url == "YOUR_PUSH_URL_HERE":
            push_url = ""
        if sendkey == "YOUR_SENDKEY_HERE":
            sendkey = ""
        if push_url or sendkey:
            raw_channels = [{
                "id": "legacy_serverchan", "type": "serverchan", "name": "Server酱³",
                "config": {"push_url": push_url, "sendkey": sendkey, "uid": uid},
            }]
    if not isinstance(raw_channels, list):
        raise ValueError("配置项 channels 必须是列表")
    channels = [normalize_channel(channel) for channel in raw_channels]
    ids = set()
    destinations = set()
    for channel in channels:
        if channel["id"] in ids:
            raise ValueError("推送渠道 ID 重复")
        destination = _destination(channel)
        if destination in destinations:
            raise ValueError("推送渠道目的地重复")
        ids.add(channel["id"])
        destinations.add(destination)
    mode = mapping.get("push_mode", "all")
    if not isinstance(mode, str) or mode not in {"all", "selected"}:
        raise ValueError("push_mode 必须是 all 或 selected")
    selected = mapping.get("selected_channel_ids", [])
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        raise ValueError("selected_channel_ids 必须是渠道 ID 列表")
    if len(selected) != len(set(selected)):
        raise ValueError("所选推送渠道 ID 重复")
    if any(item not in ids for item in selected):
        raise ValueError("所选推送渠道 ID 不存在")
    return {"channels": channels, "push_mode": mode, "selected_channel_ids": list(selected)}


def selected_channels(mapping: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return destinations in their configured order without changing selection."""
    config = normalize_push_config(mapping)
    if config["push_mode"] == "all":
        return config["channels"]
    selected = set(config["selected_channel_ids"])
    return [channel for channel in config["channels"] if channel["id"] in selected]


def has_push_targets(mapping: Mapping[str, Any]) -> bool:
    return bool(selected_channels(mapping))
