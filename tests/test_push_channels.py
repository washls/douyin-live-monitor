"""Protocol, result and formatting checks without real push requests."""

import base64
import hashlib
import hmac
import json
from dataclasses import FrozenInstanceError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
import requests

from douyin_monitor.notifier import MultiChannelNotifier, ServerChanNotifier
from douyin_monitor.push_channels import (
    ChannelResult, DeliveryReport, PushMessage, create_push_channel, markdown_to_text,
)

CHANNELS = [
    {"id": "server", "name": "Server 酱", "type": "serverchan",
     "config": {"push_url": "https://example.push.ft07.com/send/example-key.send"}},
    {"id": "plus", "name": "PushPlus", "type": "pushplus",
     "config": {"token": "example-token", "topic": "example-topic"}},
    {"id": "bark", "name": "Bark", "type": "bark",
     "config": {"server_url": "https://example.invalid/proxy", "device_key": "example-device", "group": "直播"}},
    {"id": "feishu", "name": "飞书", "type": "feishu",
     "config": {"webhook_url": "https://example.invalid/feishu", "secret": "example-secret", "keyword": "直播"}},
    {"id": "ding", "name": "钉钉", "type": "dingtalk",
     "config": {"webhook_url": "https://example.invalid/ding?access_token=example-token",
                "secret": "example-secret", "keyword": "直播"}},
    {"id": "ntfy", "name": "ntfy", "type": "ntfy",
     "config": {"server_url": "https://example.invalid/proxy", "topic": "example-topic", "token": "example-token"}},
    {"id": "hook", "name": "Webhook", "type": "webhook",
     "config": {"url": "https://example.invalid/webhook", "headers": {"Authorization": "Bearer example-token"}}},
]

ACKS = {"serverchan": {"code": 0}, "pushplus": {"code": 200}, "bark": {"code": 200},
        "feishu": {"code": 0}, "dingtalk": {"errcode": 0}, "ntfy": {"id": "example-message-id"},
        "webhook": {}}


