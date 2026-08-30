from dataclasses import FrozenInstanceError

import pytest

from douyin_monitor.application import create_monitor_service, create_monitor_worker
from monitor import MonitorConfigurationError
from douyin_monitor.monitor_types import (
    MonitorEvent,
    MonitorEventType,
    StreamerSnapshot,
    StreamerStatus,
)


class FakeClient:
    def close(self):
        pass


class FakeNotifier:
    delivery_unknown = False


def config():
    return {
        "check_interval": "30",
        "repeat_notify_interval": "600",
        "max_repeat_notifications": "3",
        "max_concurrent_checks": "2",
        "notify_on_stream_end": True,
        "startup_notify": False,
        "enable_daily_intimacy_reminder": True,
        "streamers": [],
    }


def test_worker_accepts_injected_dependencies_and_clock():
    client = FakeClient()
    notifier = FakeNotifier()
    worker = create_monitor_worker(
        config(),
        {"url": "https://www.douyin.com/user/test"},
        client=client,
        notifier=notifier,
        clock=lambda: 123.0,
    )

    assert worker.client is client
    assert worker.notifier is notifier
    assert worker.state.last_status_change == 123.0


def test_missing_target_raises_domain_error():
    with pytest.raises(MonitorConfigurationError, match="未设置目标"):
        create_monitor_worker(config(), {"url": ""})


def test_assembly_normalizes_config_and_disables_console_listener_by_default():
    service = create_monitor_service(config(), [])

    assert service.check_interval == 30
    assert service.max_concurrent_checks == 2
    assert service.enable_console_stop is False


def test_public_event_and_snapshot_are_typed_and_immutable():
    event = MonitorEvent.create("prepared", "one", nickname="主播")
    assert event.type is MonitorEventType.PREPARED
    with pytest.raises(TypeError):
        event.data["nickname"] = "changed"

    snapshot = StreamerSnapshot(
        id="one", url="https://www.douyin.com/user/one", label="",
        nickname="", status=StreamerStatus.PENDING, method="", room_id="", title="",
        last_check="", last_error="", check_count=0, consecutive_errors=0,
        consecutive_unknowns=0, suspended=False, stopped_by_user=False,
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.nickname = "changed"
