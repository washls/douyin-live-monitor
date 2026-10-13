"""Read-only notification commands and shared monitoring delivery ownership."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import monitor
from douyin_monitor import application, windows_integration


def setup_cli(monkeypatch, path, *arguments):
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--config", str(path), *arguments])
    monkeypatch.setattr(monitor, "setup_logging", lambda: None)
    monkeypatch.setattr(monitor, "load_monitor_state", Mock(side_effect=AssertionError("state read")))
    monkeypatch.setattr(windows_integration.WindowsInstanceGuard, "acquire_monitor",
                        Mock(side_effect=AssertionError("monitor lock")))


def push_config():
    return {
        "push_mode": "selected", "selected_channel_ids": ["one"],
        "channels": [
            {"id": "one", "name": "目标一", "type": "webhook", "config": {"url": "https://example.invalid/one"}},
            {"id": "two", "name": "目标二", "type": "webhook", "config": {"url": "https://example.invalid/two"}},
        ],
    }


def test_channel_listing_does_not_persist_legacy_migration(monkeypatch, tmp_path, capsys):
    path = tmp_path / "legacy.json"
    original = b'{"push_url":"https://example.push.ft07.com/send/YOUR_SENDKEY.send"}'
    path.write_bytes(original)
    setup_cli(monkeypatch, path, "--list-channels")
    monitor.main()
    assert "legacy_serverchan" in capsys.readouterr().out
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_listing_missing_config_does_not_create_file(monkeypatch, tmp_path):
    path = tmp_path / "missing.json"
    setup_cli(monkeypatch, path, "--list-channels")
    monitor.main()
    assert not path.exists()


@pytest.mark.parametrize("argument,expected", [("--test", "one"), ("--test-channel", "two")])
def test_push_test_uses_snapshot_and_closes_resources(monkeypatch, tmp_path, argument, expected):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(push_config()), encoding="utf-8")
    original = path.read_bytes()
    setup_cli(monkeypatch, path, argument, *(["two"] if argument == "--test-channel" else []))
    notifier = SimpleNamespace(verify_connection=Mock(return_value=True), close=Mock(),
                               last_report=SimpleNamespace(summary="已受理 1/1"))
    captured = []

    def create(config):
        from douyin_monitor.push_config import selected_channels
        captured.extend(item["id"] for item in selected_channels(config))
        return notifier

    monkeypatch.setattr(application, "create_notifier", create)
    monitor.main()
    assert captured == [expected]
    notifier.close.assert_called_once()
    assert path.read_bytes() == original


@pytest.mark.parametrize("success,code", [(False, 1), (True, 0)])
def test_push_test_exit_codes(monkeypatch, tmp_path, success, code):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(push_config()), encoding="utf-8")
    setup_cli(monkeypatch, path, "--test")
    notifier = SimpleNamespace(verify_connection=lambda: success, close=Mock(),
                               last_report=SimpleNamespace(summary="测试结果"))
    monkeypatch.setattr(application, "create_notifier", lambda config: notifier)
    if code:
        with pytest.raises(SystemExit) as caught:
            monitor.main()
        assert caught.value.code == code
    else:
        monitor.main()
    notifier.close.assert_called_once()


def test_unknown_channel_id_is_parameter_error(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(push_config()), encoding="utf-8")
    setup_cli(monkeypatch, path, "--test-channel", "missing")
    with pytest.raises(SystemExit) as caught:
        monitor.main()
    assert caught.value.code == 2


def test_empty_test_range_fails_without_post(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**push_config(), "selected_channel_ids": []}), encoding="utf-8")
    setup_cli(monkeypatch, path, "--test")
    post = Mock(side_effect=AssertionError("empty range must not send"))
    monkeypatch.setattr("requests.post", post)
    with pytest.raises(SystemExit) as caught:
        monitor.main()
    assert caught.value.code == 1
    post.assert_not_called()


def test_shared_service_reports_partial_results_and_owns_pool(monkeypatch):
    from douyin_monitor.push_channels import ChannelResult, DeliveryReport

    events = []
    service = application.create_monitor_service(push_config(), [], on_event=events.append)
    report = DeliveryReport("event", (
        ChannelResult("one", "目标一", "webhook", "accepted"),
        ChannelResult("two", "目标二", "webhook", "unknown", error_code="timeout"),
    ))
    try:
        service.record_delivery("", report)
        safe = events[-1]["delivery_report"]
        assert "结果未知 1" in safe["summary"]
        assert safe["results"][1]["error_code"] == "timeout"
        assert "config" not in safe["results"][0]
    finally:
        service.close()
        service.close()
