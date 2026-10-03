"""Anonymous, bounded live-status adapters for supported public platforms."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

from .douyin_client import DouyinClient
from .monitor_errors import ProfileLinkError, failure_reason


PLATFORM_NAMES = {
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "huya": "虎牙",
    "douyu": "斗鱼",
    "kuaishou": "快手",
}
MIN_CHECK_INTERVALS = {
    "douyin": 1, "bilibili": 30, "huya": 30, "douyu": 30, "kuaishou": 60,
}
_INPUT_HOSTS = {
    "douyin": {"douyin.com", "www.douyin.com", "v.douyin.com", "live.douyin.com",
               "iesdouyin.com", "www.iesdouyin.com", "webcast.amemv.com"},
    "bilibili": {"live.bilibili.com"},
    "huya": {"huya.com", "www.huya.com", "m.huya.com"},
    "douyu": {"douyu.com", "www.douyu.com", "m.douyu.com"},
    "kuaishou": {"live.kuaishou.com"},
}
_REQUEST_HOSTS = {
    name: hosts | extra for name, hosts, extra in (
        ("bilibili", _INPUT_HOSTS["bilibili"], {"api.live.bilibili.com"}),
        ("huya", _INPUT_HOSTS["huya"], {"mp.huya.com"}),
        ("douyu", _INPUT_HOSTS["douyu"], set()),
        ("kuaishou", _INPUT_HOSTS["kuaishou"], set()),
    )
}
CHECK_BUDGET_SECONDS = 15.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_IDENTIFIER = r"[A-Za-z0-9_-]+"
_CHALLENGE = re.compile(
    r"<title[^>]*>[^<]*(?:登录|验证码|安全验证|访问验证|access denied|captcha)|"
    r"(?:id|class)=[\"'][^\"']*(?:captcha|verify-page)|"
    r"请完成安全验证|访问过于频繁", re.I,
)


class PlatformAccessError(RuntimeError):
    """A retryable platform failure containing only a safe user-facing reason."""

    def __init__(self, message: str, error_kind: str = "parse", cooldown: float = 0):
        super().__init__(message)
        self.error_kind = error_kind
        self.cooldown = cooldown


def platform_name(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform)


def _parse_input(url: str):
    value = (url or "").strip()
    if not value or len(value) > 2048:
        raise ProfileLinkError("主播链接不能为空且长度不能超过 2048 个字符")
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ProfileLinkError("请填写完整的主播链接")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ProfileLinkError("主播链接格式无效") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ProfileLinkError("主播链接不能包含账号凭据")
    if parsed.scheme not in {"http", "https"}:
        raise ProfileLinkError("主播链接必须使用 HTTP 或 HTTPS")
    if port not in (None, 80 if parsed.scheme == "http" else 443):
        raise ProfileLinkError("主播链接只能使用标准端口")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return parsed._replace(scheme="https", netloc=hostname, fragment="")


def infer_platform(url: str) -> str:
    parsed = _parse_input(url)
    hostname = parsed.hostname
    if hostname == "channels.weixin.qq.com":
        raise ProfileLinkError("视频号当前不支持免登录监控")
    for platform, hosts in _INPUT_HOSTS.items():
        if hostname in hosts:
            return platform
    raise ProfileLinkError("暂不支持该平台，请使用抖音、哔哩哔哩、虎牙、斗鱼或快手直播链接")


def validate_platform_url(url: str, platform: Optional[str] = None) -> str:
    """Validate platform and target path without performing network requests."""
    parsed = _parse_input(url)
    detected = infer_platform(url)
    if platform is not None and platform != detected:
        raise ProfileLinkError("配置的平台与主播链接不一致")
    path = parsed.path
    if detected == "douyin":
        hostname = parsed.hostname or ""
        if hostname == "v.douyin.com":
            valid = re.fullmatch(rf"/{_IDENTIFIER}/?", path)
        elif hostname == "live.douyin.com":
            valid = re.fullmatch(r"/\d+/?", path)
        elif hostname == "webcast.amemv.com":
            valid = re.fullmatch(r"/douyin/webcast/reflow/\d+/?|/reflow/\d+/?", path)
        else:
            valid = re.fullmatch(rf"/(?:share/)?user/{_IDENTIFIER}/?", path)
            valid = valid or any(parse_qs(parsed.query).get(key)
                                 for key in ("sec_uid", "sec_user_id"))
    elif detected == "bilibili":
        valid = re.fullmatch(r"/\d+/?", path) or re.fullmatch(r"/(?:blanc|h5)/\d+/?", path)
    elif detected in {"huya", "douyu"}:
        valid = re.fullmatch(rf"/{_IDENTIFIER}/?", path)
        if path.strip("/").lower() in {"directory", "g", "category", "download", "login", "betard"}:
            valid = False
    else:
        valid = re.fullmatch(rf"/u/{_IDENTIFIER}/?", path)
    if not valid:
        raise ProfileLinkError(f"请填写{platform_name(detected)}主播主页或直播间链接")
    return parsed.geturl()


def _identifier(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _numeric_id(value: Any) -> str:
    text = str(value or "")
    return text if text.isdigit() and int(text) > 0 else ""


def _session(room_id: str, value: Any) -> str:
    """Only use a credible start timestamp as a session discriminator."""
    text = str(value or "")
    if text.isdigit() and int(text) >= 946684800:
        return f"{room_id}:{text}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", text):
        try:
            if datetime.fromisoformat(text).year >= 2000:
                return f"{room_id}:{text}"
        except ValueError:
            pass
    return ""


def _result(_platform: str, _status: str, _method: str, **fields: Any) -> dict[str, Any]:
    return {
        "platform": _platform, "status": _status, "is_live": _status == "live",
        "indeterminate": _status == "unknown", "nickname": "", "title": "",
        "room_id": "", "live_url": "", "session_id": "", "method": _method,
        "error": "", "error_kind": "", **fields,
    }


class DouyinAdapter:
    """Preserve Douyin's existing detection and session-refresh behavior."""

    platform = "douyin"

    def __init__(self, debug: bool = False):
        self.client = DouyinClient(debug=debug)
        self._web_room: tuple[str, str] = ("", "")

    def resolve(self, url: str) -> dict[str, Any]:
        clean = validate_platform_url(url, self.platform)
        final_url = clean if urlparse(clean).hostname == "live.douyin.com" else self.client.resolve_short_link(clean)
        if urlparse(final_url).hostname == "live.douyin.com":
            info = self.client.resolve_live_room_url(final_url)
            sec_uid = info["sec_uid"]
            self._web_room = (sec_uid, _numeric_id(info.get("web_rid")))
            return {"platform": self.platform, "target_id": sec_uid,
                    "nickname": info.get("nickname", ""), "url": info["url"]}
        sec_uid = self.client.extract_sec_uid(final_url)
        if not sec_uid:
            raise ProfileLinkError("无法从抖音链接解析主播账号，请使用主播主页或分享链接")
        info = self.client.get_user_info(sec_uid)
        if self._web_room[0] != sec_uid:
            self._web_room = (sec_uid, "")
        return {"platform": self.platform, "target_id": sec_uid,
                "nickname": info.get("nickname", ""), "url": final_url}

    @staticmethod
    def _profile_sec_uid(url: str) -> str:
        parsed = urlparse(url)
        if parsed.hostname not in _INPUT_HOSTS["douyin"]:
            return ""
        match = re.fullmatch(rf"/(?:share/)?user/({_IDENTIFIER})/?", parsed.path)
        if match:
            return match[1]
        query = parse_qs(parsed.query)
        for key in ("sec_uid", "sec_user_id"):
            values = query.get(key, [])
            if len(values) == 1 and re.fullmatch(_IDENTIFIER, values[0]):
                return values[0]
        return ""

    def check(self, target_url: str) -> dict[str, Any]:
        raw = self.client.check_live(target_url=target_url)
        status = "unknown" if raw.get("indeterminate") or raw.get("error") else (
            "live" if raw.get("is_live") else "offline"
        )
        room_id = str(raw.get("room_id") or "")
        sec_uid = self._profile_sec_uid(target_url)
        web_rid = _numeric_id(raw.get("web_rid")) if sec_uid else ""
        if not web_rid and sec_uid == self._web_room[0]:
            web_rid = self._web_room[1]
        self._web_room = (sec_uid, web_rid)
        profile_url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""
        return _result(self.platform, status, raw.get("method", "douyin"), **{
            **raw, "status": status, "platform": self.platform,
            "is_live": status == "live",
            "indeterminate": status == "unknown",
            "live_url": f"https://live.douyin.com/{web_rid}" if web_rid else profile_url,
            "session_id": room_id,
            "error_kind": raw.get("error_kind") or (
                "network" if raw.get("error") else "parse" if status == "unknown" else ""
            ),
        })

    def close(self) -> None:
        self.client.close()