def response(status=200, body=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({} if body is None else body).encode()
    result._content_consumed = True
    return result


def message():
    body = "**示例主播** 开播\n\n[点击观看](https://live.example.invalid/123)"
    return PushMessage("live", "示例主播 开播", body, markdown_to_text(body),
                       "https://live.example.invalid/123", "example-streamer", "example-event")


@pytest.mark.parametrize("config", CHANNELS, ids=lambda config: config["type"])
def test_each_channel_posts_once_with_correct_protocol(monkeypatch, config):
    kind = config["type"]
    post = Mock(return_value=response(body=ACKS[kind]))
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    monkeypatch.setattr("douyin_monitor.push_channels.time.time", lambda: 1700000000)
    result = create_push_channel(config).send(message())
    assert result.status == "accepted"
    assert (result.channel_id, result.name, result.type) == (config["id"], config["name"], kind)
    assert result.http_status == 200
    assert result.elapsed_ms >= 0
    post.assert_called_once()
    endpoint = post.call_args.args[0]
    arguments = post.call_args.kwargs
    assert arguments["allow_redirects"] is False
    assert arguments["timeout"] == 15
    assert arguments["headers"]["Content-Type"] == "application/json;charset=utf-8"
    body = arguments["json"]
    if kind == "serverchan":
        assert endpoint == config["config"]["push_url"]
        assert body == {"title": message().title, "desp": message().markdown}
    elif kind == "pushplus":
        assert endpoint == "https://www.pushplus.plus/send"
        assert body == {"token": "example-token", "title": message().title,
                        "content": message().markdown, "template": "markdown", "topic": "example-topic"}
    elif kind == "bark":
        assert endpoint == "https://example.invalid/proxy/push"
        assert body == {"device_key": "example-device", "title": message().title,
                        "body": message().text, "group": "直播", "url": message().live_url}
    elif kind == "feishu":
        assert endpoint == config["config"]["webhook_url"]
        assert body["msg_type"] == "text"
        assert body["content"]["text"] == "直播\n" + message().title + "\n\n" + message().text
        assert body["timestamp"] == "1700000000"
        expected = hmac.new(b"1700000000\nexample-secret", b"", hashlib.sha256).digest()
        assert body["sign"] == base64.b64encode(expected).decode()
    elif kind == "dingtalk":
        assert body == {"msgtype": "markdown", "markdown": {"title": message().title,
                                                               "text": "直播\n\n" + message().markdown}}
        query = parse_qs(urlsplit(endpoint).query)
        assert query["access_token"] == ["example-token"]
        assert query["timestamp"] == ["1700000000000"]
        expected = hmac.new(b"example-secret", b"1700000000000\nexample-secret", hashlib.sha256).digest()
        assert query["sign"] == [base64.b64encode(expected).decode()]
    elif kind == "ntfy":
        assert endpoint == "https://example.invalid/proxy/"
        assert body == {"topic": "example-topic", "title": message().title,
                        "message": message().text, "click": message().live_url}
        assert arguments["headers"]["Authorization"] == "Bearer example-token"
    else:
        assert endpoint == config["config"]["url"]
        assert body == {"event_type": "live", "title": message().title,
                        "body": message().text, "live_url": message().live_url}
        assert arguments["headers"]["Authorization"] == "Bearer example-token"


@pytest.mark.parametrize("config", CHANNELS, ids=lambda config: config["type"])
@pytest.mark.parametrize("exception,code", [(requests.Timeout, "timeout"),
                                            (requests.ConnectionError, "connection_error")])
def test_transport_unknown_is_never_retried(monkeypatch, config, exception, code):
    post = Mock(side_effect=exception("contains-private-request-url"))
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    result = create_push_channel(config).send(message())
    assert (result.status, result.error_code) == ("unknown", code)
    assert "private" not in repr(result)
    assert post.call_count == 1


@pytest.mark.parametrize("status,expected", [(302, "rejected"), (401, "rejected"),
                                            (429, "rejected"), (500, "unknown"), (503, "unknown")])
def test_http_classification_and_no_redirect_replay(monkeypatch, status, expected):
    post = Mock(return_value=response(status))
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    result = create_push_channel(CHANNELS[-1]).send(message())
    assert (result.status, result.http_status) == (expected, status)
    assert post.call_count == 1
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("config", CHANNELS[:5], ids=lambda config: config["type"])
def test_explicit_provider_rejection_is_distinct_from_invalid_ack(monkeypatch, config):
    key = "errcode" if config["type"] == "dingtalk" else "code"
    post = Mock(side_effect=[response(body={key: 400}), response(body={})])
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    adapter = create_push_channel(config)
    assert adapter.send(message()).status == "rejected"
    assert adapter.send(message()).status == "unknown"
    assert post.call_count == 2


@pytest.mark.parametrize("body", [[], None, True, {"code": False}])
def test_non_mapping_or_missing_valid_ack_is_unknown(monkeypatch, body):
    reply = response()
    reply._content = json.dumps(body).encode()
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", Mock(return_value=reply))
    assert create_push_channel(CHANNELS[0]).send(message()).status == "unknown"


def test_non_json_is_unknown_but_webhook_204_needs_no_body(monkeypatch):
    reply = response()
    reply._content = b"not json"
    post = Mock(side_effect=[reply, response(204)])
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    assert create_push_channel(CHANNELS[0]).send(message()).status == "unknown"
    assert create_push_channel(CHANNELS[-1]).send(message()).status == "accepted"


def test_feishu_accepts_legacy_success_response(monkeypatch):
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", Mock(return_value=response(body={"StatusCode": 0})))
    assert create_push_channel(CHANNELS[3]).send(message()).status == "accepted"


def test_response_and_request_secrets_are_not_logged(monkeypatch, caplog):
    reply = response(body={"code": 400, "message": "PRIVATE_RESPONSE"})
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", Mock(return_value=reply))
    with caplog.at_level("INFO"):
        create_push_channel(CHANNELS[0]).send(message())
    for private in ("example-key", "push.ft07.com", "PRIVATE_RESPONSE"):
        assert private not in caplog.text


def test_result_and_message_are_immutable_and_summary_preserves_unknown():
    accepted = ChannelResult("a", "示例", "webhook", "accepted")
    report = DeliveryReport("event", (accepted, ChannelResult("b", "示例2", "ntfy", "unknown")))
    assert report.accepted_count == 1
    assert report.unknown_count == 1
    assert report.delivery_unknown
    assert not report.all_accepted
    assert report.summary == "已受理 1/2，失败 0，结果未知 1，取消 0"
    with pytest.raises(FrozenInstanceError):
        accepted.status = "rejected"
    with pytest.raises(FrozenInstanceError):
        message().title = "changed"
    assert not DeliveryReport("empty").all_accepted
    mutable_input = [accepted]
    immutable_report = DeliveryReport("event", mutable_input)
    mutable_input.clear()
    assert immutable_report.results == (accepted,)


@pytest.mark.parametrize("method,args,expected", [
    ("send_live_notification", {"nickname": "示例", "room_id": "123"}, "live"),
    ("send_repeat_live_notification", {"nickname": "示例"}, "repeat_live"),
    ("send_stream_end_notification", {"nickname": "示例"}, "stream_end"),
    ("send_daily_intimacy_reminder", {"nickname": "示例", "minute": 57}, "daily_intimacy"),
    ("send_monitor_alert", {"nickname": "示例", "reason": "网络错误", "duration": 300, "retrying": False}, "monitor_alert"),
    ("send_monitor_recovery", {"nickname": "示例", "duration": 300}, "monitor_recovery"),
])
def test_shared_formatter_preserves_event_type_and_watch_link(monkeypatch, method, args, expected):
    post = Mock(return_value=response())
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    with MultiChannelNotifier([CHANNELS[-1]]) as notifier:
        assert getattr(notifier, method)(**args, live_url=message().live_url)
    body = post.call_args.kwargs["json"]
    assert set(body) == {"event_type", "title", "body", "live_url"}
    assert body["event_type"] == expected
    assert body["live_url"] == message().live_url
    assert message().live_url in body["body"]
    assert "示例" in body["body"]


def test_multi_streamer_daily_is_one_broadcast_with_empty_watch_link(monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    with MultiChannelNotifier([CHANNELS[-1]]) as notifier:
        assert notifier.send_daily_intimacy_reminder_for_streamers([
            {"nickname": "甲", "live_url": "https://live.example.invalid/1"},
            {"nickname": "乙", "live_url": "https://live.example.invalid/2"},
        ], 57)
    post.assert_called_once()
    body = post.call_args.kwargs["json"]
    assert body["event_type"] == "daily_intimacy"
    assert body["live_url"] == ""
    assert "https://live.example.invalid/1" in body["body"]
    assert "https://live.example.invalid/2" in body["body"]


def test_legacy_clone_custom_url_and_single_attempt_are_preserved(monkeypatch):
    old = ServerChanNotifier("example-key", "example-uid", retry_times=9, retry_delay=9)
    old.set_api_url("https://example.invalid/custom")
    cloned = old.clone()
    post = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr("douyin_monitor.notifier.requests.post", post)
    assert cloned.api_url == old.api_url
    assert cloned.retry_times == 9
    assert not cloned.send("test")
    assert cloned.delivery_unknown
    assert not old.delivery_unknown
    assert post.call_count == 1


@pytest.mark.parametrize("markdown,url", [
    ("**主播** [点击观看](https://live.example.invalid/sec__uid/a(b))", "https://live.example.invalid/sec__uid/a(b)"),
    ("**主播** https://live.example.invalid/sec__uid/a(b)", "https://live.example.invalid/sec__uid/a(b)"),
    ("[主播主页](https://example.invalid/user/abc__def?key=__value__)", "https://example.invalid/user/abc__def?key=__value__"),
])
def test_plain_text_preserves_exact_urls_and_literal_underscores(markdown, url):
    result = markdown_to_text(markdown + "\n正文包含 abc__def，房间 `123`")
    assert url in result
    assert "abc__def" in result
    assert "**主播**" not in result
    assert "房间 123" in result


@pytest.mark.parametrize("kind", ["feishu", "ntfy"])
def test_long_chinese_message_is_one_summary_with_header_count_and_complete_url(monkeypatch, kind):
    url = "https://live.example.invalid/sec__uid/a(b)"
    markdown = "每日亲密度刷新提醒\n\n当前有 **100 位主播** 正在直播：\n\n" + "示例主播正在直播。" * 5000
    markdown += f"\n[点击观看]({url})"
    msg = PushMessage("daily_intimacy", "100 位主播亲密度提醒 " + "中文" * 2000,
                      markdown, markdown_to_text(markdown), url)
    post = Mock(return_value=response(body=ACKS[kind]))
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    selected = next(config for config in CHANNELS if config["type"] == kind)
    assert create_push_channel(selected).send(msg).status == "accepted"
    post.assert_called_once()
    payload = post.call_args.kwargs["json"]
    if kind == "ntfy":
        assert len(payload["title"].encode("utf-8")) <= 1024
        assert len(payload["message"].encode("utf-8")) <= 4096
        text = payload["message"]
    else:
        assert len(json.dumps(payload, allow_nan=False).encode("utf-8")) <= 20000
        assert payload["content"]["text"].startswith("直播\n100 位主播")
        text = payload["content"]["text"]
    assert "每日亲密度刷新提醒" in text
    assert "100 位主播" in text
    assert url in text
    assert text.endswith(url)
    assert "…" in text


def test_overlong_aggregate_uses_first_complete_markdown_url_in_summary(monkeypatch):
    url = "https://live.example.invalid/sec__uid/a(b)"
    markdown = "当前有 **100 位主播** 正在直播：\n" + "示例直播内容" * 3000 + f"\n[观看]({url})"
    post = Mock(return_value=response(body=ACKS["ntfy"]))
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    msg = PushMessage("daily_intimacy", "100 位主播", markdown, markdown_to_text(markdown))
    assert create_push_channel(CHANNELS[5]).send(msg).status == "accepted"
    text = post.call_args.kwargs["json"]["message"]
    assert "100 位主播" in text
    assert text.endswith(url)
    assert len(text.encode("utf-8")) <= 4096


@pytest.mark.parametrize("kind", ["feishu", "ntfy"])
def test_unrepresentable_full_link_is_rejected_before_post(monkeypatch, kind):
    post = Mock()
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", post)
    selected = next(config for config in CHANNELS if config["type"] == kind)
    huge_url = "https://example.invalid/" + "a" * 30000
    msg = PushMessage("live", "开播", "中文" * 30000, "中文" * 30000, huge_url)
    assert create_push_channel(selected).send(msg).status == "rejected"
    post.assert_not_called()


@pytest.mark.parametrize("status,body", [(200, {"code": 0}), (200, {"code": 400}),
                                       (200, []), (500, {})])
def test_every_received_response_is_closed(monkeypatch, status, body):
    reply = response(status, body)
    reply.close = Mock()
    monkeypatch.setattr("douyin_monitor.push_channels.requests.post", Mock(return_value=reply))
    create_push_channel(CHANNELS[0]).send(message())
    reply.close.assert_called_once()


def test_legacy_response_is_closed_even_on_json_error(monkeypatch):
    reply = response()
    reply._content = b"invalid json"
    reply.close = Mock()
    monkeypatch.setattr("douyin_monitor.notifier.requests.post", Mock(return_value=reply))
    assert not ServerChanNotifier("example", "example").send("test")
    reply.close.assert_called_once()


def test_legacy_custom_url_and_response_body_stay_out_of_logs(monkeypatch, caplog):
    reply = response(body={"code": 400, "message": "PRIVATE_RESPONSE"})
    monkeypatch.setattr("douyin_monitor.notifier.requests.post", Mock(return_value=reply))
    notifier = ServerChanNotifier("example", "example")
    with caplog.at_level("INFO"):
        notifier.set_api_url("https://example.invalid/send?token=PRIVATE_TOKEN")
        assert not notifier.send("test")
    assert "PRIVATE_RESPONSE" not in caplog.text
    assert "PRIVATE_TOKEN" not in caplog.text
    assert "example.invalid" not in caplog.text
