import ctypes
import pytest
import tkinter as tk
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import gui_app
import monitor
from gui_app import (
    MonitorGui,
    compact_ui_text,
    open_project_repository,
    status_text,
    validate_gui_settings,
)
from project_info import PROJECT_REPOSITORY_URL, PROJECT_SOURCE_NOTICE
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

        def shutdown_after_mainloop(self):
            calls.append("shutdown")

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
        "close_action": "exit",
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


def test_project_repository_link_opens_the_fixed_address(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "gui_app.webbrowser.open_new_tab",
        lambda url: opened.append(url) or True,
    )

    assert open_project_repository() is True
    assert opened == [PROJECT_REPOSITORY_URL]


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
        "gui_app.load_streamer_entries",
        lambda _path, loaded, _legacy: list(loaded["streamers"]),
    )
    monkeypatch.setattr("gui_app.messagebox.askyesno", lambda *args, **kwargs: True)
    monkeypatch.setattr("gui_app.is_autostart_enabled", lambda _path: False)
    monkeypatch.setattr("gui_app.get_autostart_snapshot", lambda: None)
    monkeypatch.setattr(
        "gui_app.set_autostart_enabled", lambda _enabled, _path: None
    )
    monkeypatch.setattr("gui_app.restore_autostart_value", lambda _value: None)
    finished = threading.Event()

    class FakeTray:
        def __init__(self, sink):
            self.sink = sink
            self.states = []
            self.stopped = False

        def start(self):
            self.sink({"type": "tray_ready"})
            return True

        def update_state(self, running, live_count):
            self.states.append((running, live_count))

        def stop(self, wait=True):
            self.stopped = True

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

    monkeypatch.setattr(
        "gui_app.create_monitor_service",
        lambda _config, entries, **kwargs: FakeService(
            entries, kwargs["on_event"]
        ),
    )

    try:
        app = MonitorGui(root, config_path, tray_factory=FakeTray)
        app._drain_events()
        root.update_idletasks()

        assert 0.75 <= app.display_scale <= 4.0
        assert app.selected_streamer_id == first["id"]
        assert app.streamer_label_var.get() == "界面测试主播"
        assert app.status_tree.item(first["id"], "values")[3] == "等待首次检测"
        for tree in (app.streamer_tree, app.status_tree):
            for column in tree["columns"]:
                assert str(tree.heading(column, "anchor")) == "center"
                assert str(tree.column(column, "anchor")) == "center"
        assert app.status_tree.bind("<Double-1>")
        assert app.stop_selected_button.cget("text") == "停止所选主播"
        assert str(app.stop_selected_button.cget("state")) == tk.DISABLED
        assert app.settings_canvas.winfo_exists()
        assert app.tray_available is True
        assert app.close_action_combo.get() == "退出主程序"
        assert app.autostart_check.cget("text") == "Windows 开机自启"
        assert app.project_link.cget("text") == PROJECT_REPOSITORY_URL
        assert app.project_link.bind("<Button-1>")
        assert PROJECT_SOURCE_NOTICE == "本项目为开源项目，仅供非商业用途"

        stopped_ids = []
        stop_finished = threading.Event()

        class ActiveService:
            def stop_streamer(self, streamer_id):
                stopped_ids.append(streamer_id)
                stop_finished.set()
                return True

        app.service = ActiveService()
        app.service_thread = SimpleNamespace(is_alive=lambda: True)
        app.runtime_by_id[first["id"]] = {
            "id": first["id"],
            "status": "offline",
            "suspended": False,
        }
        app.status_tree.selection_set(first["id"])
        app._set_running_ui(True)
        app._on_status_selection_changed()
        assert str(app.stop_selected_button.cget("state")) == tk.NORMAL
        assert app.start_button.cget("text") == "停止全部"

        app._stop_selected_streamer()
        assert stop_finished.wait(timeout=1)
        assert stopped_ids == [first["id"]]

        app._set_running_ui(False)
        assert str(app.stop_selected_button.cget("state")) == tk.DISABLED
        app._set_global_status("stopped", "监控已停止")
        app._handle_event(
            {"type": "streamer_stopped", "streamer_id": first["id"]}
        )
        assert app.global_status_var.get() == "已停止"
        assert app.global_detail_var.get() == "监控已停止"

        app.service = None
        app.service_thread = None

        app._show_streamer_log(first["id"])
        app._handle_event(
            {
                "type": "log",
                "streamer_id": first["id"],
                "message": "12:00:00 [INFO] 实时检测日志",
            }
        )
        root.update_idletasks()
        log_text = app.streamer_log_windows[first["id"]]["text"].get(
            "1.0", tk.END
        )
        assert "实时检测日志" in log_text

        for index in range(gui_app.MAX_STREAMER_LOG_LINES + 2):
            app._handle_event(
                {
                    "type": "log",
                    "streamer_id": first["id"],
                    "message": f"overflow-{index}",
                }
            )
        bounded_text = app.streamer_log_windows[first["id"]]["text"].get(
            "1.0", tk.END
        )
        assert app.streamer_logs.count(first["id"]) == gui_app.MAX_STREAMER_LOG_LINES
        assert "overflow-0\n" not in bounded_text
        assert f"overflow-{gui_app.MAX_STREAMER_LOG_LINES + 1}" in bounded_text

        app._enqueue_streamer_log_event(
            {
                "type": "log",
                "streamer_id": first["id"],
                "message": "上一会话残留日志",
            }
        )
        app._start_monitoring()
        assert app.streamer_log_queue.empty()
        assert finished.wait(timeout=1)
        assert app.service_thread is not None
        app.service_thread.join(timeout=1)
        app._drain_events()

        assert app.service is None
        assert app.service_thread is None
        assert app.global_status_var.get() == "已停止"
        assert app.runtime_by_id == {}
        assert app.status_tree.item(first["id"], "values") == (
            "待启动",
            "界面测试主播",
            "",
            "等待首次检测",
        )
        app.config["close_action"] = "hide_to_tray"
        app._on_close()
        assert root.state() == "withdrawn"
        assert app.tray_controller.stopped is False
        app._restore_window()
    finally:
        root.destroy()