class _PublicClient:
    platform = ""
    method = ""

    def __init__(self, debug: bool = False):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=1, max_retries=0))
        self._target: dict[str, Any] = {}
        self._monotonic = time.monotonic

    def _validate_request_url(self, url: str) -> str:
        parsed = _parse_input(url)
        if urlparse(url).scheme != "https" or parsed.hostname not in _REQUEST_HOSTS[self.platform]:
            raise ProfileLinkError("平台请求重定向到了不受信任的地址")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise requests.ConnectionError("DNS 域名解析失败")
        if any(not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in addresses):
            raise ProfileLinkError("平台链接不能指向非公网地址")
        return parsed.geturl()

    def _get(self, url: str, deadline: float) -> str:
        """Check every redirect and release each response, including failures."""
        for hop in range(6):
            url = self._validate_request_url(url)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise requests.Timeout("单轮检测超过时限")
            timeout = max(0.05, min(5.0, remaining / 2))
            with self.session.get(url, allow_redirects=False, stream=True, timeout=(timeout, timeout)) as response:
                code = response.status_code
                if code == 429:
                    retry = response.headers.get("Retry-After", "")
                    cooldown = min(3600, max(60, int(retry))) if retry.isdigit() else 60
                    raise PlatformAccessError("平台请求频率受限，稍后自动重试", "rate_limited", cooldown)
                if code in {401, 403, 412}:
                    raise PlatformAccessError("平台暂时限制匿名访问", "restricted")
                if code in {301, 302, 303, 307, 308}:
                    if hop == 5 or not response.headers.get("Location"):
                        raise PlatformAccessError("平台重定向无法完成", "parse")
                    url = urljoin(url, response.headers["Location"])
                    if re.search(r"/(?:login|passport|captcha|verify)(?:[/?]|$)", urlparse(url).path, re.I):
                        raise PlatformAccessError("平台要求登录或安全验证", "restricted")
                    continue
                if code == 404:
                    raise PlatformAccessError("暂时无法读取该直播间", "parse")
                if code >= 400:
                    raise PlatformAccessError("平台服务请求失败", "network")
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=16384):
                    if self._monotonic() >= deadline:
                        raise requests.Timeout("单轮检测超过时限")
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise PlatformAccessError("平台响应超过大小限制", "parse")
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8-sig", errors="replace")
                if not text.strip():
                    raise PlatformAccessError("平台返回空响应", "parse")
                if _CHALLENGE.search(text):
                    raise PlatformAccessError("平台要求登录或安全验证", "restricted")
                return text
        raise PlatformAccessError("平台重定向无法完成", "parse")

    def _json(self, url: str, deadline: float) -> dict[str, Any]:
        try:
            data = json.loads(self._get(url, deadline))
        except json.JSONDecodeError as exc:
            raise PlatformAccessError("无法解析平台状态响应", "parse") from exc
        if not isinstance(data, dict):
            raise PlatformAccessError("平台状态响应格式发生变化", "parse")
        return data

    def _remember(self, target_id: str, nickname: str, url: str) -> dict[str, Any]:
        self._target = {"platform": self.platform, "target_id": target_id,
                        "nickname": nickname, "url": url}
        return dict(self._target)

    def check(self, target_url: str) -> dict[str, Any]:
        try:
            clean = validate_platform_url(target_url, self.platform)
            return self._check(clean, self._monotonic() + CHECK_BUDGET_SECONDS)
        except PlatformAccessError as exc:
            fields = {"error": str(exc), "error_kind": exc.error_kind}
            if exc.cooldown:
                fields["cooldown"] = exc.cooldown
        except (requests.RequestException, socket.gaierror, OSError) as exc:
            fields = {"error": failure_reason(exc), "error_kind": "network"}
        except ProfileLinkError as exc:
            fields = {"error": str(exc), "error_kind": "invalid_target"}
        except (KeyError, TypeError, ValueError, AttributeError):
            fields = {"error": "平台状态字段缺失或格式发生变化", "error_kind": "parse"}
        return _result(self.platform, "unknown", self.method,
                       nickname=self._target.get("nickname", ""),
                       live_url=self._target.get("url", target_url), **fields)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None


