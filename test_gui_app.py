import ctypes
import pytest
import tkinter as tk
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import gui_app
import monitor
from gui_app import MonitorGui, compact_ui_text, status_text, validate_gui_settings
from streamer_config import add_streamer, save_config_atomic


def test_high_dpi_prefers_per_monitor_v2(monkeypatch):
    set_context = Mock(return_value=True)
    user32 = SimpleNamespace(
        GetThreadDpiAwarenessContext=Mock(return_value=0),
        GetAwarenessFromDpiAwarenessContext=Mock(return_value=0),
        SetProcessDpiAwarenessContext=set_context,
    )
    monkeypatch.setattr(gui_app.sys, "platform", "win32")
    monkeypatch.setattr(
        gui_app.ctypes,
        "windll",
        SimpleNamespace(user32=user32),
        raising=False,
    )

    assert gui_app.enable_windows_high_dpi() is True
    assert set_context.call_args.args[0].value == ctypes.c_void_p(-4).value


def test_high_dpi_is_enabled_before_tk_window_creation(monkeypatch):
    calls = []

    class FakeApp:
        def show_window(self):
            calls.append("show")

    class FakeRoot:
        def withdraw(self):
            calls.append("withdraw")

        def update_idletasks(self):
            pass

        def winfo_width(self):
            return 1080

        def winfo_height(self):
            return 700

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def geometry(self, _value):
            pass

        def deiconify(self):
            pass

        def mainloop(self):
            pass

    monkeypatch.setattr(gui_app, "enable_windows_high_dpi", lambda: calls.append("dpi"))
    monkeypatch.setattr(gui_app.tk, "Tk", lambda: calls.append("tk") or FakeRoot())
    monkeypatch.setattr(gui_app, "MonitorGui", lambda *_args, **_kwargs: FakeApp())

    gui_app.run_gui()

    assert calls[:2] == ["dpi", "tk"]


@pytest.mark.parametrize(
    ("work_size", "scale", "expected_size", "compact"),
    [
        ((1920, 1040), 1.0, (1080, 700), False),
        ((2560, 1400), 1.5, (1620, 1050), False),
        ((3840, 2080), 2.0, (2160, 1400), False),
        ((1366, 728), 1.25, (1350, 728), True),
        ((1920, 1040), 2.0, (1920, 1040), True),
    ],
)
def test_window_metrics_scale_and_stay_inside_work_area(
    work_size, scale, expected_size, compact
):
    metrics = gui_app.calculate_window_metrics(*work_size, scale)

    assert (metrics["width"], metrics["height"]) == expected_size
    assert metrics["width"] <= work_size[0]
    assert metrics["height"] <= work_size[1]
    assert metrics["min_width"] <= metrics["width"]
    assert metrics["min_height"] <= metrics["height"]
    assert metrics["compact"] is compact


def test_window_metrics_clamp_invalid_scale_and_large_requested_size():
    metrics = gui_app.calculate_window_metrics(1280, 720, 20, 5000, 5000)

    assert metrics["width"] == 1280
    assert metrics["height"] == 720
    assert metrics["compact"] is True


def valid_settings():
    return {
        "push_url": "https://12345.push.ft07.com/send/example-key.send",
        "check_interval": "30",
        "repeat_notify_interval": "600",
        "max_repeat_notifications": "3",
        "max_concurrent_checks": "2",
        "notify_on_stream_end": True,
        "startup_notify": False,
        "enable_daily_intimacy_reminder": True,
    }


def test_gui_settings_are_normalized_without_exposing_legacy_credentials():
    settings = valid_settings()
    settings["sendkey"] = "legacy-key"
    settings["push_uid"] = "legacy-uid"

    normalized = validate_gui_settings(settings)

    assert normalized["check_interval"] == 30
    assert normalized["max_concurrent_checks"] == 2
    assert normalized["sendkey"] == ""
    assert normalized["push_uid"] == ""


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("check_interval", "0", "检测间隔"),
        ("repeat_notify_interval", "86401", "重复提醒间隔"),
        ("max_repeat_notifications", "101", "最多重复提醒"),
        ("max_concurrent_checks", "9", "并发检测数"),
    ],
)
def test_gui_settings_reject_out_of_range_values(key, value, message):
    settings = valid_settings()
    settings[key] = value

    with pytest.raises(ValueError, match=message):
        validate_gui_settings(settings)


def test_gui_settings_reject_invalid_push_url():
    settings = valid_settings()
    settings["push_url"] = "https://example.com/not-serverchan"

    with pytest.raises(ValueError, match="无法解析推送 URL"):
        validate_gui_settings(settings)


def test_status_text_has_a_safe_fallback():
    assert status_text("live") == "直播中"
    assert status_text("unexpected") == "未知"


def test_remote_text_is_single_line_and_bounded():
    assert compact_ui_text("主播\n名称  测试", 6) == "主播 名称"


def test_real_tk_window_builds_and_selects_first_streamer(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    config = monitor._default_config()
    first = add_streamer(
        config,
        "https://www.douyin.com/user/gui-test",
        label="界面测试主播",
    )
    config_path = tmp_path / "config.json"
    save_config_atomic(config_path, config)
    monkeypatch.setattr(
        monitor,
        "_load_streamer_entries",
        lambda _path, loaded: list(loaded["streamers"]),
    )
    monkeypatch.setattr("gui_app.messagebox.askyesno", lambda *args, **kwargs: True)
    finished = threading.Event()

    class FakeService:
        def __init__(self, streamers, on_event, **_kwargs):
            self.streamers = streamers
            self.on_event = on_event
            self.running = False

        def run(self):
            self.running = True
            self.on_event(
                {
                    "type": "status",
                    "streamer_id": first["id"],
                    "result": {"is_live": False},
                }
            )
            self.running = False
            finished.set()
            return True

        def stop(self):
            self.running = False

        def snapshot(self):
            return [
                {
                    "id": first["id"],
                    "status": "offline",
                    "nickname": "界面测试主播",
                    "last_check": "08-25 18:00",
                    "last_error": "",
                    "method": "fake",
                    "suspended": False,
                }
            ]

    monkeypatch.setattr("gui_app.MonitorService", FakeService)

    try:
        app = MonitorGui(root, config_path)
        root.update_idletasks()

        assert 0.75 <= app.display_scale <= 4.0
        assert app.selected_streamer_id == first["id"]
        assert app.streamer_label_var.get() == "界面测试主播"
        assert app.status_tree.item(first["id"], "values")[3] == "等待首次检测"
        assert app.settings_canvas.winfo_exists()
        app._start_monitoring()
        assert finished.wait(timeout=1)
        assert app.service_thread is not None
        app.service_thread.join(timeout=1)
        app._drain_events()

        assert app.service is None
        assert app.service_thread is None
        assert app.global_status_var.get() == "已停止"
        assert app.runtime_by_id[first["id"]]["status"] == "offline"
        assert app.status_tree.item(first["id"], "values") == (
            "未开播",
            "界面测试主播",
            "08-25 18:00",
            "fake",
        )
    finally:
        root.destroy()
