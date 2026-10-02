"""Cross-platform notification and configuration compatibility regressions."""

import json
from unittest.mock import Mock

import pytest

from douyin_monitor.application import create_monitor_worker
from douyin_monitor.monitor_service import LiveStatusUnknownError
from douyin_monitor.notifier import ServerChanNotifier
from douyin_monitor.streamer_config import add_streamer, normalize_app_config, save_config_atomic
from monitor import MonitorState


class Adapter:
    def __init__(self, results):
        self.results = iter(results)
        self.closed = False

    def resolve(self, url):
        return {"platform": "bilibili", "target_id": "123", "nickname": "示例主播", "url": url}

    def check(self, url):
        return next(self.results)

    def close(self):
        self.closed = True


def result(live=True, **values):
    return {"platform": "bilibili", "is_live": live, "nickname": "示例主播", "room_id": "123",
            "live_url": "https://live.bilibili.com/123", "session_id": "session-a", **values}


def test_session_changes_notify_but_missing_session_does_not():
    state = MonitorState(clock=lambda: 100)
    assert state.transition(True, result()) == "went_live"
    state.mark_first_notification_sent()
    assert state.transition(True, result(session_id="")) == "still_live"
    assert state.transition(True, result(room_id="456")) == "still_live"
    assert state.transition(True, result(session_id="session-b")) == "went_live"
    assert state.repeat_notify_count == 0
    state.transition(False, result(False))
    assert state.transition(True, result(session_id="session-b")) == "went_live"


def test_unknown_keeps_state_and_notification_attempt_is_not_replayed():
    adapter = Adapter([result(), result(False, indeterminate=True, error_kind="restricted", error="访问受限"), result()])
    notifier = Mock(delivery_unknown=False)
    notifier.send_live_notification.return_value = False
    worker = create_monitor_worker({"enable_daily_intimacy_reminder": False},
                                   {"url": "https://live.bilibili.com/123", "platform": "bilibili"},
                                   client=adapter, notifier=notifier)
    assert worker.prepare().identity == ("bilibili", "123")
    worker.check_once()
    with pytest.raises(LiveStatusUnknownError) as caught:
        worker.check_once()
    assert caught.value.error_kind == "restricted"
    assert worker.state.status == "live"
    worker.check_once()
    notifier.send_live_notification.assert_called_once()
    assert notifier.send_live_notification.call_args.kwargs["live_url"] == "https://live.bilibili.com/123"
    assert notifier.send_live_notification.call_args.kwargs["platform"] == "bilibili"
    notifier.send_stream_end_notification.assert_not_called()
    notifier.send_daily_intimacy_reminder.assert_not_called()
    worker.close()
    assert adapter.closed


@pytest.mark.parametrize("kind", ["live", "repeat", "end", "alert", "recovery"])
def test_all_notification_types_include_platform_and_correct_links(kind):
    notifier = ServerChanNotifier(sendkey="example-key", uid="example-uid")
    notifier.send = Mock(return_value=True)
    common = {"nickname": "示例主播", "platform": "huya", "live_url": "https://www.huya.com/123"}
    if kind == "live":
        notifier.send_live_notification(room_id="123", **common)
    elif kind == "repeat":
        notifier.send_repeat_live_notification(room_id="123", **common)
    elif kind == "end":
        notifier.send_stream_end_notification(**common)
    elif kind == "alert":
        notifier.send_monitor_alert(reason="访问受限", duration=300, retrying=True, **common)
    else:
        notifier.send_monitor_recovery(duration=300, **common)
    payload = notifier.send.call_args.kwargs
    assert "虎牙" in payload["title"]
    assert "虎牙" in payload["desp"]
    assert "live.douyin.com" not in payload["desp"]
    assert "https://www.huya.com/123" in payload["desp"]


def test_config_upgrade_preserves_original_backup_once(tmp_path):
    path = tmp_path / "custom.json"
    old = {"push_url": "", "streamers": [{"id": "one", "url": "https://www.douyin.com/user/example"}]}
    original = json.dumps(old, ensure_ascii=False).encode()
    path.write_bytes(original)
    upgraded = normalize_app_config(old)
    assert upgraded["streamers"][0]["platform"] == "douyin"
    save_config_atomic(path, upgraded)
    backup = path.with_name("custom.json.pre-v1.8.0.bak")
    assert backup.read_bytes() == original
    add_streamer(upgraded, "https://live.bilibili.com/123")
    save_config_atomic(path, upgraded)
    assert backup.read_bytes() == original
    assert json.loads(path.read_text())["streamers"][1]["platform"] == "bilibili"


def test_config_platform_mismatch_rejected():
    with pytest.raises(ValueError):
        normalize_app_config({"streamers": [{"id": "one", "url": "https://live.bilibili.com/123", "platform": "huya"}]})
    with pytest.raises(ValueError):
        normalize_app_config({"streamers": [{"id": "one", "url": "https://live.bilibili.com/123"}]})


def test_failed_backup_is_never_installed_or_overwrites_original(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    old = {"streamers": [{"id": "one", "url": "https://www.douyin.com/user/example"}]}
    original = json.dumps(old).encode()
    path.write_bytes(original)
    upgraded = normalize_app_config(old)
    with monkeypatch.context() as patch:
        patch.setattr("douyin_monitor.streamer_config.os.fsync", Mock(side_effect=OSError("disk full")))
        with pytest.raises(OSError):
            save_config_atomic(path, upgraded)
    backup = path.with_name("config.json.pre-v1.8.0.bak")
    assert path.read_bytes() == original
    assert not backup.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    save_config_atomic(path, upgraded)
    assert backup.read_bytes() == original