class BilibiliAdapter(_PublicClient):
    platform = "bilibili"
    method = "bilibili_room_init"

    def _room(self, room_id: str, deadline: float) -> dict[str, Any]:
        payload = self._json(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}", deadline)
        if payload.get("code") in {-101, -352, -412, -403}:
            raise PlatformAccessError("平台暂时限制匿名访问", "restricted")
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict) or not _numeric_id(data.get("room_id")):
            raise PlatformAccessError("无法读取哔哩哔哩房间信息", "parse")
        return data

    def _metadata(self, room_id: str, deadline: float) -> dict[str, Any]:
        try:
            data = self._json("https://api.live.bilibili.com/xlive/web-room/v1/index/"
                              f"getH5InfoByRoom?room_id={room_id}", deadline).get("data", {})
            nickname = data.get("anchor_info", {}).get("base_info", {}).get("uname", "")
            title = data.get("room_info", {}).get("title", "")
            return {"nickname": str(nickname or ""), "title": str(title or "")}
        except PlatformAccessError as exc:
            metadata = {"nickname": self._target.get("nickname", ""), "title": ""}
            if exc.cooldown:
                metadata["cooldown"] = exc.cooldown
                metadata["error_kind"] = exc.error_kind
            return metadata
        except (requests.RequestException, OSError, TypeError, AttributeError):
            return {"nickname": self._target.get("nickname", ""), "title": ""}

    def resolve(self, url: str) -> dict[str, Any]:
        clean = validate_platform_url(url, self.platform)
        deadline = self._monotonic() + CHECK_BUDGET_SECONDS
        data = self._room(_identifier(clean), deadline)
        room_id = _numeric_id(data["room_id"])
        metadata = self._metadata(room_id, deadline)
        return self._remember(room_id, metadata["nickname"], f"https://live.bilibili.com/{room_id}")

    def _check(self, url: str, deadline: float) -> dict[str, Any]:
        data = self._room(_identifier(url), deadline)
        value = data.get("live_status")
        if isinstance(value, bool) or value not in {0, 1, 2}:
            raise PlatformAccessError("哔哩哔哩未返回明确直播状态", "parse")
        room_id = _numeric_id(data["room_id"])
        metadata = self._metadata(room_id, deadline)
        return _result(self.platform, "live" if value == 1 else "offline", self.method,
                       room_id=room_id, live_url=f"https://live.bilibili.com/{room_id}",
                       session_id=_session(room_id, data.get("live_time")) if value == 1 else "", **metadata)


