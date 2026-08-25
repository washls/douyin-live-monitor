"""Tkinter desktop interface for the Douyin live monitor."""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Dict, Mapping

import monitor
from monitor_service import MonitorService
from notifier import ServerChanNotifier
from streamer_config import (
    add_streamer,
    enabled_streamers,
    remove_streamer,
    save_config_atomic,
    update_streamer,
)


COLORS = {
    "canvas": "#f3f3f3",
    "sidebar": "#f8f8f8",
    "surface": "#ffffff",
    "text": "#1a1a1a",
    "muted": "#606060",
    "border": "#d6d6d6",
    "accent": "#0067c0",
    "success": "#0f7b0f",
    "danger": "#c42b1c",
    "warning": "#9d5d00",
}

STATUS_TEXT = {
    "pending": "待启动",
    "unknown": "待检测",
    "starting": "启动中",
    "live": "直播中",
    "offline": "未开播",
    "error": "异常",
    "suspended": "已暂停",
    "stopped": "已停止",
}


def status_text(status: str) -> str:
    """Return concise Chinese status copy for the UI."""
    return STATUS_TEXT.get(str(status or "unknown"), "未知")


def compact_ui_text(value: Any, limit: int) -> str:
    """Collapse untrusted remote text into one bounded table-cell value."""
    return " ".join(str(value or "").split())[:limit].rstrip()