def test_streamer_log_event_queue_keeps_only_recent_entries():
    app = object.__new__(MonitorGui)
    app.streamer_log_queue = gui_app.queue.Queue(maxsize=3)

    for index in range(5):
        app._enqueue_streamer_log_event({"type": "log", "message": str(index)})

    messages = []
    while not app.streamer_log_queue.empty():
        messages.append(app.streamer_log_queue.get_nowait()["message"])
    assert messages == ["2", "3", "4"]


def test_silent_start_with_no_enabled_streamers_does_not_show_dialog(monkeypatch):
    app = object.__new__(MonitorGui)
    app.service_thread = None
    app.config = {"streamers": []}
    app.entries = []
    app._save_settings = Mock(return_value=True)
    app._set_global_status = Mock()
    warning = Mock()
    monkeypatch.setattr(gui_app.messagebox, "showwarning", warning)

    assert app._start_monitoring(silent=True) is False

    app._save_settings.assert_called_once_with(
        show_success=False, include_system=False
    )
    app._set_global_status.assert_called_once_with("stopped", "没有已启用的主播")
    warning.assert_not_called()


def test_close_to_tray_never_stops_active_service():
    app = object.__new__(MonitorGui)
    app.config = {"close_action": "hide_to_tray"}
    app.tray_available = True
    app.exiting = False
    app.root = Mock()
    app._request_exit = Mock()

    app._on_close()

    app.root.withdraw.assert_called_once_with()
    app._request_exit.assert_not_called()


def test_exit_requests_one_service_stop(monkeypatch):
    app = object.__new__(MonitorGui)
    app.pending_close = False
    app.runtime_closed = False
    app.exiting = False
    app.service_thread = SimpleNamespace(is_alive=lambda: True)
    app.root = Mock()
    app._stop_monitoring = Mock()
    monkeypatch.setattr(gui_app.messagebox, "askyesno", lambda *args, **kwargs: True)

    app._request_exit()
    app._request_exit()

    assert app.pending_close is True
    app._stop_monitoring.assert_called_once_with()


def test_unexpected_mainloop_exit_waits_for_service_before_tray():
    app = object.__new__(MonitorGui)
    app.runtime_closed = False
    app.service = Mock()
    app.service_thread = Mock()
    app.tray_controller = Mock()
    app._detach_streamer_log_handler = Mock()

    app.shutdown_after_mainloop()

    app.service.stop.assert_called_once_with()
    app.service_thread.join.assert_called_once_with()
    app.tray_controller.stop.assert_called_once_with()


def test_tray_ready_autostarts_monitoring_once():
    app = object.__new__(MonitorGui)
    app.tray_available = False
    app.tray_error = ""
    app.autostart_requested = True
    app.tray_disabled_for_session = False
    app.tray_controller = None
    app._sync_tray_state = Mock()
    app._start_monitoring = Mock(return_value=True)

    app._handle_event({"type": "tray_ready"})
    app._handle_event({"type": "tray_ready"})

    app._start_monitoring.assert_called_once_with(silent=True)


