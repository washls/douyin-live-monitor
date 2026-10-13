"""Exercise push drafts, selection, and background lifecycle without network."""

from copy import deepcopy
import json
import threading
import tkinter as tk
from unittest.mock import Mock

import pytest
import requests

import monitor
from douyin_monitor import gui_app
from douyin_monitor.gui_app import MonitorGui
from douyin_monitor.push_channels import ChannelResult, DeliveryReport
from douyin_monitor.streamer_config import save_config_atomic


def channel(channel_id, token=None):
    return {
        "id": channel_id, "type": "pushplus", "name": f"测试 {channel_id}",
        "config": {"token": token or f"EXAMPLE_TOKEN_{channel_id}", "topic": ""},
    }


def test_icon_uses_the_supplied_tk_interpreter(tk_runtime, monkeypatch):
    other = Mock()
    other.tk.call.side_effect = AssertionError("global interpreter must not be used")
    monkeypatch.setattr(tk, "_default_root", other)
    instance = MonitorGui.__new__(MonitorGui)
    instance.root = tk_runtime
    image = instance._create_app_icon()
    assert image.tk is tk_runtime.tk
    other.tk.call.assert_not_called()


def test_form_variables_use_the_supplied_tk_interpreter(tk_runtime, monkeypatch):
    other = Mock()
    monkeypatch.setattr(tk, "_default_root", other)
    instance = MonitorGui.__new__(MonitorGui)
    instance.root = tk_runtime
    instance.config = {}
    instance._create_variables()
    variables = [value for value in vars(instance).values() if isinstance(value, tk.Variable)]
    assert len(variables) >= 19
    assert all(value._tk is tk_runtime.tk for value in variables)


