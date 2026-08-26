from __future__ import annotations

from typing import Any

import requests
import pytest

import douyin_client
from douyin_client import DouyinClient, compact_log_preview


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        payload: Any = None,
        url: str = "https://www.douyin.com/",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.url = url
        self.headers = headers or {}
        self.is_redirect = status_code in {301, 302, 303, 307, 308}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise requests.JSONDecodeError("empty response", self.text, 0)
        return self._payload


class FakeSession:
    def __init__(self, api_responses):
        self.headers = {}
        self.proxies = {}
        self.trust_env = True
        self.cookies = requests.cookies.RequestsCookieJar()
        self.api_responses = list(api_responses)
        self.closed = False
        self.mounts = []

    def mount(self, prefix, adapter) -> None:
        self.mounts.append((prefix, adapter))

    def close(self) -> None:
        self.closed = True

    def get(self, url, **_kwargs):
        if url == "https://live.douyin.com":
            self.cookies.set("ttwid", "fresh-cookie", domain=".douyin.com")
            return FakeResponse(url=url, text="ok")
        if url.endswith("/aweme/v1/web/user/profile/other/"):
            return self.api_responses.pop(0)
        return FakeResponse(url=url, text="ok")


def build_client_with_stale_session(monkeypatch, replacement_session):
    client = DouyinClient()
    stale_session = FakeSession([FakeResponse(text="")])
    stale_session.cookies.set("ttwid", "stale-cookie", domain=".douyin.com")
    client.session.close()
    client.session = stale_session
    client._cookies_initialized = True
    monkeypatch.setattr(
        douyin_client.requests,
        "Session",
        lambda: replacement_session,
    )
    monkeypatch.setattr(
        client,
        "_generate_a_bogus",
        lambda _params: {"a_bogus": "signed", "msToken": "token"},
    )
    return client, stale_session


def test_empty_profile_api_response_rebuilds_session_and_recovers(monkeypatch):
    replacement = FakeSession(
        [
            FakeResponse(
                payload={
                    "status_code": 0,
                    "user": {
                        "nickname": "恢复主播",
                        "live_status": 1,
                        "room_id": "123456",
                    },
                }
            )
        ]
    )
    client, stale_session = build_client_with_stale_session(
        monkeypatch, replacement
    )

    result = client.check_live_by_api("sec-test")

    assert result["is_live"] is True
    assert result["nickname"] == "恢复主播"
    assert result["determined"] is True
    assert client.session is replacement
    assert stale_session.closed is True


def test_repeated_empty_profile_api_response_stays_indeterminate(monkeypatch):
    replacement = FakeSession([FakeResponse(text="")])
    client, _stale_session = build_client_with_stale_session(
        monkeypatch, replacement
    )

    result = client.check_live_by_api("sec-test")

    assert result["is_live"] is False
    assert result["determined"] is False
    assert result["error"] == "profile_api_empty_response"


def test_log_preview_is_single_line_and_bounded():
    preview = compact_log_preview("<style>\n  color: red;\n</style>", limit=20)

    assert preview == "<style> color: red;"
    assert "\n" not in preview
    assert len(preview) <= 20


def test_unavailable_primary_api_does_not_become_confirmed_offline(monkeypatch):
    client = DouyinClient()
    unavailable = {
        "is_live": False,
        "room_id": "",
        "title": "",
        "nickname": "",
        "method": "api",
        "determined": False,
        "error": "profile_api_empty_response",
    }

    monkeypatch.setattr(client, "_ensure_cookies", lambda: True)
    monkeypatch.setattr(client, "check_live_by_api", lambda _uid: unavailable)

    def offline(method):
        return {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": method,
        }

    monkeypatch.setattr(
        client,
        "check_live_by_webcast_info_by_user",
        lambda _uid: offline("webcast_info_by_user"),
    )
    monkeypatch.setattr(
        client,
        "check_live_by_profile_live_link",
        lambda _uid: offline("profile_live_link"),
    )
    monkeypatch.setattr(
        client, "check_live_by_html", lambda _uid: offline("html")
    )
    monkeypatch.setattr(
        client,
        "check_live_by_iesdouyin_api",
        lambda _uid: offline("ies_api"),
    )
    monkeypatch.setattr(
        client,
        "check_live_by_webcast_api",
        lambda _uid: offline("webcast_api"),
    )
    monkeypatch.setattr(
        client,
        "check_live_by_iesdouyin_share_page",
        lambda _uid: offline("ies_share"),
    )

    result = client.check_live(sec_uid="sec-test")

    assert result["is_live"] is False
    assert result["indeterminate"] is True
    assert result["error"] == "profile_api_empty_response"


