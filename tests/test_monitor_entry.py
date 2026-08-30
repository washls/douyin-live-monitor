import sys
from unittest.mock import Mock

import pytest

import monitor
from douyin_monitor import application, windows_integration


def cli_config():
    return {
        **monitor._default_config(),
        "push_url": "https://12345.push.ft07.com/send/example-key.send",
        "streamers": [
            {
                "id": "one",
                "url": "https://www.douyin.com/user/test",
                "label": "测试主播",
                "enabled": True,
            }
        ],
    }


def prepare_cli(monkeypatch, arguments):
    config = cli_config()
    monkeypatch.setattr(sys, "argv", ["monitor.py", *arguments])
    monkeypatch.setattr(monitor, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(monitor, "load_config", lambda _path: config)
    monkeypatch.setattr(monitor, "load_monitor_state", lambda: {})
    monkeypatch.setattr(
        application,
        "load_streamer_entries",
        lambda _path, loaded, _state: list(loaded["streamers"]),
    )
    monkeypatch.setattr(monitor, "is_serverchan_configured", lambda _config: True)
    return config


@pytest.mark.parametrize("arguments", [[], ["--once"]])
def test_monitoring_rejects_a_second_monitor_instance(monkeypatch, arguments):
    prepare_cli(monkeypatch, arguments)
    load_config = Mock(side_effect=AssertionError("config must not be loaded"))
    monkeypatch.setattr(monitor, "load_config", load_config)
    monkeypatch.setattr(
        windows_integration.WindowsInstanceGuard,
        "acquire_monitor",
        lambda: None,
    )

    with pytest.raises(SystemExit) as exc:
        monitor.main()

    assert exc.value.code == 3
    load_config.assert_not_called()


def test_once_releases_the_monitor_lock(monkeypatch):
    prepare_cli(monkeypatch, ["--once"])
    guard = Mock()
    service = Mock()
    service.check_all_once.return_value = {
        "one": {
            "nickname": "测试主播",
            "is_live": False,
            "method": "fake",
        }
    }
    monkeypatch.setattr(
        windows_integration.WindowsInstanceGuard,
        "acquire_monitor",
        lambda: guard,
    )
    monkeypatch.setattr(application, "create_monitor_service", lambda *args, **kwargs: service)

    monitor.main()

    service.check_all_once.assert_called_once_with()
    service.close.assert_called_once_with()
    guard.close.assert_called_once_with()


def test_configuration_commands_do_not_acquire_the_monitor_lock(monkeypatch, capsys):
    prepare_cli(monkeypatch, ["--list-streamers"])

    def unexpected_lock():
        raise AssertionError("configuration command acquired the monitor lock")

    monkeypatch.setattr(
        windows_integration.WindowsInstanceGuard,
        "acquire_monitor",
        unexpected_lock,
    )

    monitor.main()

    assert "测试主播" in capsys.readouterr().out