@pytest.fixture
def app(tmp_path, monkeypatch, tk_runtime, request):
    root = tk.Toplevel(tk_runtime)
    root.withdraw()
    work_size, scale = getattr(request, "param", ((1920, 1040), 1.0))
    previous_scale = float(root.tk.call("tk", "scaling"))
    root.tk.call("tk", "scaling", 96 / 72 * scale)
    monkeypatch.setattr(gui_app, "get_display_scale", lambda _root: scale)
    monkeypatch.setattr(gui_app, "get_work_area", lambda _root: (0, 0, *work_size))
    config = monitor._default_config()
    config.update(channels=[channel("a"), channel("b")], push_mode="selected", selected_channel_ids=["a"])
    path = tmp_path / "config.json"
    save_config_atomic(path, config)
    monkeypatch.setattr(monitor, "load_monitor_state", lambda: {})
    monkeypatch.setattr(gui_app, "load_streamer_entries", lambda _path, loaded, _legacy: list(loaded["streamers"]))
    monkeypatch.setattr(gui_app, "is_autostart_enabled", lambda _path: False)
    monkeypatch.setattr(MonitorGui, "_start_tray", lambda _self: None)
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(gui_app.messagebox, name, Mock())
    monkeypatch.setattr(gui_app.messagebox, "askyesno", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("requests.Session.request", Mock(side_effect=AssertionError("network forbidden")))
    instance = MonitorGui(root, path)
    try:
        yield instance
    finally:
        instance.shutdown_after_mainloop()
        try:
            root.destroy()
        except tk.TclError:
            pass
        tk_runtime.tk.call("tk", "scaling", previous_scale)


@pytest.fixture(scope="module")
def tk_runtime():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


def test_channel_range_is_separate_from_highlight_and_all_preserves_selection(app):
    assert [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()] == [
        "运行状态", "主播详情", "监控设置", "推送设置",
    ]
    app.push_tree.selection_set("b")
    assert app.push_tree.set("b", "included") == "☐"
    app.push_mode_var.set("all")
    app._refresh_push_channels()
    assert all(app.push_tree.set(item, "included") == "☑" for item in ("a", "b"))
    app._toggle_push_selection()
    assert app.push_selected_ids == ["a"]
    app.push_mode_var.set("selected")
    app._refresh_push_channels()
    assert app.push_tree.set("b", "included") == "☐"
    app._toggle_push_selection()
    assert app.push_selected_ids == ["a", "b"]
    assert "2 / 2" in app.push_count_var.get()


def test_channel_crud_retains_ids_and_new_channel_is_not_selected(app):
    app._upsert_push_channel(channel("c"))
    assert app.push_selected_ids == ["a"]
    assert app.push_tree.set("c", "included") == "☐"
    edited = channel("c")
    edited["name"] = "手机通知"
    app._upsert_push_channel(edited)
    assert [item["id"] for item in app.push_channels] == ["a", "b", "c"]
    assert app.push_tree.set("c", "name") == "手机通知"
    with pytest.raises(ValueError, match="目的地重复"):
        app._upsert_push_channel(channel("d", token="EXAMPLE_TOKEN_a"))
    assert len(app.push_channels) == 3
    app.push_tree.selection_set("a")
    app._delete_push_channel()
    assert app.push_selected_ids == []
    assert "0 / 2" in app.push_count_var.get()
    assert "当前不发送通知" in app.push_count_var.get()
    assert app._save_push_settings() is True
    assert json.loads(app.config_path.read_text(encoding="utf-8"))["selected_channel_ids"] == []


def test_mouse_checkbox_selects_the_clicked_row_and_toggles_only_self(app, monkeypatch):
    monkeypatch.setattr(app.push_tree, "identify_row", lambda _y: "b")
    monkeypatch.setattr(app.push_tree, "identify_column", lambda _x: "#1")
    event = Mock(x=1, y=1)
    assert app._on_push_click(event) == "break"
    assert app._highlighted_channel_id() == "b"
    assert app.push_selected_ids == ["a", "b"]
    app._set_running_ui(True)
    app._on_push_click(event)
    assert app.push_selected_ids == ["a", "b"]
    assert app.push_tree.bind("<space>")


def test_two_save_buttons_commit_only_their_own_draft(app, monkeypatch):
    original_channels = deepcopy(app.config["channels"])
    app._upsert_push_channel(channel("c"))
    app.check_interval_var.set("45")
    monkeypatch.setattr(gui_app, "get_autostart_snapshot", Mock(side_effect=AssertionError("registry forbidden")))
    monkeypatch.setattr(gui_app, "set_autostart_enabled", Mock(side_effect=AssertionError("registry forbidden")))
    assert app._save_settings(show_success=False, include_system=False) is True
    assert app.config["channels"] == original_channels
    assert app.config["check_interval"] == 45
    assert len(app.push_channels) == 3
    app.check_interval_var.set("invalid")
    assert app._save_push_settings() is True
    assert app.config["check_interval"] == 45
    assert len(app.config["channels"]) == 3
    assert app.check_interval_var.get() == "invalid"


def test_startup_validates_both_drafts_before_one_atomic_save(app, monkeypatch):
    original_config = deepcopy(app.config)
    original_bytes = app.config_path.read_bytes()
    save = Mock(wraps=save_config_atomic)
    monkeypatch.setattr(gui_app, "save_config_atomic", save)
    monkeypatch.setattr(gui_app, "get_autostart_snapshot", Mock(side_effect=AssertionError("registry forbidden")))
    monkeypatch.setattr(gui_app, "set_autostart_enabled", Mock(side_effect=AssertionError("registry forbidden")))
    app.check_interval_var.set("0")
    assert app._save_startup_drafts() is False
    app.check_interval_var.set("60")
    app.push_channels.append(channel("c", token="EXAMPLE_TOKEN_a"))
    assert app._save_startup_drafts() is False
    save.assert_not_called()
    assert app.config == original_config
    assert app.config_path.read_bytes() == original_bytes
    app.push_channels[-1] = channel("c")
    app.close_action_var.set("隐藏到任务栏托盘")
    assert app._save_startup_drafts() is True
    save.assert_called_once()
    assert app.config["check_interval"] == 60
    assert len(app.config["channels"]) == 3
    assert app.config["close_action"] == original_config["close_action"]


@pytest.mark.parametrize("single, expected", [(False, ["a"]), (True, ["b"])])
def test_push_test_uses_draft_and_queue_without_saving(app, monkeypatch, single, expected):
    before = app.config_path.read_bytes()
    app.check_interval_var.set("invalid")
    app.push_tree.selection_set("b")
    release = threading.Event()
    report = DeliveryReport("example_event", tuple(
        ChannelResult(item, f"测试 {item}", "pushplus", "accepted") for item in expected
    ))
    fake = Mock()
    fake.last_report = report
    fake.verify_connection.side_effect = lambda: release.wait(timeout=2)
    factory = Mock(return_value=fake)
    monkeypatch.setattr(gui_app, "create_notifier", factory)
    app._test_push(single=single)
    assert factory.call_args.args[0]["selected_channel_ids"] == expected
    assert app.testing_push is True
    assert app._start_monitoring() is False
    assert str(app.add_push_button.cget("state")) == tk.DISABLED
    assert str(app.start_button.cget("state")) == tk.DISABLED
    assert app.config_path.read_bytes() == before
    assert app.config["selected_channel_ids"] == ["a"]
    assert app._save_push_settings() is False
    release.set()
    app.push_test_thread.join(timeout=2)
    assert app.push_test_result_var.get().startswith("正在测试")
    event = app.event_queue.get_nowait()
    assert event["type"] == "push_test"
    app._handle_event(event)
    assert app.testing_push is False
    assert app.push_test_thread is None
    assert app.push_test_result_var.get() == report.summary
    assert app.push_tree.set(expected[0], "result") == "服务已接收"
    fake.close.assert_called_once_with()
    assert app.config_path.read_bytes() == before


def test_current_scope_zero_does_not_create_a_notifier(app, monkeypatch):
    app.push_selected_ids.clear()
    app._refresh_push_channels()
    factory = Mock(side_effect=AssertionError("empty scope must not submit"))
    monkeypatch.setattr(gui_app, "create_notifier", factory)
    app._test_push()
    factory.assert_not_called()
    assert str(app.test_push_button.cget("state")) == tk.DISABLED


def test_gui_fixture_blocks_top_level_post_and_session_get(app):
    with pytest.raises(AssertionError, match="network forbidden"):
        requests.post("https://example.invalid/push", json={"title": "example"})
    with requests.Session() as session:
        with pytest.raises(AssertionError, match="network forbidden"):
            session.get("https://example.invalid/status")


def test_notification_report_updates_table_while_editing_is_locked(app):
    app._set_running_ui(True)
    app._handle_event({
        "type": "notification", "delivery_report": {
            "summary": "已受理 1/2，结果未知 1",
            "results": [{"channel_id": "a", "status": "accepted"}, {"channel_id": "b", "status": "unknown"}],
        },
    })
    assert app.push_tree.set("a", "result") == "服务已接收"
    assert app.push_tree.set("b", "result") == "结果未知"
    assert str(app.add_push_button.cget("state")) == tk.DISABLED
    assert str(app.test_push_button.cget("state")) == tk.DISABLED
    assert app.global_detail_var.get() == "0 个任务运行，0 个直播中"


def test_exit_cancels_and_reclaims_test_worker(app, monkeypatch):
    release = threading.Event()
    report = DeliveryReport("example_event", (ChannelResult("a", "测试 a", "pushplus", "cancelled"),))
    fake = Mock()
    fake.last_report = report
    fake.verify_connection.side_effect = lambda: release.wait(timeout=2) and False
    fake.dispatcher.stop.side_effect = release.set
    monkeypatch.setattr(gui_app, "create_notifier", Mock(return_value=fake))
    app._test_push()
    thread = app.push_test_thread
    app._request_exit()
    assert app.push_test_cancel.is_set()
    thread.join(timeout=2)
    app._drain_events()
    assert not thread.is_alive()
    assert app.runtime_closed is True
    fake.close.assert_called_once_with()


def descendants(widget):
    return [child for item in widget.winfo_children() for child in [item, *descendants(item)]]


def test_channel_editor_masks_credentials_and_changes_fields(app):
    app._open_channel_editor(app.push_channels[0])
    dialog = app.channel_editor
    children = descendants(dialog)
    combo = next(item for item in children if isinstance(item, gui_app.ttk.Combobox))
    masked = [item for item in children if isinstance(item, gui_app.ttk.Entry) and item.cget("show") == "●"]
    assert len(masked) == 1
    assert masked[0].get() == "EXAMPLE_TOKEN_a"
    combo.set("通用 Webhook")
    combo.event_generate("<<ComboboxSelected>>")
    children = descendants(dialog)
    labels = [item.cget("text") for item in children if isinstance(item, gui_app.ttk.Label)]
    assert "Webhook URL" in labels
    assert "请求头（JSON 对象）" in labels
    masked = [item for item in children if isinstance(item, gui_app.ttk.Entry) and item.cget("show") == "●"]
    assert len(masked) == 2
    reveal = next(item for item in children if isinstance(item, gui_app.ttk.Checkbutton))
    reveal.invoke()
    assert all(item.cget("show") == "" for item in masked)
    cancel = next(item for item in children if isinstance(item, gui_app.ttk.Button) and item.cget("text") == "取消")
    cancel.invoke()
    assert app.channel_editor is None
    assert app.push_channels[0]["type"] == "pushplus"


def test_webhook_editor_rejects_bad_headers_without_persisting_draft(app):
    before = app.config_path.read_bytes()
    app._open_channel_editor()
    dialog = app.channel_editor
    combo = next(item for item in descendants(dialog) if isinstance(item, gui_app.ttk.Combobox))
    combo.set("通用 Webhook")
    combo.event_generate("<<ComboboxSelected>>")
    masked = [item for item in descendants(dialog) if isinstance(item, gui_app.ttk.Entry) and item.cget("show") == "●"]
    masked[0].insert(0, "https://example.invalid/push")
    masked[1].delete(0, tk.END)
    masked[1].insert(0, "EXAMPLE_INVALID_JSON_SECRET")
    save = next(item for item in descendants(dialog) if isinstance(item, gui_app.ttk.Button) and item.cget("text") == "确定")
    save.invoke()
    error = gui_app.messagebox.showerror.call_args.args[1]
    assert error == "自定义请求头必须是 JSON 对象"
    assert "EXAMPLE_INVALID_JSON_SECRET" not in error
    assert app.channel_editor is dialog
    assert len(app.push_channels) == 2
    assert app.config_path.read_bytes() == before
    masked[1].delete(0, tk.END)
    masked[1].insert(0, '{"Authorization":"EXAMPLE_WEBHOOK_TOKEN"}')
    save.invoke()
    assert app.channel_editor is None
    assert len(app.push_channels) == 3
    assert app.push_channels[-1]["type"] == "webhook"
    assert app.config_path.read_bytes() == before


def assert_control_in_viewport(root, canvas, control):
    control.event_generate("<FocusIn>")
    root.update()
    top = canvas.winfo_rooty()
    bottom = top + canvas.winfo_height()
    assert control.winfo_rooty() >= top
    assert control.winfo_rooty() + control.winfo_height() <= bottom
    assert canvas.cget("yscrollcommand")


@pytest.mark.parametrize("app", [
    ((1920, 1040), 1.0), ((1366, 728), 1.25), ((1366, 728), 2.0),
], indirect=True)
def test_push_page_and_each_editor_fields_stay_reachable_when_scaled(app):
    root = app.root
    app.show_window()
    root.deiconify()
    app.notebook.select(3)
    root.update()
    assert root.winfo_width() <= app.work_area[2]
    assert root.winfo_height() <= app.work_area[3]
    for control in (
        *app.push_mode_buttons, app.add_push_button,
        app.test_push_button, app.save_push_button,
    ):
        assert_control_in_viewport(root, app.push_canvas, control)
    for provider in gui_app.CHANNEL_TYPES.values():
        app._open_channel_editor()
        window = app.channel_editor
        combo = next(item for item in descendants(window) if isinstance(item, gui_app.ttk.Combobox))
        combo.set(provider)
        combo.event_generate("<<ComboboxSelected>>")
        root.update()
        canvas = next(item for item in descendants(window) if isinstance(item, tk.Canvas))
        for entry in (item for item in descendants(window) if isinstance(item, gui_app.ttk.Entry)):
            assert_control_in_viewport(root, canvas, entry)
        save = next(item for item in descendants(window) if isinstance(item, gui_app.ttk.Button) and item.cget("text") == "确定")
        assert_control_in_viewport(root, canvas, save)
        cancel = next(item for item in descendants(window) if isinstance(item, gui_app.ttk.Button) and item.cget("text") == "取消")
        cancel.invoke()
