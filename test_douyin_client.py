from __future__ import annotations

from typing import Any

import requests

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
    ):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.url = url

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