class HuyaAdapter(_PublicClient):
    platform = "huya"
    method = "huya_profile_room"

    def _room_id(self, url: str, deadline: float) -> str:
        target = _identifier(url)
        if _numeric_id(target):
            return target
        page = self._get(f"https://m.huya.com/{target}", deadline)
        match = re.search(r'["\'](?:profileRoom|ProfileRoom)["\']\s*:\s*["\']?(\d+)', page)
        if not match:
            raise PlatformAccessError("暂时无法解析虎牙直播间别名", "parse")
        return match[1]

    def _room(self, room_id: str, deadline: float) -> dict[str, Any]:
        payload = self._json(f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}&showSecret=1", deadline)
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise PlatformAccessError("无法读取虎牙房间信息", "parse")
        return data

    def resolve(self, url: str) -> dict[str, Any]:
        clean = validate_platform_url(url, self.platform)
        deadline = self._monotonic() + CHECK_BUDGET_SECONDS
        room_id = self._room_id(clean, deadline)
        data = self._room(room_id, deadline)
        profile = data.get("profileInfo") or {}
        room_id = _numeric_id(profile.get("profileRoom")) or room_id
        return self._remember(room_id, str(profile.get("nick") or ""), f"https://www.huya.com/{room_id}")

    def _check(self, url: str, deadline: float) -> dict[str, Any]:
        room_id = self._room_id(url, deadline)
        data = self._room(room_id, deadline)
        value = data.get("realLiveStatus")
        if value not in {"ON", "OFF"}:
            raise PlatformAccessError("虎牙未返回明确直播状态", "parse")
        profile = data.get("profileInfo") or {}
        live = data.get("liveData") or {}
        room_id = _numeric_id(profile.get("profileRoom")) or room_id
        return _result(self.platform, "live" if value == "ON" else "offline", self.method,
                       nickname=str(profile.get("nick") or ""), title=str(live.get("introduction") or ""),
                       room_id=room_id, live_url=f"https://www.huya.com/{room_id}",
                       session_id=_session(room_id, live.get("startTime")) if value == "ON" else "")


