"""Single-attempt push transports and a bounded, shared dispatcher."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import requests

logger = logging.getLogger(__name__)


def _markdown_links(markdown: str):
    """Find link destinations with balanced parentheses without rewriting URLs."""
    previous_end = 0
    for match in re.finditer(r"\[([^\]\n]+)\]\(", markdown):
        if match.start() < previous_end:
            continue
        end, depth = match.end(), 1
        while end < len(markdown) and depth:
            depth += (markdown[end] == "(") - (markdown[end] == ")")
            end += 1
        if depth == 0:
            previous_end = end
            yield match.start(), end, match[1], markdown[match.end():end - 1]


def markdown_to_text(markdown: str) -> str:
    """Convert formatting while preserving every character of link URLs."""
    protected = []

    def protect(value):
        protected.append(value)
        return f"\x00link{len(protected) - 1}\x00"

    parts, previous_end = [], 0
    for start, end, label, url in _markdown_links(markdown):
        parts.extend((markdown[previous_end:start], protect(f"{label} ({url})")))
        previous_end = end
    parts.append(markdown[previous_end:])
    text = re.sub(r"https?://[^\s<>]+", lambda match: protect(match[0]), "".join(parts))
    text = text.replace("**", "")
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^(?:#{1,6}\s+|>\s?)", "", text)
    for index, value in enumerate(protected):
        text = text.replace(f"\x00link{index}\x00", value)
    return text


def _watch_url(message: "PushMessage") -> str:
    if message.live_url:
        return message.live_url
    for _start, _end, _label, url in _markdown_links(message.markdown):
        if url.startswith(("http://", "https://")):
            return url
    found = re.search(r"https?://[^\s<>]+", message.text)
    return found[0] if found else ""


def _short_text(text: str, limit: int, url: str = "", measure=None) -> str:
    """Keep the event header and one complete watch URL in a single summary."""
    if measure is None:
        def measure(value):
            return len(value.encode("utf-8"))
    if measure(text) <= limit:
        return text
    suffix = "\n\n…" + (f"\n观看链接: {url}" if url else "")
    if measure(suffix) > limit:
        raise ValueError("推送链接超出渠道长度限制")
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + suffix) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix


def _short_title(title: str, limit: int) -> str:
    if len(title.encode("utf-8")) <= limit:
        return title
    return title.encode("utf-8")[:limit - 3].decode("utf-8", errors="ignore") + "…"


@dataclass(frozen=True)
class PushMessage:
    event_type: str
    title: str
    markdown: str = ""
    text: str = ""
    live_url: str = ""
    streamer_id: str = ""
    event_id: str = field(default_factory=lambda: uuid4().hex)
    tags: str = ""
    short: str = ""


@dataclass(frozen=True)
class ChannelResult:
    channel_id: str
    name: str
    type: str
    status: str
    http_status: int | None = None
    error_code: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class DeliveryReport:
    event_id: str
    results: tuple[ChannelResult, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def accepted_count(self) -> int:
        return sum(item.status == "accepted" for item in self.results)

    @property
    def unknown_count(self) -> int:
        return sum(item.status == "unknown" for item in self.results)

    @property
    def delivery_unknown(self) -> bool:
        return self.unknown_count > 0

    @property
    def all_accepted(self) -> bool:
        return bool(self.results) and self.accepted_count == len(self.results)

    @property
    def summary(self) -> str:
        if not self.results:
            return "未选择推送渠道，本次不推送"
        rejected = sum(item.status == "rejected" for item in self.results)
        cancelled = sum(item.status == "cancelled" for item in self.results)
        return (f"已受理 {self.accepted_count}/{len(self.results)}，"
                f"失败 {rejected}，结果未知 {self.unknown_count}，取消 {cancelled}")


class PushChannel:
    """A stateless transport: one invocation submits at most one POST."""

    def __init__(self, channel: Mapping[str, Any]):
        self.channel_id = str(channel["id"])
        self.name = str(channel["name"])
        self.type = str(channel["type"])
        self.config = deepcopy(dict(channel["config"]))

    def result(self, status: str, http_status: int | None = None,
               error_code: str = "", elapsed_ms: int = 0) -> ChannelResult:
        return ChannelResult(self.channel_id, self.name, self.type, status,
                             http_status, error_code, elapsed_ms)

    def prepare(self, message: PushMessage) -> tuple[str, dict[str, Any], dict[str, str]]:
        raise NotImplementedError

    def acknowledgement(self, body: Mapping[str, Any]) -> tuple[str, str]:
        raise NotImplementedError

    def send(self, message: PushMessage) -> ChannelResult:
        started = time.monotonic()
        status, error_code, http_status = "unknown", "transport_error", None
        response = None
        try:
            url, payload, headers = self.prepare(message)
        except Exception:
            return self.result("rejected", error_code="invalid_request")
        try:
            response = requests.post(
                url, json=payload,
                headers={**headers, "Content-Type": "application/json;charset=utf-8"},
                timeout=15, allow_redirects=False,
            )
            http_status = response.status_code
            if 200 <= http_status < 300:
                if self.type == "webhook":
                    status, error_code = "accepted", ""
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if isinstance(body, Mapping):
                        status, error_code = self.acknowledgement(body)
                    else:
                        status, error_code = "unknown", "invalid_response"
            elif http_status >= 500:
                status, error_code = "unknown", "http_server_error"
            else:
                status, error_code = "rejected", "http_rejected"
        except requests.HTTPError as exc:
            response = exc.response
            http_status = exc.response.status_code if exc.response is not None else None
            status = "unknown" if http_status is None or http_status >= 500 else "rejected"
            error_code = "http_error"
        except requests.Timeout:
            error_code = "timeout"
        except requests.ConnectionError:
            error_code = "connection_error"
        except Exception:
            error_code = "transport_error"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as exc:
                    logger.warning("推送响应关闭异常 (%s)", type(exc).__name__)
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        result = self.result(status, http_status, error_code, elapsed)
        logger.info("推送渠道 %s: %s (%s)", self.type, status, error_code or "ok")
        return result


def _code_ack(body: Mapping[str, Any], key: str, accepted: int) -> tuple[str, str]:
    code = body.get(key)
    if not isinstance(code, int) or isinstance(code, bool):
        return "unknown", "invalid_response"
    return ("accepted", "") if code == accepted else ("rejected", "provider_rejected")


class ServerChanChannel(PushChannel):
    def prepare(self, message: PushMessage):
        config = self.config
        url = config.get("push_url") or (
            f"https://{config.get('uid', '')}.push.ft07.com/send/{config.get('sendkey', '')}.send"
        )
        body = {"title": message.title}
        for key, value in (("desp", message.markdown), ("tags", message.tags), ("short", message.short)):
            if value:
                body[key] = value
        return url, body, {}

    def acknowledgement(self, body):
        return _code_ack(body, "code", 0)


class PushPlusChannel(PushChannel):
    def prepare(self, message: PushMessage):
        body = {"token": self.config["token"], "title": message.title,
                "content": message.markdown or message.text, "template": "markdown"}
        if self.config.get("topic"):
            body["topic"] = self.config["topic"]
        return "https://www.pushplus.plus/send", body, {}

    def acknowledgement(self, body):
        return _code_ack(body, "code", 200)


class BarkChannel(PushChannel):
    def prepare(self, message: PushMessage):
        body = {"device_key": self.config["device_key"], "title": message.title,
                "body": message.text or markdown_to_text(message.markdown)}
        if self.config.get("group"):
            body["group"] = self.config["group"]
        if message.live_url:
            body["url"] = message.live_url
        return self.config.get("server_url", "https://api.day.app").rstrip("/") + "/push", body, {}

    def acknowledgement(self, body):
        return _code_ack(body, "code", 200)


class FeishuChannel(PushChannel):
    def prepare(self, message: PushMessage):
        heading = _short_title(message.title, 1024) + "\n\n"
        if self.config.get("keyword"):
            heading = self.config["keyword"] + "\n" + heading
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": ""}}
        if self.config.get("secret"):
            timestamp = str(int(time.time()))
            key = f"{timestamp}\n{self.config['secret']}".encode()
            body.update(timestamp=timestamp,
                        sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
        def serialized_size(value):
            payload = {**body, "content": {"text": heading + value}}
            return len(json.dumps(payload, allow_nan=False).encode("utf-8"))

        text = message.text or markdown_to_text(message.markdown)
        body["content"]["text"] = heading + _short_text(text, 20000, _watch_url(message), serialized_size)
        return self.config["webhook_url"], body, {}

    def acknowledgement(self, body):
        return _code_ack(body, "code" if "code" in body else "StatusCode", 0)


class DingTalkChannel(PushChannel):
    def prepare(self, message: PushMessage):
        text = message.markdown or message.text
        if self.config.get("keyword"):
            text = self.config["keyword"] + "\n\n" + text
        body = {"msgtype": "markdown", "markdown": {"title": message.title, "text": text}}
        url = self.config["webhook_url"]
        if self.config.get("secret"):
            timestamp = str(int(time.time() * 1000))
            secret = self.config["secret"]
            signature = base64.b64encode(
                hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
            ).decode()
            parts = urlsplit(url)
            query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                     if key not in {"timestamp", "sign"}]
            query.extend((("timestamp", timestamp), ("sign", signature)))
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        return url, body, {}

    def acknowledgement(self, body):
        return _code_ack(body, "errcode", 0)


class NtfyChannel(PushChannel):
    def prepare(self, message: PushMessage):
        body = {"topic": self.config["topic"], "title": _short_title(message.title, 1024),
                "message": _short_text(message.text or markdown_to_text(message.markdown) or message.title,
                                       4096, _watch_url(message))}
        if message.live_url:
            body["click"] = message.live_url
        headers = {}
        if self.config.get("token"):
            headers["Authorization"] = "Bearer " + self.config["token"]
        return self.config.get("server_url", "https://ntfy.sh").rstrip("/") + "/", body, headers

    def acknowledgement(self, body):
        return ("accepted", "") if isinstance(body.get("id"), str) and body["id"] else (
            "unknown", "invalid_response"
        )


class WebhookChannel(PushChannel):
    def prepare(self, message: PushMessage):
        body = {"event_type": message.event_type, "title": message.title,
                "body": message.text or markdown_to_text(message.markdown), "live_url": message.live_url}
        return self.config["url"], body, dict(self.config.get("headers", {}))


_CHANNEL_TYPES = {"serverchan": ServerChanChannel, "pushplus": PushPlusChannel,
                  "bark": BarkChannel, "feishu": FeishuChannel, "dingtalk": DingTalkChannel,
                  "ntfy": NtfyChannel, "webhook": WebhookChannel}


def create_push_channel(channel: Mapping[str, Any]) -> PushChannel:
    return _CHANNEL_TYPES[str(channel["type"])](channel)


class PushDispatcher:
    """Four global request slots, including work submitted to the executor."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="push-channel")
        self._slots = threading.BoundedSemaphore(4)
        self._stopped = threading.Event()
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False

    def _cancelled(self, cancelled: Callable[[], bool] | None) -> bool:
        if self._stopped.is_set() or cancelled is None:
            return self._stopped.is_set()
        try:
            return bool(cancelled())
        except Exception as exc:
            logger.warning("推送取消检查异常 (%s)", type(exc).__name__)
            return True

    def _deliver(self, channel: PushChannel, message: PushMessage,
                 cancelled: Callable[[], bool] | None) -> ChannelResult:
        if self._cancelled(cancelled):
            return channel.result("cancelled", error_code="stopped")
        try:
            return channel.send(message)
        except Exception:
            return channel.result("unknown", error_code="adapter_error")

    def send(self, message: PushMessage, channels: Sequence[PushChannel], *,
             cancelled: Callable[[], bool] | None = None) -> DeliveryReport:
        pending: list[tuple[PushChannel, Future | None]] = []
        for channel in channels:
            acquired = False
            while not self._cancelled(cancelled):
                acquired = self._slots.acquire(timeout=0.05)
                if acquired:
                    break
            if not acquired:
                pending.append((channel, None))
                continue
            if self._cancelled(cancelled):
                self._slots.release()
                pending.append((channel, None))
                continue
            with self._state_lock:
                if self._stopped.is_set():
                    self._slots.release()
                    pending.append((channel, None))
                    continue
                try:
                    future = self._executor.submit(copy_context().run, self._deliver, channel, message, cancelled)
                except RuntimeError:
                    self._slots.release()
                    pending.append((channel, None))
                    continue
                future.add_done_callback(lambda _future: self._slots.release())
                pending.append((channel, future))
        results = []
        for channel, future in pending:
            if future is None:
                result = channel.result("cancelled", error_code="stopped")
            else:
                try:
                    result = future.result()
                except CancelledError:
                    result = channel.result("cancelled", error_code="stopped")
                except Exception:
                    result = channel.result("unknown", error_code="adapter_error")
            results.append(result)
        return DeliveryReport(message.event_id, tuple(results))

    def stop(self) -> None:
        with self._state_lock:
            self._stopped.set()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.stop()
            self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