def validate_gui_settings(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize settings collected from form controls."""
    ranges = {
        "check_interval": (1, 86400, "检测间隔"),
        "repeat_notify_interval": (1, 86400, "重复提醒间隔"),
        "max_repeat_notifications": (0, 100, "最多重复提醒"),
        "max_concurrent_checks": (1, 8, "并发检测数"),
    }
    normalized: Dict[str, Any] = {}
    for key, (minimum, maximum, label) in ranges.items():
        try:
            number = int(str(values.get(key, "")).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if number < minimum or number > maximum:
            raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
        normalized[key] = number

    push_url = str(values.get("push_url", "") or "").strip()
    if push_url:
        ServerChanNotifier._parse_push_url(push_url)
    normalized["push_url"] = push_url
    normalized["sendkey"] = "" if push_url else str(values.get("sendkey", "") or "")
    normalized["push_uid"] = "" if push_url else str(values.get("push_uid", "") or "")
    for key in (
        "notify_on_stream_end",
        "startup_notify",
        "enable_daily_intimacy_reminder",
    ):
        normalized[key] = bool(values.get(key, False))
    return normalized


class MonitorGui:
    """Windows-oriented light desktop interface."""

    def __init__(self, root: tk.Tk, config_path: Path, debug: bool = False):
        self.root = root
        self.config_path = Path(config_path)
        self.debug = debug
        self.config = monitor.load_config(self.config_path)
        self.entries = monitor._load_streamer_entries(self.config_path, self.config)
        self.service: MonitorService | None = None
        self.service_thread: threading.Thread | None = None
        self.event_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self.pending_close = False
        self.testing_push = False
        self.selected_streamer_id = ""
        self.runtime_by_id: Dict[str, Dict[str, Any]] = {}

        self._configure_window()
        self._configure_styles()
        self._create_variables()
        self._build_layout()
        self._refresh_streamer_views()
        self._load_settings_into_form()
        self._set_global_status("stopped", "监控未启动")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._drain_events)

    def _configure_window(self) -> None:
        self.root.title(f"抖音直播监听器  v{monitor.APP_VERSION}")
        self.root.geometry("1080x700")
        self.root.minsize(900, 600)
        self.root.configure(background=COLORS["canvas"])
        self.root.option_add("*Font", "{Segoe UI} 10")
        self._app_icon = self._create_app_icon()
        self.root.iconphoto(True, self._app_icon)

    def _create_app_icon(self) -> tk.PhotoImage:
        """Create a small flat live-status mark without an external asset."""
        icon = tk.PhotoImage(width=32, height=32)
        blue = COLORS["accent"]
        white = "#ffffff"
        red = COLORS["danger"]
        for y in range(3, 29):
            inset = 2 if y in {3, 28} else 1 if y in {4, 27} else 0
            icon.put(blue, to=(3 + inset, y, 29 - inset, y + 1))
        for y in range(9, 23):
            for x in range(9, 23):
                distance = (x - 15.5) ** 2 + (y - 15.5) ** 2
                if 31 <= distance <= 46:
                    icon.put(white, (x, y))
                elif distance <= 8:
                    icon.put(red, (x, y))
        return icon

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TFrame", background=COLORS["canvas"])
        style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure(
            "Title.TLabel",
            background=COLORS["canvas"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 18),
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["canvas"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "Muted.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
        )
        style.configure("Surface.TLabelframe", background=COLORS["surface"])
        style.configure(
            "Surface.TLabelframe.Label",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 10),
        )
        style.configure("Primary.TButton", padding=(16, 8))
        style.configure("Action.TButton", padding=(12, 7))
        style.configure("Treeview", rowheight=34, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.map(
            "Treeview",
            background=[("selected", "#dcecff")],
            foreground=[("selected", COLORS["text"])],
        )

    def _create_variables(self) -> None:
        self.global_status_var = tk.StringVar()
        self.global_detail_var = tk.StringVar()
        self.streamer_id_var = tk.StringVar()
        self.streamer_label_var = tk.StringVar()
        self.streamer_url_var = tk.StringVar()
        self.streamer_enabled_var = tk.BooleanVar(value=True)
        self.push_url_var = tk.StringVar()
        self.reveal_push_url_var = tk.BooleanVar(value=False)
        self.check_interval_var = tk.StringVar()
        self.repeat_interval_var = tk.StringVar()
        self.max_repeat_var = tk.StringVar()
        self.max_concurrent_var = tk.StringVar()
        self.notify_end_var = tk.BooleanVar()
        self.startup_notify_var = tk.BooleanVar()
        self.daily_reminder_var = tk.BooleanVar()

    def _build_layout(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = ttk.Frame(self.root, style="App.TFrame", padding=(24, 18, 24, 14))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ttk.Label(header, text="抖音直播监听器", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text=f"v{monitor.APP_VERSION}  ·  多主播监控",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        status_box = ttk.Frame(header, style="App.TFrame")
        status_box.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 18))
        self.status_dot = tk.Label(
            status_box,
            text="●",
            fg=COLORS["muted"],
            bg=COLORS["canvas"],
            font=("Segoe UI", 12),
        )
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(0, 7))
        ttk.Label(status_box, textvariable=self.global_status_var, style="Subtitle.TLabel").grid(
            row=0, column=1, sticky="e"
        )
        ttk.Label(status_box, textvariable=self.global_detail_var, style="Subtitle.TLabel").grid(
            row=1, column=1, sticky="e"
        )

        self.start_button = ttk.Button(
            header,
            text="开始监控",
            style="Primary.TButton",
            command=self._start_monitoring,
            width=12,
        )
        self.start_button.grid(row=0, column=2, rowspan=2, sticky="e")

        paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        paned.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 14))

        sidebar = ttk.Frame(paned, style="Sidebar.TFrame", padding=14, width=350)
        content = ttk.Frame(paned, style="Surface.TFrame", padding=14)
        paned.add(sidebar, weight=1)
        paned.add(content, weight=2)
        self._build_sidebar(sidebar)
        self._build_content(content)

        footer = ttk.Frame(self.root, style="App.TFrame", padding=(22, 0, 22, 12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="配置保存在程序目录，不会上传到网络",
            style="Subtitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            footer,
            text="关闭窗口前会先停止监控",
            style="Subtitle.TLabel",
        ).grid(row=0, column=1, sticky="e")

    def _build_sidebar(self, parent: ttk.Frame) -> None:
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        title_row = ttk.Frame(parent, style="Sidebar.TFrame")
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        title_row.grid_columnconfigure(0, weight=1)
        ttk.Label(
            title_row,
            text="主播任务",
            background=COLORS["sidebar"],
            font=("Segoe UI Semibold", 11),
        ).grid(row=0, column=0, sticky="w")
        self.streamer_count_label = ttk.Label(
            title_row, text="0 个", background=COLORS["sidebar"]
        )
        self.streamer_count_label.grid(row=0, column=1, sticky="e")

        self.streamer_tree = ttk.Treeview(
            parent,
            columns=("status", "name"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.streamer_tree.heading("status", text="状态")
        self.streamer_tree.heading("name", text="主播")
        self.streamer_tree.column("status", width=76, minwidth=70, stretch=False)
        self.streamer_tree.column("name", width=220, minwidth=140, stretch=True)
        self.streamer_tree.grid(row=1, column=0, sticky="nsew")
        self.streamer_tree.bind("<<TreeviewSelect>>", self._on_streamer_selected)
        sidebar_scroll = ttk.Scrollbar(
            parent, orient=tk.VERTICAL, command=self.streamer_tree.yview
        )
        sidebar_scroll.grid(row=1, column=1, sticky="ns")
        self.streamer_tree.configure(yscrollcommand=sidebar_scroll.set)

        actions = ttk.Frame(parent, style="Sidebar.TFrame")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.new_button = ttk.Button(
            actions, text="新增主播", style="Action.TButton", command=self._new_streamer
        )
        self.new_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.remove_button = ttk.Button(
            actions,
            text="删除",
            style="Action.TButton",
            command=self._remove_selected_streamer,
        )
        self.remove_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

    def _build_content(self, parent: ttk.Frame) -> None:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        status_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=16)
        detail_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=16)
        settings_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=16)
        self.notebook.add(status_tab, text="运行状态")
        self.notebook.add(detail_tab, text="主播详情")
        self.notebook.add(settings_tab, text="监控设置")
        self._build_status_tab(status_tab)
        self._build_detail_tab(detail_tab)
        self._build_settings_tab(settings_tab)

    def _build_status_tab(self, parent: ttk.Frame) -> None:
        parent.grid_rowconfigure(2, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="任务状态", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="每个主播独立检测，单个任务异常不会中断其他任务。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 12))
        self.status_tree = ttk.Treeview(
            parent,
            columns=("status", "name", "last_check", "detail"),
            show="headings",
        )
        for column, label in (
            ("status", "状态"),
            ("name", "主播"),
            ("last_check", "最后检测"),
            ("detail", "详情"),
        ):
            self.status_tree.heading(column, text=label)
        self.status_tree.column("status", width=82, stretch=False)
        self.status_tree.column("name", width=150, minwidth=110)
        self.status_tree.column("last_check", width=112, stretch=False)
        self.status_tree.column("detail", width=250, minwidth=160)
        self.status_tree.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.status_tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.status_tree.configure(yscrollcommand=scroll.set)
        self.status_tree.tag_configure("live", foreground=COLORS["success"])
        self.status_tree.tag_configure("error", foreground=COLORS["danger"])
        self.status_tree.tag_configure("suspended", foreground=COLORS["warning"])

    def _build_detail_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="主播详情", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="选择左侧任务后编辑，或点击“新增主播”清空表单。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 18))
        form = ttk.LabelFrame(
            parent, text="任务配置", style="Surface.TLabelframe", padding=16
        )
        form.grid(row=2, column=0, sticky="ew")
        form.grid_columnconfigure(1, weight=1)
        ttk.Label(form, text="本地 ID").grid(row=0, column=0, sticky="w", pady=7)
        ttk.Entry(form, textvariable=self.streamer_id_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=(14, 0), pady=7
        )
        ttk.Label(form, text="显示名称").grid(row=1, column=0, sticky="w", pady=7)
        self.label_entry = ttk.Entry(form, textvariable=self.streamer_label_var)
        self.label_entry.grid(row=1, column=1, sticky="ew", padx=(14, 0), pady=7)
        ttk.Label(form, text="抖音链接").grid(row=2, column=0, sticky="w", pady=7)
        self.url_entry = ttk.Entry(form, textvariable=self.streamer_url_var)
        self.url_entry.grid(row=2, column=1, sticky="ew", padx=(14, 0), pady=7)
        self.enabled_check = ttk.Checkbutton(
            form, text="启用此主播", variable=self.streamer_enabled_var
        )
        self.enabled_check.grid(row=3, column=1, sticky="w", padx=(14, 0), pady=(8, 4))
        action_row = ttk.Frame(parent, style="Surface.TFrame")
        action_row.grid(row=3, column=0, sticky="e", pady=(16, 0))
        self.save_streamer_button = ttk.Button(
            action_row,
            text="保存主播",
            style="Primary.TButton",
            command=self._save_streamer,
        )
        self.save_streamer_button.grid(row=0, column=0)

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="监控设置", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="保存后在下一次启动监控时生效。推送地址仅保存在本机。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 14))

        push_frame = ttk.LabelFrame(
            parent, text="Server酱³ 推送", style="Surface.TLabelframe", padding=14
        )
        push_frame.grid(row=2, column=0, sticky="ew")
        push_frame.grid_columnconfigure(0, weight=1)
        self.push_entry = ttk.Entry(
            push_frame, textvariable=self.push_url_var, show="●"
        )
        self.push_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.reveal_check = ttk.Checkbutton(
            push_frame,
            text="显示",
            variable=self.reveal_push_url_var,
            command=self._toggle_push_url_visibility,
        )
        self.reveal_check.grid(row=0, column=1)
        self.test_push_button = ttk.Button(
            push_frame,
            text="测试推送",
            style="Action.TButton",
            command=self._test_push,
        )
        self.test_push_button.grid(row=0, column=2, padx=(10, 0))

        timing = ttk.LabelFrame(
            parent, text="检测与提醒", style="Surface.TLabelframe", padding=14
        )
        timing.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        timing.grid_columnconfigure((1, 3), weight=1)
        self.setting_inputs = []
        fields = (
            ("检测间隔（秒）", self.check_interval_var, 0, 0),
            ("重复提醒间隔（秒）", self.repeat_interval_var, 0, 2),
            ("最多重复提醒", self.max_repeat_var, 1, 0),
            ("并发检测数", self.max_concurrent_var, 1, 2),
        )
        for label, variable, row, column in fields:
            ttk.Label(timing, text=label).grid(
                row=row, column=column, sticky="w", pady=7, padx=(0, 10)
            )
            entry = ttk.Entry(timing, textvariable=variable, width=12)
            entry.grid(row=row, column=column + 1, sticky="ew", pady=7, padx=(0, 20))
            self.setting_inputs.append(entry)

        options = ttk.Frame(parent, style="Surface.TFrame")
        options.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        self.notify_end_check = ttk.Checkbutton(
            options, text="下播时通知", variable=self.notify_end_var
        )
        self.notify_end_check.grid(row=0, column=0, sticky="w", pady=4)
        self.startup_check = ttk.Checkbutton(
            options, text="启动时发送汇总通知", variable=self.startup_notify_var
        )
        self.startup_check.grid(row=1, column=0, sticky="w", pady=4)
        self.daily_check = ttk.Checkbutton(
            options, text="启用每日亲密度提醒", variable=self.daily_reminder_var
        )
        self.daily_check.grid(row=2, column=0, sticky="w", pady=4)
        self.save_settings_button = ttk.Button(
            parent,
            text="保存设置",
            style="Primary.TButton",
            command=self._save_settings,
        )
        self.save_settings_button.grid(row=5, column=0, sticky="e", pady=(16, 0))

    def _load_settings_into_form(self) -> None:
        self.push_url_var.set(self.config.get("push_url", ""))
        self.check_interval_var.set(str(self.config.get("check_interval", 30)))
        self.repeat_interval_var.set(
            str(self.config.get("repeat_notify_interval", 600))
        )
        self.max_repeat_var.set(
            str(self.config.get("max_repeat_notifications", 3))
        )
        self.max_concurrent_var.set(
            str(self.config.get("max_concurrent_checks", 2))
        )
        self.notify_end_var.set(self.config.get("notify_on_stream_end", True))
        self.startup_notify_var.set(self.config.get("startup_notify", False))
        self.daily_reminder_var.set(
            self.config.get("enable_daily_intimacy_reminder", True)
        )

    def _settings_values(self) -> Dict[str, Any]:
        return {
            "push_url": self.push_url_var.get(),
            "sendkey": self.config.get("sendkey", ""),
            "push_uid": self.config.get("push_uid", ""),
            "check_interval": self.check_interval_var.get(),
            "repeat_notify_interval": self.repeat_interval_var.get(),
            "max_repeat_notifications": self.max_repeat_var.get(),
            "max_concurrent_checks": self.max_concurrent_var.get(),
            "notify_on_stream_end": self.notify_end_var.get(),
            "startup_notify": self.startup_notify_var.get(),
            "enable_daily_intimacy_reminder": self.daily_reminder_var.get(),
        }

    def _save_settings(self, show_success: bool = True) -> bool:
        try:
            normalized = validate_gui_settings(self._settings_values())
            self.config.update(normalized)
            save_config_atomic(self.config_path, self.config)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存设置", str(exc), parent=self.root)
            return False
        if show_success:
            self._set_global_status("stopped", "设置已保存")
        return True

    def _toggle_push_url_visibility(self) -> None:
        self.push_entry.configure(show="" if self.reveal_push_url_var.get() else "●")

    def _refresh_streamer_views(self) -> None:
        current_id = self.selected_streamer_id
        for tree in (self.streamer_tree, self.status_tree):
            for item in tree.get_children():
                tree.delete(item)
        for entry in self.entries:
            streamer_id = entry["id"]
            state, name, last_check, detail = self._streamer_row_values(entry)
            self.streamer_tree.insert(
                "", tk.END, iid=streamer_id, values=(status_text(state), name)
            )
            self.status_tree.insert(
                "",
                tk.END,
                iid=streamer_id,
                values=(
                    status_text(state),
                    name,
                    last_check,
                    detail,
                ),
                tags=(state,),
            )
        self.streamer_count_label.configure(text=f"{len(self.entries)} 个")
        if current_id and self.streamer_tree.exists(current_id):
            self.streamer_tree.selection_set(current_id)
        elif self.entries:
            first_id = self.entries[0]["id"]
            self.streamer_tree.selection_set(first_id)
            self.streamer_tree.focus(first_id)
            self._on_streamer_selected()

    def _streamer_row_values(self, entry: Mapping[str, Any]):
        streamer_id = str(entry["id"])
        runtime = self.runtime_by_id.get(streamer_id, {})
        state = runtime.get("status", "pending")
        if not entry.get("enabled", True):
            state = "stopped"
        name = compact_ui_text(
            runtime.get("nickname") or entry.get("label") or "未命名主播",
            80,
        )
        detail = compact_ui_text(
            runtime.get("last_error")
            or runtime.get("method")
            or ("已停用" if not entry.get("enabled", True) else "等待首次检测"),
            160,
        )
        return state, name, runtime.get("last_check", ""), detail

    def _refresh_runtime_row(self, streamer_id: str) -> None:
        entry = next(
            (item for item in self.entries if item["id"] == streamer_id), None
        )
        if entry is None:
            return
        state, name, last_check, detail = self._streamer_row_values(entry)
        if self.streamer_tree.exists(streamer_id):
            self.streamer_tree.item(
                streamer_id, values=(status_text(state), name)
            )
        if self.status_tree.exists(streamer_id):
            self.status_tree.item(
                streamer_id,
                values=(status_text(state), name, last_check, detail),
                tags=(state,),
            )

    def _new_streamer(self) -> None:
        self.selected_streamer_id = ""
        self.streamer_id_var.set("新增后自动生成")
        self.streamer_label_var.set("")
        self.streamer_url_var.set("")
        self.streamer_enabled_var.set(True)
        self.notebook.select(1)
        self.label_entry.focus_set()

    def _on_streamer_selected(self, _event=None) -> None:
        selection = self.streamer_tree.selection()
        if not selection:
            return
        streamer_id = selection[0]
        entry = next((item for item in self.entries if item["id"] == streamer_id), None)
        if entry is None:
            return
        self.selected_streamer_id = streamer_id
        self.streamer_id_var.set(streamer_id)
        self.streamer_label_var.set(entry.get("label", ""))
        self.streamer_url_var.set(entry["url"])
        self.streamer_enabled_var.set(entry.get("enabled", True))

    def _save_streamer(self) -> None:
        try:
            if self.selected_streamer_id:
                entry = update_streamer(
                    self.config,
                    self.selected_streamer_id,
                    self.streamer_url_var.get(),
                    label=self.streamer_label_var.get(),
                    enabled=self.streamer_enabled_var.get(),
                )
            else:
                entry = add_streamer(
                    self.config,
                    self.streamer_url_var.get(),
                    label=self.streamer_label_var.get(),
                )
                entry["enabled"] = self.streamer_enabled_var.get()
            save_config_atomic(self.config_path, self.config)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存主播", str(exc), parent=self.root)
            return
        self.entries = list(self.config["streamers"])
        self.selected_streamer_id = entry["id"]
        self.streamer_id_var.set(entry["id"])
        self._refresh_streamer_views()
        self._set_global_status("stopped", "主播配置已保存")

    def _remove_selected_streamer(self) -> None:
        if not self.selected_streamer_id:
            messagebox.showinfo("删除主播", "请先选择一个主播。", parent=self.root)
            return
        entry = next(
            (item for item in self.entries if item["id"] == self.selected_streamer_id),
            None,
        )
        if entry is None:
            return
        name = entry.get("label") or entry["url"]
        if not messagebox.askyesno(
            "确认删除",
            f"确定删除主播“{name}”吗？\n\n此操作只删除本地配置。",
            parent=self.root,
        ):
            return
        try:
            remove_streamer(self.config, entry["id"])
            save_config_atomic(self.config_path, self.config)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法删除主播", str(exc), parent=self.root)
            return
        self.entries = list(self.config["streamers"])
        self.runtime_by_id.pop(entry["id"], None)
        self.selected_streamer_id = ""
        self._new_streamer()
        self._refresh_streamer_views()
        self._set_global_status("stopped", "主播已删除")

    def _start_monitoring(self) -> None:
        if self.service_thread and self.service_thread.is_alive():
            self._stop_monitoring()
            return
        if not self._save_settings(show_success=False):
            return
        self.entries = list(self.config.get("streamers", []))
        active_entries = enabled_streamers(self.entries)
        if not active_entries:
            messagebox.showwarning(
                "无法开始监控", "请至少添加并启用一个主播。", parent=self.root
            )
            return
        if not monitor.is_serverchan_configured(self.config):
            proceed = messagebox.askyesno(
                "尚未配置推送",
                "当前没有有效的 Server酱³ 推送地址。\n"
                "可以继续检测，但不会收到手机通知。\n\n仍然开始监控吗？",
                parent=self.root,
            )
            if not proceed:
                self.notebook.select(2)
                self.push_entry.focus_set()
                return

        def worker_factory(entry: Mapping[str, Any]):
            return monitor.DouyinLiveMonitor(
                dict(self.config), target_url=entry["url"], debug=self.debug
            )

        try:
            self.service = MonitorService(
                active_entries,
                worker_factory=worker_factory,
                check_interval=self.config["check_interval"],
                max_concurrent_checks=self.config["max_concurrent_checks"],
                startup_notify=self.config.get("startup_notify", False),
                on_event=self.event_queue.put,
            )
        except (TypeError, ValueError) as exc:
            messagebox.showerror("无法开始监控", str(exc), parent=self.root)
            return
        self._set_running_ui(True)
        self._set_global_status("starting", f"正在初始化 {len(active_entries)} 个主播")

        def run_service() -> None:
            try:
                success = self.service.run() if self.service else False
                self.event_queue.put({"type": "service_finished", "success": success})
            except Exception as exc:
                self.event_queue.put({"type": "fatal_error", "error": str(exc)})

        self.service_thread = threading.Thread(
            target=run_service, name="monitor-gui-service", daemon=True
        )
        self.service_thread.start()

    def _stop_monitoring(self) -> None:
        if self.service is not None:
            self._set_global_status("starting", "正在停止监控")
            self.start_button.configure(state=tk.DISABLED)
            self.service.stop()

    def _set_running_ui(self, running: bool) -> None:
        self.start_button.configure(
            text="停止监控" if running else "开始监控",
            command=self._stop_monitoring if running else self._start_monitoring,
            state=tk.NORMAL,
        )
        edit_state = tk.DISABLED if running else tk.NORMAL
        for widget in (
            self.new_button,
            self.remove_button,
            self.label_entry,
            self.url_entry,
            self.enabled_check,
            self.save_streamer_button,
            self.push_entry,
            self.reveal_check,
            self.test_push_button,
            self.notify_end_check,
            self.startup_check,
            self.daily_check,
            self.save_settings_button,
            *self.setting_inputs,
        ):
            widget.configure(state=edit_state)

    def _set_global_status(self, state: str, detail: str) -> None:
        self.global_status_var.set(status_text(state))
        self.global_detail_var.set(detail)
        color = {
            "live": COLORS["success"],
            "starting": COLORS["accent"],
            "error": COLORS["danger"],
            "suspended": COLORS["warning"],
        }.get(state, COLORS["muted"])
        self.status_dot.configure(fg=color)

    def _test_push(self) -> None:
        if self.testing_push or not self._save_settings(show_success=False):
            return
        if not monitor.is_serverchan_configured(self.config):
            messagebox.showwarning(
                "无法测试推送", "请先填写有效的 Server酱³ 推送地址。", parent=self.root
            )
            return
        self.testing_push = True
        self.test_push_button.configure(state=tk.DISABLED, text="正在测试")

        def verify() -> None:
            try:
                success = monitor._create_notifier(dict(self.config)).verify_connection()
                self.event_queue.put({"type": "push_test", "success": success})
            except Exception as exc:
                self.event_queue.put(
                    {"type": "push_test", "success": False, "error": str(exc)}
                )

        threading.Thread(target=verify, name="monitor-push-test", daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.event_queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        if self.pending_close:
            if not self.service_thread or not self.service_thread.is_alive():
                self.root.destroy()
                return
        delay = 200 if (
            self.testing_push
            or self.pending_close
            or (self.service_thread and self.service_thread.is_alive())
        ) else 750
        self.root.after(delay, self._drain_events)

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        if event_type in {
            "prepared",
            "prepare_error",
            "status",
            "error",
            "suspended",
        }:
            if self.service is not None:
                self.runtime_by_id = {
                    item["id"]: item for item in self.service.snapshot()
                }
                streamer_id = str(event.get("streamer_id") or "")
                if streamer_id:
                    self._refresh_runtime_row(streamer_id)
            runnable = sum(
                not item.get("suspended", False)
                for item in self.runtime_by_id.values()
            )
            live = sum(
                item.get("status") == "live" for item in self.runtime_by_id.values()
            )
            self._set_global_status(
                "live" if live else "starting",
                f"{runnable} 个任务运行，{live} 个直播中",
            )
        elif event_type == "service_finished":
            self._set_running_ui(False)
            if event.get("success"):
                self._set_global_status("stopped", "监控已停止")
            else:
                self._set_global_status("error", "没有可继续运行的主播任务")
            self.service = None
            self.service_thread = None
        elif event_type == "fatal_error":
            self._set_running_ui(False)
            self._set_global_status("error", "监控服务异常结束")
            messagebox.showerror(
                "监控服务异常", str(event.get("error", "未知异常")), parent=self.root
            )
            self.service = None
            self.service_thread = None
        elif event_type == "push_test":
            self.testing_push = False
            monitor_running = bool(
                self.service_thread and self.service_thread.is_alive()
            )
            self.test_push_button.configure(
                state=tk.DISABLED if monitor_running else tk.NORMAL,
                text="测试推送",
            )
            if event.get("success"):
                messagebox.showinfo("测试推送", "测试消息已发送。", parent=self.root)
            else:
                detail = event.get("error") or "请检查推送地址和网络连接。"
                messagebox.showerror("测试推送失败", str(detail), parent=self.root)

    def _on_close(self) -> None:
        if self.service_thread and self.service_thread.is_alive():
            if not messagebox.askyesno(
                "退出程序",
                "监控仍在运行。确定停止全部任务并退出吗？",
                parent=self.root,
            ):
                return
            self.pending_close = True
            self._stop_monitoring()
            return
        self.root.destroy()


def run_gui(config_path: Path | None = None, debug: bool = False) -> None:
    """Create and run the desktop application."""
    root = tk.Tk()
    root.withdraw()
    try:
        MonitorGui(root, config_path or monitor.DEFAULT_CONFIG, debug=debug)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        messagebox.showerror("无法启动程序", str(exc), parent=root)
        root.destroy()
        return
    root.update_idletasks()
    width = max(root.winfo_width(), 1080)
    height = max(root.winfo_height(), 700)
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    y = max(0, (root.winfo_screenheight() - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
