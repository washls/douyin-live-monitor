"""Tkinter desktop interface for the Douyin live monitor."""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
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
from streamer_logging import StreamerLogHandler, StreamerLogStore


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

MAX_STREAMER_LOG_LINES = 500
MAX_PENDING_LOG_EVENTS = 2000
MAX_LOG_EVENTS_PER_TICK = 200

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

BASE_DPI = 96
BASE_WINDOW_SIZE = (1080, 700)
BASE_MIN_WINDOW_SIZE = (900, 600)


class _MonitorInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    )


def calculate_window_metrics(
    work_width: int,
    work_height: int,
    scale: float,
    requested_width: int = 0,
    requested_height: int = 0,
) -> Dict[str, int | bool]:
    """Return bounded DPI-scaled dimensions for the current monitor."""
    safe_scale = min(max(float(scale), 0.75), 4.0)
    work_width = max(1, int(work_width))
    work_height = max(1, int(work_height))
    desired_width = max(round(BASE_WINDOW_SIZE[0] * safe_scale), requested_width)
    desired_height = max(round(BASE_WINDOW_SIZE[1] * safe_scale), requested_height)
    width = min(desired_width, work_width)
    height = min(desired_height, work_height)
    min_width = min(round(BASE_MIN_WINDOW_SIZE[0] * safe_scale), width)
    min_height = min(round(BASE_MIN_WINDOW_SIZE[1] * safe_scale), height)
    effective_width = work_width / safe_scale
    effective_height = work_height / safe_scale
    return {
        "width": width,
        "height": height,
        "min_width": min_width,
        "min_height": min_height,
        "compact": effective_width < 1000 or effective_height < 650,
    }


def enable_windows_high_dpi() -> bool:
    """Enable sharp per-monitor rendering before the first Tk window exists."""
    if sys.platform != "win32":
        return False

    try:
        user32 = ctypes.windll.user32
        get_context = user32.GetThreadDpiAwarenessContext
        get_context.restype = ctypes.c_void_p
        get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        get_awareness.argtypes = [ctypes.c_void_p]
        get_awareness.restype = ctypes.c_int
        current_context = get_context()
        if get_awareness(current_context) == 2:
            return True
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    try:
        set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def get_display_scale(root: tk.Misc) -> float:
    """Read the DPI scale for the monitor that currently owns the root window."""
    if sys.platform == "win32":
        try:
            get_dpi_for_window = ctypes.windll.user32.GetDpiForWindow
            get_dpi_for_window.argtypes = [wintypes.HWND]
            get_dpi_for_window.restype = wintypes.UINT
            dpi = get_dpi_for_window(root.winfo_id())
            if dpi:
                return min(max(dpi / BASE_DPI, 0.75), 4.0)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    try:
        return min(max(float(root.winfo_fpixels("1i")) / BASE_DPI, 0.75), 4.0)
    except (tk.TclError, TypeError, ValueError):
        return 1.0