class DouyuAdapter(_PublicClient):
    platform = "douyu"
    method = "douyu_betard"

    def _room_id(self, url: str, deadline: float) -> str:
        target = _identifier(url)
        if _numeric_id(target):
            return target
        page = self._get(f"https://m.douyu.com/{target}", deadline)
        match = re.search(r'["\']rid["\']\s*:\s*["\']?(\d+)', page)
        if not match:
            raise PlatformAccessError("暂时无法解析斗鱼直播间别名", "parse")
        return match[1]

    def _room(self, room_id: str, deadline: float) -> dict[str, Any]:
        data = self._json(f"https://www.douyu.com/betard/{room_id}", deadline).get("room")
        if not isinstance(data, dict) or not _numeric_id(data.get("room_id")):
            raise PlatformAccessError("无法读取斗鱼房间信息", "parse")
        return data

    def resolve(self, url: str) -> dict[str, Any]:
        clean = validate_platform_url(url, self.platform)
        deadline = self._monotonic() + CHECK_BUDGET_SECONDS
        data = self._room(self._room_id(clean, deadline), deadline)
        room_id = _numeric_id(data["room_id"])
        return self._remember(room_id, str(data.get("nickname") or ""), f"https://www.douyu.com/{room_id}")

    def _check(self, url: str, deadline: float) -> dict[str, Any]:
        data = self._room(self._room_id(url, deadline), deadline)
        show, loop = data.get("show_status"), data.get("videoLoop")
        if isinstance(show, bool) or isinstance(loop, bool) or show not in {1, 2} or loop not in {0, 1}:
            raise PlatformAccessError("斗鱼未返回明确直播及轮播状态", "parse")
        is_live = show == 1 and loop == 0
        room_id = _numeric_id(data["room_id"])
        return _result(self.platform, "live" if is_live else "offline", self.method,
                       nickname=str(data.get("nickname") or ""), title=str(data.get("room_name") or ""),
                       room_id=room_id, live_url=f"https://www.douyu.com/{room_id}",
                       session_id=_session(room_id, data.get("show_time")) if is_live else "")


