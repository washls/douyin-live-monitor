"""Synthetic HTTP evidence exercises live decisions and trust boundaries."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
import requests

from douyin_monitor import platform_clients as clients
from douyin_monitor.monitor_errors import ProfileLinkError


class FakeResponse:
    def __init__(self, payload=None, text=None, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = (text if text is not None else json.dumps(payload)).encode()
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def iter_content(self, **_kwargs):
        yield from self.chunks if self.chunks is not None else [self.body]


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.headers = {}
        self.trust_env = True
        self.calls = []
        self.closed = False

    def mount(self, *_args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0) if self.responses else FakeResponse({})
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(clients.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("8.8.8.8", 443)),
    ])


def build(monkeypatch, platform, responses):
    session = FakeSession(responses)
    monkeypatch.setattr(clients.requests, "Session", lambda: session)
    adapter = clients.create_platform_client(platform)
    return adapter, session


@pytest.mark.parametrize("url,platform", [
    ("https://www.douyin.com/user/synthetic_user", "douyin"),
    ("https://v.douyin.com/synthetic/", "douyin"),
    ("https://www.iesdouyin.com/share/user/synthetic_user", "douyin"),
    ("http://live.bilibili.com:80/100000001?from=example", "bilibili"),
    ("https://live.bilibili.com/blanc/100000001", "bilibili"),
    ("https://www.huya.com/synthetic_alias", "huya"),
    ("https://m.douyu.com/synthetic_alias", "douyu"),
    ("https://live.kuaishou.com/u/synthetic_user", "kuaishou"),
])
def test_infer_and_normalize(url, platform):
    assert clients.infer_platform(url) == platform
    clean = clients.validate_platform_url(url, platform)
    assert clean.startswith("https://")
    assert ":80" not in clean


@pytest.mark.parametrize("url", [
    "https://channels.weixin.qq.com/", "https://www.example.com/1",
    "https://live.bilibili.com.example.com/1", "https://127.0.0.1/1",
    "https://www.huya.com@127.0.0.1/1", "https://user:password@www.huya.com/1",
    "https://www.huya.com:8443/1", "https://www.huya.com:80/1",
    "ftp://www.huya.com/1", "https://www.huya.com/a\\b",
    "https://www.huya.com/1\n", "https://live.bilibili.com/video/example",
    "https://live.kuaishou.com/profile/example", "https://www.huya.com/directory",
])
def test_invalid_input_rejected(url):
    # Leading/trailing whitespace is deliberately trimmed as in legacy config.
    if url.endswith("\n"):
        url = url + "suffix"
    with pytest.raises(ProfileLinkError):
        clients.validate_platform_url(url)


def test_platform_mismatch_and_video_channel_message():
    with pytest.raises(ProfileLinkError, match="平台与主播链接不一致"):
        clients.validate_platform_url("https://www.huya.com/100000001", "douyu")
    with pytest.raises(ProfileLinkError, match="视频号当前不支持免登录监控"):
        clients.infer_platform("https://channels.weixin.qq.com/example")


@pytest.mark.parametrize("status,expected", [(1, "live"), (0, "offline"), (2, "offline")])
def test_bilibili_status_and_canonical_short_room(monkeypatch, status, expected):
    room = {"code": 0, "data": {"room_id": 100000001, "uid": 100000002,
                                "live_status": status, "live_time": 1800000000}}
    metadata = {"data": {"anchor_info": {"base_info": {"uname": "示例主播"}},
                         "room_info": {"title": "示例直播"}}}
    adapter, session = build(monkeypatch, "bilibili", [
        FakeResponse(room), FakeResponse(metadata), FakeResponse(room), FakeResponse(metadata),
    ])
    prepared = adapter.resolve("https://live.bilibili.com/1")
    assert prepared["target_id"] == "100000001"
    assert prepared["url"] == "https://live.bilibili.com/100000001"
    result = adapter.check(prepared["url"])
    assert result["status"] == expected
    assert result["is_live"] is (expected == "live")
    assert result["indeterminate"] is False
    assert result["nickname"] == "示例主播"
    assert result["title"] == "示例直播"
    assert result["session_id"] == ("100000001:1800000000" if status == 1 else "")
    assert session.trust_env is False
    adapter.close()
    adapter.close()
    assert session.closed


@pytest.mark.parametrize("payload", [
    {}, {"code": 0, "data": {}}, {"code": 0, "data": {"room_id": 100000001}},
    {"code": 0, "data": {"room_id": 100000001, "live_status": 99}},
    {"code": 0, "data": {"room_id": 100000001, "live_status": False}},
    {"code": 0, "data": {"room_id": 100000001, "live_status": "1"}},
])
def test_bilibili_malformed_response_is_unknown(monkeypatch, payload):
    adapter, _session = build(monkeypatch, "bilibili", [FakeResponse(payload)])
    result = adapter.check("https://live.bilibili.com/100000001")
    assert result["status"] == "unknown"
    assert result["indeterminate"] is True
    assert result["is_live"] is False


def test_optional_bilibili_metadata_failure_keeps_authoritative_status(monkeypatch):
    adapter, _session = build(monkeypatch, "bilibili", [
        FakeResponse({"code": 0, "data": {"room_id": 100000001, "live_status": 1}}),
        requests.Timeout("synthetic timeout"),
    ])
    result = adapter.check("https://live.bilibili.com/100000001")
    assert result["status"] == "live"
    assert result["session_id"] == ""


def test_optional_metadata_rate_limit_preserves_primary_evidence_and_cooldown(monkeypatch):
    adapter, _session = build(monkeypatch, "bilibili", [
        FakeResponse({"code": 0, "data": {"room_id": 100000001, "live_status": 0}}),
        FakeResponse(status=429, headers={"Retry-After": "90"}),
    ])
    result = adapter.check("https://live.bilibili.com/100000001")
    assert result["status"] == "offline"
    assert result["cooldown"] == 90
    assert result["error_kind"] == "rate_limited"


@pytest.mark.parametrize("value,expected", [
    ("0000-00-00 00:00:00", ""), ("2026-99-99 00:00:00", ""),
    ("1999-12-31 23:59:59", ""), (0, ""), (100, ""),
    ("2026-10-02 17:00:00", "room:2026-10-02 17:00:00"),
    (1800000000, "room:1800000000"),
])
def test_session_id_requires_credible_date_or_timestamp(value, expected):
    assert clients._session("room", value) == expected


@pytest.mark.parametrize("value,expected", [("ON", "live"), ("OFF", "offline"),
                                            (None, "unknown"), ("REPLAY", "unknown")])
def test_huya_alias_and_true_status(monkeypatch, value, expected):
    payload = {"data": {"realLiveStatus": value,
                        "profileInfo": {"profileRoom": 100000001, "nick": "示例主播"},
                        "liveData": {"introduction": "示例标题"}}}
    adapter, session = build(monkeypatch, "huya", [
        FakeResponse(text='{"ProfileRoom":100000001,"sPrivateHost":""}'),
        FakeResponse(payload), FakeResponse(payload),
    ])
    prepared = adapter.resolve("https://www.huya.com/synthetic_alias")
    assert prepared["target_id"] == "100000001"
    assert session.calls[0][0] == "https://m.huya.com/synthetic_alias"
    result = adapter.check(prepared["url"])
    assert result["status"] == expected
    assert result["session_id"] == ""


@pytest.mark.parametrize("show,loop,expected", [
    (1, 0, "live"), (1, 1, "offline"), (2, 0, "offline"), (2, 1, "offline"),
    (None, 0, "unknown"), (1, None, "unknown"), (1, 2, "unknown"),
    (True, 0, "unknown"), (1, False, "unknown"),
])
def test_douyu_excludes_loop_and_requires_explicit_status(monkeypatch, show, loop, expected):
    payload = {"room": {"room_id": 100000001, "nickname": "示例主播",
                        "room_name": "示例标题", "show_status": show, "videoLoop": loop}}
    adapter, _session = build(monkeypatch, "douyu", [FakeResponse(payload)])
    result = adapter.check("https://www.douyu.com/100000001")
    assert result["status"] == expected
    assert result["is_live"] is (expected == "live")


def test_douyu_alias_resolves_canonical_identity(monkeypatch):
    adapter, _session = build(monkeypatch, "douyu", [
        FakeResponse(text='{"rid":100000001}'),
        FakeResponse({"room": {"room_id": 100000001, "nickname": "示例主播"}}),
    ])
    result = adapter.resolve("https://www.douyu.com/synthetic_alias")
    assert result["target_id"] == "100000001"
    assert result["url"] == "https://www.douyu.com/100000001"


def kuaishou_page(state):
    return '<script>window.__INITIAL_STATE__=' + json.dumps(state) + ';(function(){var s;</script>'


@pytest.mark.parametrize("stream,extra,expected", [
    ({"id": "synthetic_session", "playUrls": [{"url": "https://example.com/synthetic.flv"}]}, {}, "live"),
    (None, {}, "unknown"), ({}, {}, "unknown"),
    (None, {"living": False}, "offline"), ({}, {"isLiving": False}, "offline"),
    ({"playUrls": [{"url": "https://example.com/synthetic.flv"}]}, {"living": False}, "unknown"),
    (None, {"living": "false"}, "unknown"),
])
def test_kuaishou_never_infers_offline_from_missing_stream(monkeypatch, stream, extra, expected):
    state = {"room": {"author": {"name": "示例主播", "principalId": "synthetic_user"},
                       "liveStream": stream, **extra}}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    result = adapter.check("https://live.kuaishou.com/u/synthetic_user")
    assert result["status"] == expected
    assert result["live_url"] == "https://live.kuaishou.com/u/synthetic_user"
    if expected == "live":
        assert result["session_id"] == "synthetic_session"


def test_kuaishou_unrelated_recommendation_is_not_live_evidence(monkeypatch):
    state = {"room": {"author": {"principalId": "another_user"},
                       "liveStream": {"playUrls": ["synthetic_url"]}}}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == "unknown"


@pytest.mark.parametrize("author", [{"name": "示例推荐"}, {"id": "another_user"}, {"id": None}])
def test_kuaishou_recommendation_without_exact_identity_is_unknown(monkeypatch, author):
    state = {"recommendations": [{"author": author, "liveStream": {
        "playUrls": [{"url": "https://example.com/synthetic.flv"}],
    }}]}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == "unknown"


@pytest.mark.parametrize("play_urls", [
    "malformed", ["https://example.com/synthetic.flv"], {"h264": {}},
    {"unknown": {"url": "https://example.com/synthetic.flv"}},
    {"h264": {"adaptationSet": {"representation": []}}},
    [{"url": "malformed"}], [{"url": 123}],
])
def test_kuaishou_malformed_play_urls_never_confirm_live(monkeypatch, play_urls):
    state = {"room": {"author": {"id": "synthetic_user"},
                       "liveStream": {"playUrls": play_urls}}}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == "unknown"


def test_kuaishou_structured_play_url_and_explicit_offline_placeholders(monkeypatch):
    room = {"author": {"id": "synthetic_user"}, "liveStream": {
        "playUrls": {"h264": {"adaptationSet": {"representation": [
            {"url": "https://example.com/synthetic.flv"},
        ]}}},
    }}
    offline = {"author": {"id": "synthetic_user", "living": False},
               "isLiving": False, "liveStream": {"playUrls": {"h264": {}, "hevc": {}}}}
    adapter, _session = build(monkeypatch, "kuaishou", [
        FakeResponse(text=kuaishou_page({"liveroom": room})),
        FakeResponse(text=kuaishou_page({"liveroom": offline})),
    ])
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == "live"
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == "offline"


def test_kuaishou_author_metadata_without_room_evidence_is_unknown(monkeypatch):
    state = {"room": {"author": {"id": "synthetic_user", "living": False}}}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == "unknown"


def test_kuaishou_current_room_stream_overrides_author_metadata(monkeypatch):
    state = {"liveroom": {
        "author": {"principalId": "synthetic_user", "name": "示例主播", "living": False},
        "isLiving": True,
        "liveStream": {"id": "synthetic_session", "playUrls": {
            "h264": {"adaptationSet": {"representation": [
                {"url": "https://example.com/synthetic.flv"},
            ]}},
        }},
    }}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    result = adapter.check("https://live.kuaishou.com/u/synthetic_user")
    assert result["status"] == "live"
    assert result["session_id"] == "synthetic_session"


@pytest.mark.parametrize("room_flags,stream_flags,expected", [
    ({"living": True, "isLiving": False}, {}, "unknown"),
    ({"living": True}, {"isLiving": False}, "unknown"),
    ({"living": False}, {"isLiving": True}, "unknown"),
    ({}, {"living": False}, "offline"),
    ({"living": True}, {}, "unknown"),
    ({"living": 0}, {}, "unknown"),
    ({"isLiving": "false"}, {}, "unknown"),
])
def test_kuaishou_room_flags_are_strict_and_conflicts_remain_unknown(
    monkeypatch, room_flags, stream_flags, expected,
):
    state = {"room": {"author": {"id": "synthetic_user", "living": False},
                       "liveStream": stream_flags, **room_flags}}
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=kuaishou_page(state))])
    assert adapter.check("https://live.kuaishou.com/u/synthetic_user")["status"] == expected


def test_kuaishou_undefined_token_preserves_strings_and_is_unknown(monkeypatch):
    page = '<script>window.__INITIAL_STATE__={"liveroom":{"liveStream":undefined,' \
           '"author":{"name":"undefined","principalId":"synthetic_user"}}};</script>'
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=page)])
    result = adapter.check("https://live.kuaishou.com/u/synthetic_user")
    assert result["status"] == "unknown"
    assert result["error_kind"] == "restricted"
    adapter, _session = build(monkeypatch, "kuaishou", [FakeResponse(text=page)])
    state = adapter._state("https://live.kuaishou.com/u/synthetic_user", adapter._monotonic() + 15)
    assert state["author"]["name"] == "undefined"


@pytest.mark.parametrize("response,kind", [
    (FakeResponse(status=429, headers={"Retry-After": "120"}), "rate_limited"),
    (FakeResponse(status=403), "restricted"), (FakeResponse(status=500), "network"),
    (FakeResponse(text=""), "parse"), (FakeResponse(text="<html>invalid page</html>"), "parse"),
    (FakeResponse(text="<title>安全验证</title>"), "restricted"),
    (requests.Timeout("synthetic request URL must not leak"), "network"),
])
def test_http_and_network_failures_are_unknown_and_release_response(monkeypatch, response, kind):
    adapter, _session = build(monkeypatch, "douyu", [response])
    result = adapter.check("https://www.douyu.com/100000001")
    assert result["status"] == "unknown"
    assert result["error_kind"] == kind
    assert "synthetic request URL" not in result["error"]
    if isinstance(response, FakeResponse):
        assert response.closed
    if kind == "rate_limited":
        assert result["cooldown"] == 120


def test_prepare_rate_limit_is_retryable_error_with_cooldown(monkeypatch):
    adapter, _session = build(monkeypatch, "huya", [FakeResponse(status=429)])
    with pytest.raises(clients.PlatformAccessError) as error:
        adapter.resolve("https://www.huya.com/100000001")
    assert error.value.error_kind == "rate_limited"
    assert error.value.cooldown == 60


@pytest.mark.parametrize("location", [
    "http://www.douyu.com/100000001", "https://127.0.0.1/",
    "https://www.example.com/", "https://www.douyu.com:8443/",
    "https://user:password@www.douyu.com/",
])
def test_untrusted_redirect_is_rejected_before_second_request(monkeypatch, location):
    response = FakeResponse(status=302, headers={"Location": location})
    adapter, session = build(monkeypatch, "douyu", [response])
    result = adapter.check("https://www.douyu.com/100000001")
    assert result["status"] == "unknown"
    assert result["error_kind"] == "invalid_target"
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert response.closed


def test_relative_redirect_followed_safely_and_login_redirect_restricted(monkeypatch):
    first = FakeResponse(status=302, headers={"Location": "/betard/100000001?canonical=1"})
    final = FakeResponse({"room": {"room_id": 100000001, "show_status": 2, "videoLoop": 0}})
    adapter, session = build(monkeypatch, "douyu", [first, final])
    assert adapter.check("https://www.douyu.com/100000001")["status"] == "offline"
    assert len(session.calls) == 2
    assert first.closed and final.closed
    session.responses = [FakeResponse(status=302, headers={"Location": "/login"})]
    assert adapter.check("https://www.douyu.com/100000001")["error_kind"] == "restricted"


def test_private_dns_target_rejected_without_connecting(monkeypatch):
    adapter, session = build(monkeypatch, "douyu", [])
    monkeypatch.setattr(clients.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("127.0.0.1", 443)),
    ])
    assert adapter.check("https://www.douyu.com/100000001")["error_kind"] == "invalid_target"
    assert not session.calls


def test_response_size_and_deadline_bounded_and_responses_closed(monkeypatch):
    large = FakeResponse(chunks=[b"x" * (clients.MAX_RESPONSE_BYTES + 1)])
    adapter, session = build(monkeypatch, "douyu", [large])
    assert adapter.check("https://www.douyu.com/100000001")["status"] == "unknown"
    assert large.closed
    clock = [0.0]
    adapter._monotonic = lambda: clock[0]

    def slow_chunks():
        clock[0] += clients.CHECK_BUDGET_SECONDS + 1
        yield b"{}"

    slow = FakeResponse(chunks=slow_chunks())
    session.responses = [slow]
    assert adapter.check("https://www.douyu.com/100000001")["error_kind"] == "network"
    assert slow.closed


def test_douyin_wrapper_preserves_existing_calls_and_room_session(monkeypatch):
    calls = []

    class FakeDouyin:
        def __init__(self, debug=False):
            self.closed = False

        def resolve_short_link(self, url):
            calls.append(("resolve", url))
            return "https://www.douyin.com/user/synthetic_user"

        def extract_sec_uid(self, url):
            calls.append(("extract", url))
            return "synthetic_user"

        def get_user_info(self, sec_uid):
            calls.append(("info", sec_uid))
            return {"nickname": "示例主播"}

        def check_live(self, target_url=None):
            calls.append(("check", target_url))
            return {"is_live": True, "room_id": "synthetic_session", "method": "api"}

        def close(self):
            self.closed = True

    monkeypatch.setattr(clients, "DouyinClient", FakeDouyin)
    adapter = clients.create_platform_client("douyin")
    prepared = adapter.resolve("https://v.douyin.com/synthetic/")
    assert prepared["target_id"] == "synthetic_user"
    result = adapter.check(prepared["url"])
    assert result["status"] == "live"
    assert result["session_id"] == "synthetic_session"
    assert result["live_url"] == "https://www.douyin.com/user/synthetic_user"
    assert [kind for kind, _value in calls] == ["resolve", "extract", "info", "check"]
    adapter.close()
    assert adapter.client.closed


class FakeDouyinIdentity:
    def __init__(self, debug=False):
        self.results = []
        self.room_calls = []

    def resolve_short_link(self, url):
        if url == "https://v.douyin.com/synthetic/":
            return "https://live.douyin.com/100000001"
        return url

    def resolve_live_room_url(self, url):
        self.room_calls.append(url)
        return {"sec_uid": "synthetic_user", "nickname": "示例主播", "web_rid": "100000001",
                "url": "https://www.douyin.com/user/synthetic_user"}

    def extract_sec_uid(self, url):
        return url.rstrip("/").rsplit("/", 1)[-1]

    def get_user_info(self, sec_uid):
        return {"nickname": "示例主播"}

    def check_live(self, target_url=None):
        return self.results.pop(0)

    def close(self):
        pass


@pytest.mark.parametrize("url", [
    "https://live.douyin.com/100000001", "https://v.douyin.com/synthetic/",
])
def test_douyin_live_room_resolves_profile_and_keeps_session_separate(monkeypatch, url):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    direct = adapter.resolve(url)
    profile = adapter.resolve("https://www.douyin.com/user/synthetic_user")
    assert direct == profile
    assert adapter.client.room_calls == ["https://live.douyin.com/100000001"]
    adapter.client.results = [
        {"is_live": True, "room_id": "synthetic_session_one"},
        {"is_live": False, "room_id": ""},
        {"is_live": True, "room_id": "synthetic_session_two"},
    ]
    first, offline, second = [adapter.check(direct["url"]) for _ in range(3)]
    assert first["session_id"] == "synthetic_session_one"
    assert offline["status"] == "offline"
    assert second["session_id"] == "synthetic_session_two"
    assert {result["live_url"] for result in (first, offline, second)} == {
        "https://live.douyin.com/100000001",
    }


@pytest.mark.parametrize("web_rid,expected_url", [
    ("100000002", "https://live.douyin.com/100000002"),
    (100000002, "https://live.douyin.com/100000002"),
    ("synthetic_session", "https://www.douyin.com/user/synthetic_user"),
    (True, "https://www.douyin.com/user/synthetic_user"),
    ("0", "https://www.douyin.com/user/synthetic_user"),
    (None, "https://www.douyin.com/user/synthetic_user"),
])
def test_douyin_watch_link_requires_valid_web_room_id(monkeypatch, web_rid, expected_url):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    adapter.client.results = [{"is_live": True, "room_id": "100000099", "web_rid": web_rid}]
    result = adapter.check("https://www.douyin.com/user/synthetic_user?from=example")
    assert result["live_url"] == expected_url
    assert result["room_id"] == result["session_id"] == "100000099"


def test_douyin_bound_web_room_does_not_cross_target_identity(monkeypatch):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    adapter.resolve("https://live.douyin.com/100000001")
    adapter.client.results = [{"is_live": True, "room_id": "synthetic_session"}]
    result = adapter.check("https://www.douyin.com/user/another_synthetic_user")
    assert result["live_url"] == "https://www.douyin.com/user/another_synthetic_user"


def test_douyin_new_web_room_metadata_replaces_only_bound_room(monkeypatch):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    prepared = adapter.resolve("https://live.douyin.com/100000001")
    adapter.client.results = [
        {"is_live": True, "room_id": "synthetic_session", "web_rid": "100000002"},
        {"is_live": False},
    ]
    assert adapter.check(prepared["url"])["live_url"] == "https://live.douyin.com/100000002"
    assert adapter.check(prepared["url"])["live_url"] == "https://live.douyin.com/100000002"


@pytest.mark.parametrize("url", [
    "https://www.iesdouyin.com/share/user/synthetic_user",
    "https://webcast.amemv.com/douyin/webcast/reflow/100000099?sec_uid=synthetic_user",
    "https://webcast.amemv.com/reflow/100000099?sec_user_id=synthetic_user",
])
def test_douyin_share_watch_fallback_uses_canonical_profile(monkeypatch, url):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    adapter.client.results = [{"is_live": True, "room_id": "100000099"}]
    assert adapter.check(url)["live_url"] == "https://www.douyin.com/user/synthetic_user"


def test_douyin_unresolved_watch_target_omits_link(monkeypatch):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    adapter.client.results = [{"is_live": True, "room_id": "100000099"}]
    result = adapter.check("https://v.douyin.com/synthetic/")
    assert result["live_url"] == ""


@pytest.mark.parametrize("kind,cooldown", [("restricted", 0), ("rate_limited", 90), ("network", 0)])
@pytest.mark.parametrize("confirmed_offline", [False, True])
def test_douyin_adapter_keeps_access_error_category_and_platform_cooldown(
    monkeypatch, kind, cooldown, confirmed_offline,
):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    adapter.client.results = [{"is_live": False, "determined": confirmed_offline,
                               "indeterminate": not confirmed_offline, "error_kind": kind,
                               "cooldown": cooldown, "error": "" if confirmed_offline else "访问受限"}]
    result = adapter.check("https://www.douyin.com/user/synthetic_user")
    assert result["status"] == ("offline" if confirmed_offline else "unknown")
    assert result["error_kind"] == kind
    assert result["cooldown"] == cooldown


def test_direct_douyin_room_uses_bounded_resolver_without_short_link_head(monkeypatch):
    monkeypatch.setattr(clients, "DouyinClient", FakeDouyinIdentity)
    adapter = clients.create_platform_client("douyin")
    adapter.client.resolve_short_link = Mock(side_effect=AssertionError("unexpected HEAD resolution"))
    prepared = adapter.resolve("https://live.douyin.com/100000001")
    assert prepared["target_id"] == "synthetic_user"
    assert prepared["url"] == "https://www.douyin.com/user/synthetic_user"
    assert adapter.client.room_calls == ["https://live.douyin.com/100000001"]
    adapter.client.resolve_short_link.assert_not_called()