def test_unverified_share_link_is_indeterminate(monkeypatch):
    client = DouyinClient()
    monkeypatch.setattr(client, "_ensure_cookies", lambda: True)
    def resolve(_url):
        client._cached_room_id = "123"
        return "https://webcast.amemv.com/reflow/123"

    monkeypatch.setattr(client, "resolve_short_link", resolve)
    monkeypatch.setattr(client, "extract_sec_uid", lambda _url: "sec-test")
    monkeypatch.setattr(
        client,
        "check_live_by_iesdouyin_api",
        lambda _uid: {"is_live": False, "nickname": ""},
    )

    result = client.check_live(target_url="https://v.douyin.com/a/")

    assert result["is_live"] is False
    assert result["indeterminate"] is True
    assert result["method"] == "link_unverified"


def test_redirect_rejects_private_or_untrusted_targets(monkeypatch):
    monkeypatch.setattr(
        douyin_client.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="非公网"):
        DouyinClient._validate_redirect_target("https://v.douyin.com/a/")
    with pytest.raises(ValueError, match="不受信任"):
        DouyinClient._validate_redirect_target("https://example.com/a/")


def test_client_close_releases_session():
    client = DouyinClient()
    session = client.session

    client.close()

    assert client.session is None
    assert session is not None


def _offline(method):
    return {
        "is_live": False,
        "room_id": "",
        "title": "",
        "nickname": "主播",
        "method": method,
        "determined": True,
    }


def _configure_detection_stubs(monkeypatch, client, calls):
    monkeypatch.setattr(client, "_ensure_cookies", lambda: True)
    for name, method in (
        ("check_live_by_api", "api"),
        ("check_live_by_webcast_info_by_user", "webcast_info_by_user"),
        ("check_live_by_profile_live_link", "profile_live_link"),
        ("check_live_by_html", "html"),
        ("check_live_by_iesdouyin_api", "ies_api"),
        ("check_live_by_webcast_api", "webcast_api"),
        ("check_live_by_iesdouyin_share_page", "ies_share"),
    ):
        monkeypatch.setattr(
            client,
            name,
            lambda _uid, method=method: calls.append(method) or _offline(method),
        )


def test_dual_offline_short_circuits_ordinary_poll(monkeypatch):
    client = DouyinClient()
    calls = []
    _configure_detection_stubs(monkeypatch, client, calls)
    client._last_full_check_at = 100.0
    client._monotonic = lambda: 101.0

    result = client.check_live(sec_uid="sec-test")

    assert result["method"] == "dual_source"
    assert calls == ["api", "webcast_info_by_user"]


def test_every_tenth_dual_offline_poll_runs_full_chain(monkeypatch):
    client = DouyinClient()
    calls = []
    _configure_detection_stubs(monkeypatch, client, calls)
    client._last_full_check_at = 100.0
    client._monotonic = lambda: 101.0

    for _ in range(10):
        result = client.check_live(sec_uid="sec-test")

    assert result["method"] == "full_check"
    assert calls.count("profile_live_link") == 1
    assert calls.count("api") == 10


def test_full_chain_budget_stops_launching_later_strategies(monkeypatch):
    client = DouyinClient()
    calls = []
    _configure_detection_stubs(monkeypatch, client, calls)
    monkeypatch.setattr(
        client,
        "check_live_by_api",
        lambda _uid: calls.append("api") or {
            **_offline("api"), "determined": False, "error": "unavailable"
        },
    )
    ticks = iter([0.0, 0.0, 0.0, 46.0])
    client._monotonic = lambda: next(ticks)

    result = client.check_live(sec_uid="sec-test")

    assert result["indeterminate"] is True
    assert result["error"] == "full_check_budget_exhausted"
    assert calls == ["api", "webcast_info_by_user", "profile_live_link"]


def test_profile_page_is_fetched_once_and_captcha_is_not_decisive(monkeypatch):
    client = DouyinClient()
    response = FakeResponse(
        text=("直播中 " * 1000),
        url="https://www.douyin.com/verify/captcha",
    )
    client.session.get = lambda *_args, **_kwargs: response

    profile = client.check_live_by_profile_live_link("sec-test")
    html = client.check_live_by_html("sec-test")

    assert profile["is_live"] is False
    assert html["is_live"] is False
    assert profile["determined"] is False
    assert len(client._profile_html_cache) == 1


@pytest.mark.parametrize(
    "body",
    [
        "<script id=\"RENDER_DATA\">{broken</script>" + "x" * 6000,
        "live.douyin.com/111 live.douyin.com/222 " + "x" * 6000,
    ],
)
def test_malformed_or_multi_room_profile_is_not_decisive(body):
    client = DouyinClient()
    response = FakeResponse(text=body, url="https://www.douyin.com/user/test")
    client.session.get = lambda *_args, **_kwargs: response

    assert client.check_live_by_profile_live_link("test")["is_live"] is False
    assert client.check_live_by_html("test")["is_live"] is False