def get_work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """Return the current monitor's usable work area in physical pixels."""
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            monitor_from_window = user32.MonitorFromWindow
            monitor_from_window.argtypes = [wintypes.HWND, wintypes.DWORD]
            monitor_from_window.restype = wintypes.HANDLE
            get_monitor_info = user32.GetMonitorInfoW
            get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
            get_monitor_info.restype = wintypes.BOOL
            monitor = monitor_from_window(root.winfo_id(), 2)
            info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
            if monitor and get_monitor_info(monitor, ctypes.byref(info)):
                work = info.rcWork
                return (
                    work.left,
                    work.top,
                    work.right - work.left,
                    work.bottom - work.top,
                )
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


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
        self.streamer_logs = StreamerLogStore(max_lines=MAX_STREAMER_LOG_LINES)
        self.streamer_log_queue: queue.Queue[Dict[str, Any]] = queue.Queue(
            maxsize=MAX_PENDING_LOG_EVENTS
        )
        self.streamer_log_handler: StreamerLogHandler | None = None
        self.streamer_log_windows: Dict[str, Dict[str, Any]] = {}

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
        self.display_scale = get_display_scale(self.root)
        self.work_area = get_work_area(self.root)
        initial = calculate_window_metrics(
            self.work_area[2], self.work_area[3], self.display_scale
        )
        self.compact_layout = bool(initial["compact"])
        self.layout_density = 0.82 if self.compact_layout else 1.0
        self.root.geometry(f"{initial['width']}x{initial['height']}")
        self.root.minsize(initial["min_width"], initial["min_height"])
        self.root.configure(background=COLORS["canvas"])
        self.root.option_add("*Font", "{Segoe UI} 10")
        self._app_icon = self._create_app_icon()
        self.root.iconphoto(True, self._app_icon)

    def _px(self, value: int | float) -> int:
        """Scale fixed visual dimensions while preserving compact density."""
        return max(1, round(float(value) * self.display_scale * self.layout_density))

    def show_window(self) -> None:
        """Fit the completed layout inside the current monitor's work area."""
        self.root.update_idletasks()
        work_x, work_y, work_width, work_height = self.work_area
        metrics = calculate_window_metrics(
            work_width,
            work_height,
            self.display_scale,
            self.root.winfo_reqwidth(),
            self.root.winfo_reqheight(),
        )
        width = int(metrics["width"])
        height = int(metrics["height"])
        x = work_x + max(0, (work_width - width) // 2)
        y = work_y + max(0, (work_height - height) // 2)
        self.root.minsize(metrics["min_width"], metrics["min_height"])
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _create_app_icon(self) -> tk.PhotoImage:
        """Create a small flat live-status mark without an external asset."""
        size = max(32, self._px(32))
        icon = tk.PhotoImage(width=size, height=size)
        blue = COLORS["accent"]
        white = "#ffffff"
        red = COLORS["danger"]
        for y in range(size):
            source_y = (y + 0.5) * 32 / size
            if 3 <= source_y < 29:
                for x in range(size):
                    source_x = (x + 0.5) * 32 / size
                    if 3 <= source_x < 29:
                        color = blue
                        distance = (source_x - 16) ** 2 + (source_y - 16) ** 2
                        if 31 <= distance <= 46:
                            color = white
                        elif distance <= 8:
                            color = red
                        icon.put(color, (x, y))
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
            font=("Segoe UI Semibold", 16 if self.compact_layout else 18),
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
        style.configure(
            "Primary.TButton", padding=(self._px(16), self._px(8))
        )
        style.configure("Action.TButton", padding=(self._px(12), self._px(7)))
        style.configure("Treeview", rowheight=self._px(34), font=("Segoe UI", 9))
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

        header = ttk.Frame(
            self.root,
            style="App.TFrame",
            padding=(self._px(24), self._px(18), self._px(24), self._px(14)),
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ttk.Label(header, text="抖音直播监听器", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text=f"v{monitor.APP_VERSION}  ·  多主播监控",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(self._px(3), 0))

        status_box = ttk.Frame(header, style="App.TFrame")
        status_box.grid(
            row=0,
            column=1,
            rowspan=2,
            sticky="e",
            padx=(self._px(16), self._px(18)),
        )
        self.status_dot = tk.Label(
            status_box,
            text="●",
            fg=COLORS["muted"],
            bg=COLORS["canvas"],
            font=("Segoe UI", 12),
        )
        self.status_dot.grid(
            row=0, column=0, rowspan=2, padx=(0, self._px(7))
        )
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
        paned.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=self._px(20),
            pady=(0, self._px(14)),
        )

        sidebar_width = 280 if self.compact_layout else 350
        sidebar = ttk.Frame(
            paned,
            style="Sidebar.TFrame",
            padding=self._px(14),
            width=self._px(sidebar_width),
        )
        content = ttk.Frame(
            paned, style="Surface.TFrame", padding=self._px(14)
        )
        paned.add(sidebar, weight=1)
        paned.add(content, weight=2)
        self._build_sidebar(sidebar)
        self._build_content(content)

        footer = ttk.Frame(
            self.root,
            style="App.TFrame",
            padding=(self._px(22), 0, self._px(22), self._px(12)),
        )
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
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, self._px(10)))
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
        self.streamer_tree.heading("status", text="状态", anchor=tk.CENTER)
        self.streamer_tree.heading("name", text="主播", anchor=tk.CENTER)
        self.streamer_tree.column(
            "status",
            width=self._px(76),
            minwidth=self._px(70),
            stretch=False,
            anchor=tk.CENTER,
        )
        self.streamer_tree.column(
            "name",
            width=self._px(220),
            minwidth=self._px(120),
            stretch=True,
            anchor=tk.CENTER,
        )
        self.streamer_tree.grid(row=1, column=0, sticky="nsew")
        self.streamer_tree.bind("<<TreeviewSelect>>", self._on_streamer_selected)
        sidebar_scroll = ttk.Scrollbar(
            parent, orient=tk.VERTICAL, command=self.streamer_tree.yview
        )
        sidebar_scroll.grid(row=1, column=1, sticky="ns")
        self.streamer_tree.configure(yscrollcommand=sidebar_scroll.set)

        actions = ttk.Frame(parent, style="Sidebar.TFrame")
        actions.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(self._px(12), 0),
        )
        actions.grid_columnconfigure((0, 1), weight=1)
        self.new_button = ttk.Button(
            actions, text="新增主播", style="Action.TButton", command=self._new_streamer
        )
        self.new_button.grid(
            row=0, column=0, sticky="ew", padx=(0, self._px(5))
        )
        self.remove_button = ttk.Button(
            actions,
            text="删除",
            style="Action.TButton",
            command=self._remove_selected_streamer,
        )
        self.remove_button.grid(
            row=0, column=1, sticky="ew", padx=(self._px(5), 0)
        )

    def _build_content(self, parent: ttk.Frame) -> None:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        status_tab = ttk.Frame(
            self.notebook, style="Surface.TFrame", padding=self._px(16)
        )
        detail_tab = ttk.Frame(
            self.notebook, style="Surface.TFrame", padding=self._px(16)
        )
        settings_tab = ttk.Frame(self.notebook, style="Surface.TFrame")
        self.notebook.add(status_tab, text="运行状态")
        self.notebook.add(detail_tab, text="主播详情")
        self.notebook.add(settings_tab, text="监控设置")
        self._build_status_tab(status_tab)
        self._build_detail_tab(detail_tab)
        self._build_scrollable_settings_tab(settings_tab)

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
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(self._px(4), self._px(12)),
        )
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
            self.status_tree.heading(column, text=label, anchor=tk.CENTER)
        self.status_tree.column(
            "status", width=self._px(82), stretch=False, anchor=tk.CENTER
        )
        self.status_tree.column(
            "name", width=self._px(150), minwidth=self._px(110), anchor=tk.CENTER
        )
        self.status_tree.column(
            "last_check", width=self._px(112), stretch=False, anchor=tk.CENTER
        )
        self.status_tree.column(
            "detail", width=self._px(250), minwidth=self._px(140), anchor=tk.CENTER
        )
        self.status_tree.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.status_tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        horizontal_scroll = ttk.Scrollbar(
            parent, orient=tk.HORIZONTAL, command=self.status_tree.xview
        )
        horizontal_scroll.grid(row=3, column=0, sticky="ew")
        self.status_tree.configure(
            yscrollcommand=scroll.set, xscrollcommand=horizontal_scroll.set
        )
        self.status_tree.bind("<Double-1>", self._on_status_tree_double_click)
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
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(self._px(4), self._px(18)),
        )
        form = ttk.LabelFrame(
            parent,
            text="任务配置",
            style="Surface.TLabelframe",
            padding=self._px(16),
        )
        form.grid(row=2, column=0, sticky="ew")
        form.grid_columnconfigure(1, weight=1)
        ttk.Label(form, text="本地 ID").grid(
            row=0, column=0, sticky="w", pady=self._px(7)
        )
        ttk.Entry(form, textvariable=self.streamer_id_var, state="readonly").grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(self._px(14), 0),
            pady=self._px(7),
        )
        ttk.Label(form, text="显示名称").grid(
            row=1, column=0, sticky="w", pady=self._px(7)
        )
        self.label_entry = ttk.Entry(form, textvariable=self.streamer_label_var)
        self.label_entry.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(self._px(14), 0),
            pady=self._px(7),
        )
        ttk.Label(form, text="抖音链接").grid(
            row=2, column=0, sticky="w", pady=self._px(7)
        )
        self.url_entry = ttk.Entry(form, textvariable=self.streamer_url_var)
        self.url_entry.grid(
            row=2,
            column=1,
            sticky="ew",
            padx=(self._px(14), 0),
            pady=self._px(7),
        )
        self.enabled_check = ttk.Checkbutton(
            form, text="启用此主播", variable=self.streamer_enabled_var
        )
        self.enabled_check.grid(
            row=3,
            column=1,
            sticky="w",
            padx=(self._px(14), 0),
            pady=(self._px(8), self._px(4)),
        )
        action_row = ttk.Frame(parent, style="Surface.TFrame")
        action_row.grid(
            row=3, column=0, sticky="e", pady=(self._px(16), 0)
        )
        self.save_streamer_button = ttk.Button(
            action_row,
            text="保存主播",
            style="Primary.TButton",
            command=self._save_streamer,
        )
        self.save_streamer_button.grid(row=0, column=0)

    def _build_scrollable_settings_tab(self, parent: ttk.Frame) -> None:
        """Keep all settings reachable on short or heavily scaled displays."""
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(
            parent,
            background=COLORS["surface"],
            highlightthickness=0,
            borderwidth=0,
        )
        settings_scroll = ttk.Scrollbar(
            parent, orient=tk.VERTICAL, command=self.settings_canvas.yview
        )
        self.settings_canvas.configure(yscrollcommand=settings_scroll.set)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scroll.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(
            self.settings_canvas,
            style="Surface.TFrame",
            padding=self._px(16),
        )
        inner_window = self.settings_canvas.create_window(
            (0, 0), window=inner, anchor="nw"
        )
        inner.bind(
            "<Configure>",
            lambda _event: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all")
            ),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda event: self.settings_canvas.itemconfigure(
                inner_window, width=event.width
            ),
        )
        self._build_settings_tab(inner)

        def bind_mousewheel(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", self._scroll_settings, add="+")
            for child in widget.winfo_children():
                bind_mousewheel(child)

        bind_mousewheel(inner)

    def _scroll_settings(self, event: tk.Event) -> str | None:
        bounds = self.settings_canvas.bbox("all")
        if bounds and bounds[3] > self.settings_canvas.winfo_height():
            direction = -1 if event.delta > 0 else 1
            self.settings_canvas.yview_scroll(direction, "units")
            return "break"
        return None

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="监控设置", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="保存后在下一次启动监控时生效。推送地址仅保存在本机。",
            style="Muted.TLabel",
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(self._px(4), self._px(14)),
        )

        push_frame = ttk.LabelFrame(
            parent,
            text="Server酱³ 推送",
            style="Surface.TLabelframe",
            padding=self._px(14),
        )
        push_frame.grid(row=2, column=0, sticky="ew")
        push_frame.grid_columnconfigure(0, weight=1)
        self.push_entry = ttk.Entry(
            push_frame, textvariable=self.push_url_var, show="●"
        )
        self.push_entry.grid(
            row=0, column=0, sticky="ew", padx=(0, self._px(10))
        )
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
        self.test_push_button.grid(
            row=0, column=2, padx=(self._px(10), 0)
        )

        timing = ttk.LabelFrame(
            parent,
            text="检测与提醒",
            style="Surface.TLabelframe",
            padding=self._px(14),
        )
        timing.grid(row=3, column=0, sticky="ew", pady=(self._px(14), 0))
        timing.grid_columnconfigure(1, weight=1)
        if not self.compact_layout:
            timing.grid_columnconfigure(3, weight=1)
        self.setting_inputs = []
        if self.compact_layout:
            fields = (
                ("检测间隔（秒）", self.check_interval_var, 0, 0),
                ("重复提醒间隔（秒）", self.repeat_interval_var, 1, 0),
                ("最多重复提醒", self.max_repeat_var, 2, 0),
                ("并发检测数", self.max_concurrent_var, 3, 0),
            )
        else:
            fields = (
                ("检测间隔（秒）", self.check_interval_var, 0, 0),
                ("重复提醒间隔（秒）", self.repeat_interval_var, 0, 2),
                ("最多重复提醒", self.max_repeat_var, 1, 0),
                ("并发检测数", self.max_concurrent_var, 1, 2),
            )
        for label, variable, row, column in fields:
            ttk.Label(timing, text=label).grid(
                row=row,
                column=column,
                sticky="w",
                pady=self._px(7),
                padx=(0, self._px(10)),
            )
            entry = ttk.Entry(timing, textvariable=variable, width=12)
            entry.grid(
                row=row,
                column=column + 1,
                sticky="ew",
                pady=self._px(7),
                padx=(0, self._px(20)),
            )
            self.setting_inputs.append(entry)

        options = ttk.Frame(parent, style="Surface.TFrame")
        options.grid(row=4, column=0, sticky="ew", pady=(self._px(16), 0))
        self.notify_end_check = ttk.Checkbutton(
            options, text="下播时通知", variable=self.notify_end_var
        )
        self.notify_end_check.grid(
            row=0, column=0, sticky="w", pady=self._px(4)
        )
        self.startup_check = ttk.Checkbutton(
            options, text="启动时发送汇总通知", variable=self.startup_notify_var
        )
        self.startup_check.grid(
            row=1, column=0, sticky="w", pady=self._px(4)
        )
        self.daily_check = ttk.Checkbutton(
            options, text="启用每日亲密度提醒", variable=self.daily_reminder_var
        )
        self.daily_check.grid(
            row=2, column=0, sticky="w", pady=self._px(4)
        )
        self.save_settings_button = ttk.Button(
            parent,
            text="保存设置",
            style="Primary.TButton",
            command=self._save_settings,
        )
        self.save_settings_button.grid(
            row=5, column=0, sticky="e", pady=(self._px(16), 0)
        )

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

    def _on_status_tree_double_click(self, event) -> None:
        streamer_id = self.status_tree.identify_row(event.y)
        if not streamer_id:
            return
        self.status_tree.selection_set(streamer_id)
        self.status_tree.focus(streamer_id)
        self._show_streamer_log(streamer_id)

    def _show_streamer_log(self, streamer_id: str) -> None:
        existing = self.streamer_log_windows.get(streamer_id)
        if existing and existing["window"].winfo_exists():
            existing["window"].deiconify()
            existing["window"].lift()
            existing["window"].focus_force()
            return

        entry = next(
            (item for item in self.entries if item["id"] == streamer_id), None
        )
        if entry is None:
            return
        runtime = self.runtime_by_id.get(streamer_id, {})
        name = compact_ui_text(
            runtime.get("nickname") or entry.get("label") or "未命名主播",
            80,
        )

        window = tk.Toplevel(self.root)
        window.title(f"{name} · 实时运行日志")
        window.configure(background=COLORS["surface"])
        width = min(self._px(780), max(self._px(520), self.root.winfo_width()))
        height = min(self._px(520), max(self._px(360), self.root.winfo_height()))
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(self._px(520), self._px(340))

        content = ttk.Frame(
            window, style="Surface.TFrame", padding=self._px(16)
        )
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            content,
            text=name,
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            content,
            text="当前监控会话日志，停止后仍可查看。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(self._px(3), self._px(10)))

        text = ScrolledText(
            content,
            wrap=tk.WORD,
            font=("Consolas", 9),
            background="#fbfbfb",
            foreground=COLORS["text"],
            borderwidth=1,
            relief=tk.SOLID,
            padx=self._px(10),
            pady=self._px(8),
        )
        text.pack(fill=tk.BOTH, expand=True)
        self.streamer_log_windows[streamer_id] = {
            "window": window,
            "text": text,
            "has_logs": False,
        }

        def close_window() -> None:
            self.streamer_log_windows.pop(streamer_id, None)
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_window)
        self._render_streamer_log_window(streamer_id)

    def _render_streamer_log_window(self, streamer_id: str) -> None:
        log_window = self.streamer_log_windows.get(streamer_id)
        if not log_window:
            return
        lines = self.streamer_logs.get(streamer_id)
        text = log_window["text"]
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        if lines:
            text.insert(tk.END, "\n".join(lines) + "\n")
        else:
            text.insert(tk.END, "当前会话暂无日志。监控开始后将在这里实时显示。\n")
        text.configure(state=tk.DISABLED)
        text.see(tk.END)
        log_window["has_logs"] = bool(lines)

    def _append_streamer_log(self, streamer_id: str, message: Any) -> None:
        was_full = (
            self.streamer_logs.count(streamer_id) >= self.streamer_logs.max_lines
        )
        line = self.streamer_logs.append(streamer_id, message)
        log_window = self.streamer_log_windows.get(streamer_id)
        if not log_window:
            return
        text = log_window["text"]
        text.configure(state=tk.NORMAL)
        if not log_window["has_logs"]:
            text.delete("1.0", tk.END)
            log_window["has_logs"] = True
        elif was_full:
            text.delete("1.0", "2.0")
        text.insert(tk.END, line + "\n")
        text.configure(state=tk.DISABLED)
        text.see(tk.END)

    def _attach_streamer_log_handler(self) -> None:
        self._detach_streamer_log_handler()
        self.streamer_log_handler = StreamerLogHandler(
            self._enqueue_streamer_log_event
        )
        logging.getLogger().addHandler(self.streamer_log_handler)

    def _enqueue_streamer_log_event(self, event: Dict[str, Any]) -> None:
        try:
            self.streamer_log_queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self.streamer_log_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.streamer_log_queue.put_nowait(event)
        except queue.Full:
            pass

    def _detach_streamer_log_handler(self) -> None:
        if self.streamer_log_handler is None:
            return
        logging.getLogger().removeHandler(self.streamer_log_handler)
        self.streamer_log_handler.close()
        self.streamer_log_handler = None

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
        self.streamer_logs.clear()
        self.streamer_log_queue = queue.Queue(maxsize=MAX_PENDING_LOG_EVENTS)
        for streamer_id in list(self.streamer_log_windows):
            self._render_streamer_log_window(streamer_id)
        self._attach_streamer_log_handler()
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
        for _ in range(MAX_LOG_EVENTS_PER_TICK):
            try:
                event = self.streamer_log_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
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
        if event_type == "log":
            streamer_id = str(event.get("streamer_id") or "")
            if streamer_id:
                self._append_streamer_log(streamer_id, event.get("message", ""))
        elif event_type in {
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
            self._detach_streamer_log_handler()
            self._set_running_ui(False)
            if event.get("success"):
                self.runtime_by_id = {}
                self._refresh_streamer_views()
                self._set_global_status("stopped", "监控已停止")
            else:
                self._set_global_status("error", "没有可继续运行的主播任务")
            self.service = None
            self.service_thread = None
        elif event_type == "fatal_error":
            self._detach_streamer_log_handler()
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
        self._detach_streamer_log_handler()
        self.root.destroy()


def run_gui(config_path: Path | None = None, debug: bool = False) -> None:
    """Create and run the desktop application."""
    enable_windows_high_dpi()
    root = tk.Tk()
    root.withdraw()
    try:
        app = MonitorGui(root, config_path or monitor.DEFAULT_CONFIG, debug=debug)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        messagebox.showerror("无法启动程序", str(exc), parent=root)
        root.destroy()
        return
    app.show_window()
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