def test_tray_failure_restores_window_and_disables_tray_session(monkeypatch):
    app = object.__new__(MonitorGui)
    app.tray_available = True
    app.tray_error = ""
    app.tray_failure_reported = False
    app.tray_disabled_for_session = False
    app.tray_controller = Mock()
    app.exiting = False
    app.root = Mock()
    app._restore_window = Mock()
    showerror = Mock()
    monkeypatch.setattr(gui_app.messagebox, "showerror", showerror)

    app._handle_event({"type": "tray_failed", "error": "backend stopped"})

    assert app.tray_available is False
    app.tray_controller.stop.assert_called_once_with(wait=False)
    app._restore_window.assert_called_once_with()
    showerror.assert_called_once()


def test_tray_start_false_enqueues_failure(monkeypatch):
    app = object.__new__(MonitorGui)
    app.event_queue = gui_app.queue.Queue()
    app.tray_error = ""
    app.tray_factory = lambda _sink: SimpleNamespace(start=lambda: False)
    monkeypatch.setattr(gui_app, "IS_WINDOWS", True)

    app._start_tray()

    assert app.event_queue.get_nowait() == {
        "type": "tray_failed",
        "error": "无法启动任务栏托盘",
    }


def test_tray_ready_watchdog_disables_stalled_backend():
    app = object.__new__(MonitorGui)
    app.tray_available = False
    app.tray_disabled_for_session = False
    app.exiting = False
    app.runtime_closed = False
    app.tray_controller = Mock()
    app.event_queue = gui_app.queue.Queue()

    app._check_tray_ready()

    assert app.tray_disabled_for_session is True
    app.tray_controller.stop.assert_called_once_with(wait=False)
    assert app.event_queue.get_nowait() == {
        "type": "tray_failed",
        "error": "任务栏托盘初始化超时",
    }


def test_monitor_settings_save_does_not_touch_registry_or_system_choice(
    monkeypatch, tmp_path
):
    app = object.__new__(MonitorGui)
    app.config = monitor._default_config()
    app.config["close_action"] = "hide_to_tray"
    app.config_path = tmp_path / "config.json"
    app.tray_available = False
    app.autostart_var = Mock()
    app.autostart_var.get.return_value = False
    app.monitoring_ui_active = False
    app.root = Mock()
    values = valid_settings()
    values["close_action"] = "exit"
    app._settings_values = Mock(return_value=values)
    registry_read = Mock(side_effect=AssertionError("registry must not be read"))
    registry_write = Mock(side_effect=AssertionError("registry must not be written"))
    monkeypatch.setattr(gui_app, "get_autostart_snapshot", registry_read)
    monkeypatch.setattr(gui_app, "set_autostart_enabled", registry_write)

    assert app._save_settings(show_success=False, include_system=False) is True

    assert app.config["close_action"] == "hide_to_tray"
    registry_read.assert_not_called()
    registry_write.assert_not_called()


def test_saving_while_running_preserves_status_text(monkeypatch, tmp_path):
    app = object.__new__(MonitorGui)
    app.config = monitor._default_config()
    app.config_path = tmp_path / "config.json"
    app.tray_available = True
    app.autostart_var = Mock()
    app.autostart_var.get.return_value = False
    app.monitoring_ui_active = True
    app.root = Mock()
    app.global_status_var = Mock()
    app.global_detail_var = Mock()
    app._settings_values = Mock(return_value=valid_settings())
    app._set_global_status = Mock()
    monkeypatch.setattr(gui_app, "get_autostart_snapshot", lambda: None)
    monkeypatch.setattr(gui_app, "set_autostart_enabled", lambda *_args: None)
    showinfo = Mock()
    monkeypatch.setattr(gui_app.messagebox, "showinfo", showinfo)

    assert app._save_settings() is True

    app._set_global_status.assert_not_called()
    app.global_status_var.set.assert_not_called()
    app.global_detail_var.set.assert_not_called()
    showinfo.assert_called_once()


def test_settings_save_rolls_back_autostart_when_config_write_fails(
    monkeypatch, tmp_path
):
    app = object.__new__(MonitorGui)
    app.config = monitor._default_config()
    original = dict(app.config)
    app.config_path = tmp_path / "config.json"
    app.tray_available = True
    app.autostart_var = Mock()
    app.autostart_var.get.return_value = True
    app.monitoring_ui_active = False
    app.root = Mock()
    app._settings_values = Mock(return_value=valid_settings())
    old_snapshot = ("old-command", 2)
    monkeypatch.setattr(gui_app, "get_autostart_snapshot", lambda: old_snapshot)
    monkeypatch.setattr(gui_app, "set_autostart_enabled", lambda *_args: None)
    restore = Mock()
    monkeypatch.setattr(gui_app, "restore_autostart_value", restore)
    monkeypatch.setattr(
        gui_app,
        "save_config_atomic",
        Mock(side_effect=OSError("disk full")),
    )
    monkeypatch.setattr(gui_app.messagebox, "showerror", Mock())

    assert app._save_settings() is False

    assert app.config == original
    restore.assert_called_once_with(old_snapshot)