class KuaishouAdapter(_PublicClient):
    platform = "kuaishou"
    method = "kuaishou_initial_state"

    def _state(self, url: str, deadline: float) -> dict[str, Any]:
        page = self._get(url, deadline)
        match = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", page)
        if not match:
            raise PlatformAccessError("快手匿名页面暂未提供直播状态", "restricted")
        try:
            # Kuaishou emits JavaScript undefined outside JSON strings. Preserve
            # strings verbatim and treat undefined as missing evidence, never offline.
            serialized = re.sub(r'"(?:[^"\\]|\\.)*"|\bundefined\b',
                                lambda token: "null" if token[0] == "undefined" else token[0],
                                page[match.end():])
            state, _end = json.JSONDecoder().raw_decode(serialized)
        except json.JSONDecodeError as exc:
            raise PlatformAccessError("无法解析快手匿名页面状态", "parse") from exc
        if not isinstance(state, dict):
            raise PlatformAccessError("快手匿名页面状态格式发生变化", "parse")
        # State keys differ by page version; only accept the requested author's data.
        pending = [state]
        candidates = []
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                author = item.get("author")
                author_status = isinstance(author, dict) and any(key in author for key in ("living", "isLiving"))
                if "author" in item and ("liveStream" in item or "living" in item or "isLiving" in item or author_status):
                    candidates.append(item)
                else:
                    pending.extend(value for value in item.values() if isinstance(value, (dict, list)))
            elif isinstance(item, list):
                pending.extend(item)
        target = _identifier(url)
        matched = []
        for item in candidates:
            author = item.get("author")
            if not isinstance(author, dict):
                continue
            if any(isinstance(author.get(key), (str, int)) and not isinstance(author.get(key), bool)
                   and str(author[key]) == target for key in ("principalId", "userId", "id")):
                matched.append(item)
        if len(matched) != 1:
            raise PlatformAccessError("快手匿名页面未提供唯一主播状态", "restricted")
        item = matched[0]
        if item.get("errorType"):
            raise PlatformAccessError("快手暂时限制匿名访问", "restricted")
        return item

    @staticmethod
    def _has_play_urls(play_urls: Any) -> bool:
        """Require concrete stream URL records, not a populated placeholder."""
        groups = [play_urls[key] for key in ("h264", "hevc") if key in play_urls] \
            if isinstance(play_urls, dict) else play_urls
        if not isinstance(groups, list):
            return False
        for group in groups:
            if not isinstance(group, dict):
                continue
            adaptation = group.get("adaptationSet")
            representations = adaptation.get("representation", []) if isinstance(adaptation, dict) else []
            if "url" in group:
                representations = [group]
            if not isinstance(representations, list):
                continue
            for record in representations:
                if isinstance(record, dict) and isinstance(record.get("url"), str):
                    parsed = urlparse(record["url"])
                    if parsed.scheme in {"http", "https"} and parsed.hostname:
                        return True
        return False

    def resolve(self, url: str) -> dict[str, Any]:
        clean = validate_platform_url(url, self.platform)
        url = f"https://live.kuaishou.com/u/{_identifier(clean)}"
        state = self._state(url, self._monotonic() + CHECK_BUDGET_SECONDS)
        return self._remember(_identifier(url), str(state["author"].get("name") or ""), url)

    def _check(self, url: str, deadline: float) -> dict[str, Any]:
        state = self._state(url, deadline)
        author = state["author"]
        stream = state.get("liveStream")
        live_stream = stream if isinstance(stream, dict) else {}
        offline = any(source.get(field) is False for source in (state, author, live_stream)
                      for field in ("living", "isLiving"))
        live = self._has_play_urls(live_stream.get("playUrls"))
        if live and offline:
            raise PlatformAccessError("快手匿名页面直播状态存在冲突", "parse")
        if not live and not offline:
            raise PlatformAccessError("快手匿名页面暂未提供明确直播状态", "restricted")
        stream_id = str(live_stream.get("id") or live_stream.get("liveStreamId") or "")
        return _result(self.platform, "live" if live else "offline", self.method,
                       nickname=str(author.get("name") or ""), title=str(live_stream.get("caption") or ""),
                       room_id=stream_id, session_id=stream_id if live else "", live_url=url)


_CLIENTS = {
    "douyin": DouyinAdapter, "bilibili": BilibiliAdapter, "huya": HuyaAdapter,
    "douyu": DouyuAdapter, "kuaishou": KuaishouAdapter,
}


def create_platform_client(platform: str, debug: bool = False):
    try:
        client_type = _CLIENTS[platform]
    except KeyError as exc:
        raise ProfileLinkError("暂不支持该直播平台") from exc
    return client_type(debug=debug)
