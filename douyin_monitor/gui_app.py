"""Tkinter desktop interface for the Douyin live monitor."""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import sys
import threading
import tkinter as tk
import webbrowser
import uuid
from copy import deepcopy
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable, Dict, Mapping, Optional, Union

import monitor
from .app_icon import get_app_icon_path
from .application import (
    create_monitor_service,
    create_notifier,
    load_streamer_entries,
)
from .monitor_service import MonitorService
from .notifier import ServerChanNotifier
from .platform_clients import infer_platform, platform_name, validate_platform_url
from .project_info import PROJECT_REPOSITORY_URL, PROJECT_SOURCE_NOTICE
from .push_config import (
    CHANNEL_FIELDS,
    CHANNEL_TYPES,
    has_push_targets,
    normalize_channel,
    normalize_push_config,
    selected_channels,
)
from .streamer_config import (
    add_streamer,
    enabled_streamers,
    normalize_app_config,
    remove_streamer,
    save_config_atomic,
    update_streamer,
)
from .streamer_logging import StreamerLogHandler, StreamerLogStore
from .tray_controller import TrayController
from .windows_integration import (
    IS_WINDOWS,
    WindowsInstanceGuard,
    get_autostart_snapshot,
    is_autostart_enabled,
    restore_autostart_value,
    set_autostart_enabled,
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

MAX_STREAMER_LOG_LINES = 500
MAX_PENDING_LOG_EVENTS = 2000
MAX_LOG_EVENTS_PER_TICK = 200

STATUS_TEXT = {
    "pending": "待启动",
    "retrying": "等待重试",
    "unknown": "待检测",
    "starting": "启动中",
    "live": "直播中",
    "offline": "未开播",
    "error": "异常",
    "restricted": "访问受限",
    "suspended": "已暂停",
    "stopped": "已停止",
}

CLOSE_ACTION_LABELS = {
    "exit": "退出主程序",
    "hide_to_tray": "隐藏到任务栏托盘",
}
CLOSE_ACTION_VALUES = {label: value for value, label in CLOSE_ACTION_LABELS.items()}

BASE_DPI = 96
BASE_WINDOW_SIZE = (1080, 700)
BASE_MIN_WINDOW_SIZE = (900, 600)
TRAY_READY_TIMEOUT_MS = 5000

PLATFORM_LINK_EXAMPLES = (
    "抖音：www.douyin.com/user/主播ID 或 v.douyin.com/分享码\n"
    "B站：live.bilibili.com/房间号\n"
    "虎牙：www.huya.com/房间号或别名\n"
    "斗鱼：www.douyu.com/房间号或别名\n"
    "快手：live.kuaishou.com/u/主播ID（实验支持）\n"
    "视频号：当前不支持免登录监控"
)


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


def gui_platform_name(platform: str) -> str:
    """Include the experimental support level wherever a platform is shown."""
    name = platform_name(platform)
    return f"{name}（实验）" if platform == "kuaishou" else name


def compact_ui_text(value: Any, limit: int) -> str:
    """Collapse untrusted remote text into one bounded table-cell value."""
    return " ".join(str(value or "").split())[:limit].rstrip()


def open_project_repository() -> bool:
    """Open the fixed public project address in the user's browser."""
    try:
        return bool(webbrowser.open_new_tab(PROJECT_REPOSITORY_URL))
    except (OSError, webbrowser.Error):
        return False


def validate_gui_settings(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize settings collected from form controls."""
    validated = normalize_app_config(values)
    normalized = {
        key: validated[key]
        for key in (
            "check_interval",
            "repeat_notify_interval",
            "max_repeat_notifications",
            "max_concurrent_checks",
        )
    }

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
        normalized[key] = validated[key]
    normalized["close_action"] = validated["close_action"]
    return normalized


class MonitorGui:
    """Windows-oriented light desktop interface."""

    def __init__(
        self,
        root: tk.Tk,
        config_path: Path,
        debug: bool = False,
        autostart: bool = False,
        instance_guard: Optional[WindowsInstanceGuard] = None,
        tray_factory: Any = TrayController,
    ):
        self.root = root
        self.config_path = Path(config_path)
        self.debug = debug
        self.autostart_requested = bool(autostart)
        self.instance_guard = instance_guard
        self.tray_factory = tray_factory
        self.config = monitor.load_config(self.config_path)
        self.entries = load_streamer_entries(
            self.config_path, self.config, monitor.load_monitor_state()
        )
        self.service: Optional[MonitorService] = None
        self.service_thread: Optional[threading.Thread] = None
        self.event_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self.pending_close = False
        self.exiting = False
        self.runtime_closed = False
        self.testing_push = False
        self.push_test_thread: Optional[threading.Thread] = None
        self.push_test_notifier: Any = None
        self.push_test_cancel = threading.Event()
        self.channel_editor: Optional[tk.Toplevel] = None
        self.push_channels = deepcopy(self.config.get("channels", []))
        self.push_selected_ids = list(self.config.get("selected_channel_ids", []))
        self.push_results: Dict[str, str] = {}
        self.monitoring_ui_active = False
        self.selected_streamer_id = ""
        self.runtime_by_id: Dict[str, Dict[str, Any]] = {}
        self.streamer_logs = StreamerLogStore(max_lines=MAX_STREAMER_LOG_LINES)
        self.streamer_log_queue: queue.Queue[Dict[str, Any]] = queue.Queue(
            maxsize=MAX_PENDING_LOG_EVENTS
        )
        self.streamer_log_handler: Optional[StreamerLogHandler] = None
        self.streamer_log_windows: Dict[str, Dict[str, Any]] = {}
        self.tray_controller: Optional[TrayController] = None
        self.tray_available = False
        self.tray_error = ""
        self.tray_failure_reported = False
        self.tray_disabled_for_session = False

        self._configure_window()
        self._configure_styles()
        self._create_variables()
        self._build_layout()
        self._refresh_streamer_views()
        self._load_settings_into_form()
        self._set_global_status("stopped", "监控未启动")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_tray()
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

    def _px(self, value: Union[int, float]) -> int:
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

    def _restore_window(self) -> None:
        """Restore and focus the existing Tk window from the tray or mutex event."""
        self.show_window()
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _start_tray(self) -> None:
        if not IS_WINDOWS:
            self.tray_error = "任务栏托盘仅支持 Windows"
            return
        try:
            self.tray_controller = self.tray_factory(self.event_queue.put)
            if not self.tray_controller.start():
                self.tray_error = "无法启动任务栏托盘"
                self.event_queue.put(
                    {"type": "tray_failed", "error": self.tray_error}
                )
            else:
                self.root.after(
                    TRAY_READY_TIMEOUT_MS, self._check_tray_ready
                )
        except Exception as exc:
            self.tray_error = str(exc)
            self.event_queue.put({"type": "tray_failed", "error": str(exc)})

    def _sync_tray_state(self) -> None:
        if self.tray_controller is None or self.tray_disabled_for_session:
            return
        live_count = 0
        if self.monitoring_ui_active:
            live_count = sum(
                item.get("status") == "live" for item in self.runtime_by_id.values()
            )
        self.tray_controller.update_state(self.monitoring_ui_active, live_count)

    def _check_tray_ready(self) -> None:
        if (
            self.tray_available
            or self.tray_disabled_for_session
            or self.exiting
            or self.runtime_closed
        ):
            return
        self.tray_disabled_for_session = True
        if self.tray_controller is not None:
            self.tray_controller.stop(wait=False)
        self.event_queue.put(
            {
                "type": "tray_failed",
                "error": "任务栏托盘初始化超时",
            }
        )

    def _create_app_icon(self) -> tk.PhotoImage:
        """Load the shared application artwork for Tk windows and taskbar."""
        return tk.PhotoImage(file=str(get_app_icon_path()))

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
        self.streamer_platform_var = tk.StringVar(value="输入链接后自动识别")
        self.streamer_enabled_var = tk.BooleanVar(value=True)
        self.push_mode_var = tk.StringVar(value=self.config.get("push_mode", "all"))
        self.push_count_var = tk.StringVar()
        self.push_test_result_var = tk.StringVar()
        self.check_interval_var = tk.StringVar()
        self.repeat_interval_var = tk.StringVar()
        self.max_repeat_var = tk.StringVar()
        self.max_concurrent_var = tk.StringVar()
        self.notify_end_var = tk.BooleanVar()
        self.startup_notify_var = tk.BooleanVar()
        self.daily_reminder_var = tk.BooleanVar()
        self.close_action_var = tk.StringVar()
        self.autostart_var = tk.BooleanVar(value=False)

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
            text=f"v{monitor.APP_VERSION}  ·  多平台主播监控",
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
        source_notice = ttk.Frame(footer, style="App.TFrame")
        source_notice.grid(
            row=0,
            column=0,
        )
        ttk.Label(
            source_notice,
            text=f"{PROJECT_SOURCE_NOTICE} · 项目地址：",
            style="Subtitle.TLabel",
        ).grid(row=0, column=0, sticky="e")
        self.project_link = tk.Label(
            source_notice,
            text=PROJECT_REPOSITORY_URL,
            fg=COLORS["accent"],
            bg=COLORS["canvas"],
            activeforeground=COLORS["accent"],
            activebackground=COLORS["canvas"],
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
            borderwidth=0,
            highlightthickness=0,
            takefocus=True,
        )
        self.project_link.grid(row=0, column=1, sticky="w")
        self.project_link.bind("<Button-1>", self._open_project_repository)
        self.project_link.bind("<Return>", self._open_project_repository)
        self.project_link.bind("<space>", self._open_project_repository)

    def _open_project_repository(self, _event: Any = None) -> None:
        if not open_project_repository():
            messagebox.showerror(
                "无法打开项目地址",
                f"请在浏览器中访问：\n{PROJECT_REPOSITORY_URL}",
                parent=self.root,
            )

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
            columns=("status", "platform", "name"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.streamer_tree.heading("status", text="状态", anchor=tk.CENTER)
        self.streamer_tree.heading("platform", text="平台", anchor=tk.CENTER)
        self.streamer_tree.heading("name", text="主播", anchor=tk.CENTER)
        self.streamer_tree.column(
            "status",
            width=self._px(76),
            minwidth=self._px(70),
            stretch=False,
            anchor=tk.CENTER,
        )
        self.streamer_tree.column(
            "platform",
            width=self._px(94),
            minwidth=self._px(80),
            stretch=False,
            anchor=tk.CENTER,
        )
        self.streamer_tree.column(
            "name",
            width=self._px(128),
            minwidth=self._px(86),
            stretch=True,
            anchor=tk.CENTER,
        )
        self.streamer_tree.grid(row=1, column=0, sticky="nsew")
        self.streamer_tree.bind("<<TreeviewSelect>>", self._on_streamer_selected)
        self.streamer_tree.bind("<Configure>", self._fit_streamer_columns)
        sidebar_scroll = ttk.Scrollbar(
            parent, orient=tk.VERTICAL, command=self.streamer_tree.yview
        )
        sidebar_scroll.grid(row=1, column=1, sticky="ns")
        sidebar_horizontal_scroll = ttk.Scrollbar(
            parent, orient=tk.HORIZONTAL, command=self.streamer_tree.xview
        )
        sidebar_horizontal_scroll.grid(row=2, column=0, sticky="ew")
        self.streamer_tree.configure(
            yscrollcommand=sidebar_scroll.set,
            xscrollcommand=sidebar_horizontal_scroll.set,
        )

        actions = ttk.Frame(parent, style="Sidebar.TFrame")
        actions.grid(
            row=3,
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

    def _fit_streamer_columns(self, event: Any) -> None:
        """Keep all three identities visible as the sidebar narrows."""
        available = max(3, int(event.width) - 4)
        preferred = tuple(self._px(width) for width in (76, 94, 128))
        if available >= sum(preferred):
            status_width, platform_width = preferred[:2]
        else:
            status_width = max(1, int(available * preferred[0] / sum(preferred)))
            platform_width = max(1, int(available * preferred[1] / sum(preferred)))
        name_width = max(1, available - status_width - platform_width)
        for column, width in zip(
            ("status", "platform", "name"),
            (status_width, platform_width, name_width),
        ):
            self.streamer_tree.column(column, width=width, minwidth=1)

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
        push_tab = ttk.Frame(self.notebook, style="Surface.TFrame")
        self.notebook.add(status_tab, text="运行状态")
        self.notebook.add(detail_tab, text="主播详情")
        self.notebook.add(settings_tab, text="监控设置")
        self.notebook.add(push_tab, text="推送设置")
        self._build_status_tab(status_tab)
        self.detail_canvas = self._build_scrollable_form(
            detail_tab, self._build_detail_tab, padding=0
        )
        self._build_scrollable_settings_tab(settings_tab)
        self.push_canvas = self._build_scrollable_form(
            push_tab, self._build_push_tab, padding=self._px(16)
        )
        self._refresh_push_channels()

    def _build_status_tab(self, parent: ttk.Frame) -> None:
        parent.grid_rowconfigure(2, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="任务状态", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="各平台独立检测与恢复，访问受限时等待重试。",
            style="Muted.TLabel",
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(self._px(4), self._px(12)),
        )
        self.status_tree = ttk.Treeview(
            parent,
            columns=("status", "platform", "name", "last_check", "detail"),
            show="headings",
        )
        for column, label in (
            ("status", "状态"),
            ("platform", "平台"),
            ("name", "主播"),
            ("last_check", "最后检测"),
            ("detail", "详情"),
        ):
            self.status_tree.heading(column, text=label, anchor=tk.CENTER)
        self.status_tree.column(
            "status", width=self._px(82), stretch=False, anchor=tk.CENTER
        )
        self.status_tree.column(
            "platform", width=self._px(96), stretch=False, anchor=tk.CENTER
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
        self.status_tree.bind(
            "<<TreeviewSelect>>", self._on_status_selection_changed
        )
        self.status_tree.tag_configure("live", foreground=COLORS["success"])
        self.status_tree.tag_configure("error", foreground=COLORS["danger"])
        self.status_tree.tag_configure("restricted", foreground=COLORS["warning"])
        self.status_tree.tag_configure("suspended", foreground=COLORS["warning"])

        status_actions = ttk.Frame(parent, style="Surface.TFrame")
        status_actions.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(self._px(12), 0),
        )
        status_actions.grid_columnconfigure(0, weight=1)
        ttk.Label(
            status_actions,
            text="选择一个任务，可只停止该主播的本次监控。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.stop_selected_button = ttk.Button(
            status_actions,
            text="停止所选主播",
            style="Action.TButton",
            command=self._stop_selected_streamer,
            state=tk.DISABLED,
        )
        self.stop_selected_button.grid(row=0, column=1, sticky="e")

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
        ttk.Label(form, text="主播链接").grid(
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
        ttk.Label(form, text="平台").grid(
            row=3, column=0, sticky="w", pady=self._px(7)
        )
        self.streamer_platform_label = ttk.Label(
            form,
            textvariable=self.streamer_platform_var,
            style="Muted.TLabel",
        )
        self.streamer_platform_label.grid(
            row=3,
            column=1,
            sticky="ew",
            padx=(self._px(14), 0),
            pady=self._px(7),
        )
        self.streamer_url_var.trace_add("write", self._update_streamer_platform)
        self.enabled_check = ttk.Checkbutton(
            form, text="启用此主播", variable=self.streamer_enabled_var
        )
        self.enabled_check.grid(
            row=4,
            column=1,
            sticky="w",
            padx=(self._px(14), 0),
            pady=(self._px(8), self._px(4)),
        )
        self.platform_support_label = ttk.Label(
            parent,
            text=PLATFORM_LINK_EXAMPLES,
            style="Muted.TLabel",
            justify=tk.LEFT,
            wraplength=self._px(560),
        )
        self.platform_support_label.grid(
            row=3, column=0, sticky="ew", pady=(self._px(12), 0)
        )
        parent.bind(
            "<Configure>",
            lambda event: self.platform_support_label.configure(
                wraplength=max(1, event.width)
            ),
            add="+",
        )
        action_row = ttk.Frame(parent, style="Surface.TFrame")
        action_row.grid(
            row=4, column=0, sticky="e", pady=(self._px(12), 0)
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
        self.settings_canvas = self._build_scrollable_form(
            parent, self._build_settings_tab, padding=self._px(16)
        )

    def _build_scrollable_form(
        self,
        parent: ttk.Frame,
        build_content: Callable[[ttk.Frame], None],
        padding: int,
    ) -> tk.Canvas:
        """Share the existing form scroll behavior across details and settings."""
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(
            parent,
            background=COLORS["surface"],
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(
            parent, orient=tk.VERTICAL, command=canvas.yview
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(
            canvas,
            style="Surface.TFrame",
            padding=padding,
        )
        inner_window = canvas.create_window(
            (0, 0), window=inner, anchor="nw"
        )
        inner.bind(
            "<Configure>",
            lambda _event: canvas.configure(
                scrollregion=canvas.bbox("all")
            ),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(
                inner_window, width=event.width
            ),
        )
        build_content(inner)

        def reveal_focused_control(event: tk.Event) -> None:
            bounds = canvas.bbox("all")
            if not bounds or bounds[3] <= canvas.winfo_height():
                return
            y = event.widget.winfo_rooty() - inner.winfo_rooty()
            bottom = y + event.widget.winfo_height()
            visible_top = canvas.canvasy(0)
            if y < visible_top:
                canvas.yview_moveto(y / bounds[3])
            elif bottom > visible_top + canvas.winfo_height():
                canvas.yview_moveto((bottom - canvas.winfo_height()) / bounds[3])

        def bind_scroll_controls(widget: tk.Misc) -> None:
            widget.bind(
                "<MouseWheel>",
                lambda event: self._scroll_form(event, canvas),
                add="+",
            )
            widget.bind("<FocusIn>", reveal_focused_control, add="+")
            for child in widget.winfo_children():
                bind_scroll_controls(child)

        bind_scroll_controls(inner)
        return canvas

    def _scroll_settings(self, event: tk.Event) -> Optional[str]:
        return self._scroll_form(event, self.settings_canvas)

    def _scroll_form(self, event: tk.Event, canvas: tk.Canvas) -> Optional[str]:
        bounds = canvas.bbox("all")
        if bounds and bounds[3] > canvas.winfo_height():
            direction = -1 if event.delta > 0 else 1
            canvas.yview_scroll(direction, "units")
            return "break"
        return None

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="监控设置", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="监控参数下次启动监控时生效，窗口与系统设置保存后立即生效。",
            style="Muted.TLabel",
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(self._px(4), self._px(14)),
        )

        timing = ttk.LabelFrame(
            parent,
            text="检测与提醒",
            style="Surface.TLabelframe",
            padding=self._px(14),
        )
        timing.grid(row=2, column=0, sticky="ew")
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

        ttk.Label(
            timing,
            text="B站、虎牙和斗鱼至少 30 秒，快手至少 60 秒；同平台依次检测。",
            style="Muted.TLabel",
            wraplength=self._px(520),
        ).grid(
            row=4 if self.compact_layout else 2,
            column=0,
            columnspan=2 if self.compact_layout else 4,
            sticky="w",
            pady=(self._px(6), 0),
        )

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
            options, text="启用抖音每日亲密度提醒", variable=self.daily_reminder_var
        )
        self.daily_check.grid(
            row=2, column=0, sticky="w", pady=self._px(4)
        )

        system_frame = ttk.LabelFrame(
            parent,
            text="窗口与系统",
            style="Surface.TLabelframe",
            padding=self._px(14),
        )
        system_frame.grid(row=5, column=0, sticky="ew", pady=(self._px(14), 0))
        system_frame.grid_columnconfigure(1, weight=1)
        ttk.Label(system_frame, text="关闭窗口时").grid(
            row=0, column=0, sticky="w", pady=self._px(6)
        )
        self.close_action_combo = ttk.Combobox(
            system_frame,
            textvariable=self.close_action_var,
            values=tuple(CLOSE_ACTION_LABELS.values()),
            state="readonly" if IS_WINDOWS else tk.DISABLED,
            width=22,
        )
        self.close_action_combo.grid(
            row=0,
            column=1,
            sticky="e",
            padx=(self._px(14), 0),
            pady=self._px(6),
        )
        self.autostart_check = ttk.Checkbutton(
            system_frame,
            text="Windows 开机自启",
            variable=self.autostart_var,
            state=tk.NORMAL if IS_WINDOWS else tk.DISABLED,
        )
        self.autostart_check.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(self._px(7), self._px(2)),
        )
        ttk.Label(
            system_frame,
            text="登录 Windows 后静默进入托盘，并自动监控所有已启用主播。",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        self.save_settings_button = ttk.Button(
            parent,
            text="保存设置",
            style="Primary.TButton",
            command=self._save_settings,
        )
        self.save_settings_button.grid(
            row=6, column=0, sticky="e", pady=(self._px(16), 0)
        )

    def _build_push_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        ttk.Label(parent, text="推送设置", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="管理接收通知的渠道，推送范围在下次启动监控时生效。",
            style="Muted.TLabel",
            wraplength=self._px(560),
        ).grid(row=1, column=0, sticky="w", pady=(self._px(4), self._px(14)))
        modes = ttk.Frame(parent, style="Surface.TFrame")
        modes.grid(row=2, column=0, sticky="ew")
        self.push_mode_buttons = []
        for column, (value, label) in enumerate(
            (("all", "全部渠道"), ("selected", "自选渠道"))
        ):
            button = ttk.Radiobutton(
                modes, text=label, variable=self.push_mode_var, value=value,
                command=self._refresh_push_channels,
            )
            button.grid(row=0, column=column, sticky="w", padx=(0, self._px(18)))
            self.push_mode_buttons.append(button)
        ttk.Label(
            parent, textvariable=self.push_count_var, style="Muted.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=self._px(8))
        table = ttk.Frame(parent, style="Surface.TFrame")
        table.grid(row=4, column=0, sticky="nsew")
        table.grid_columnconfigure(0, weight=1)
        self.push_tree = ttk.Treeview(
            table, columns=("included", "name", "type", "result"),
            show="headings", selectmode="browse", height=6,
        )
        for column, label, width in (
            ("included", "发送", 54), ("name", "渠道名称", 155),
            ("type", "渠道类型", 110), ("result", "最近结果", 180),
        ):
            self.push_tree.heading(column, text=label, anchor=tk.CENTER)
            self.push_tree.column(
                column, width=self._px(width), minwidth=self._px(width),
                stretch=column in {"name", "result"}, anchor=tk.CENTER,
            )
        self.push_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.push_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.push_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.push_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.push_tree.bind("<Button-1>", self._on_push_click)
        self.push_tree.bind("<space>", self._toggle_push_selection)
        self.push_tree.bind("<<TreeviewSelect>>", self._refresh_push_controls)
        self.push_tree.bind("<Double-1>", lambda _event: self._edit_push_channel())
        ttk.Label(
            parent,
            text="自选模式下点击发送列或按空格勾选；全部模式自动发送到所有渠道。",
            style="Muted.TLabel", wraplength=self._px(560),
        ).grid(row=5, column=0, sticky="w", pady=self._px(8))
        actions = ttk.Frame(parent, style="Surface.TFrame")
        actions.grid(row=6, column=0, sticky="ew")
        self.add_push_button = ttk.Button(actions, text="新增渠道", command=self._new_push_channel)
        self.edit_push_button = ttk.Button(actions, text="编辑渠道", command=self._edit_push_channel)
        self.delete_push_button = ttk.Button(actions, text="删除渠道", command=self._delete_push_channel)
        for column, button in enumerate(
            (self.add_push_button, self.edit_push_button, self.delete_push_button)
        ):
            button.grid(row=0, column=column, padx=(0, self._px(8)), pady=self._px(4))
        tests = ttk.Frame(parent, style="Surface.TFrame")
        tests.grid(row=7, column=0, sticky="ew", pady=(self._px(10), 0))
        self.test_selected_push_button = ttk.Button(
            tests, text="测试所选渠道", command=lambda: self._test_push(single=True)
        )
        self.test_push_button = ttk.Button(
            tests, text="测试当前范围", command=self._test_push,
        )
        self.test_selected_push_button.grid(row=0, column=0, padx=(0, self._px(8)))
        self.test_push_button.grid(row=0, column=1)
        ttk.Label(
            parent, textvariable=self.push_test_result_var, style="Muted.TLabel",
            wraplength=self._px(560), justify=tk.LEFT,
        ).grid(row=8, column=0, sticky="ew", pady=self._px(10))
        self.save_push_button = ttk.Button(
            parent, text="保存推送设置", style="Primary.TButton",
            command=self._save_push_settings,
        )
        self.save_push_button.grid(row=9, column=0, sticky="e", pady=self._px(6))

    def _push_values(self) -> Dict[str, Any]:
        return {
            "channels": deepcopy(self.push_channels),
            "push_mode": self.push_mode_var.get(),
            "selected_channel_ids": list(self.push_selected_ids),
        }

    def _push_busy(self) -> bool:
        return bool(
            self.monitoring_ui_active or self.testing_push
            or self.exiting or self.runtime_closed
        )

    def _highlighted_channel_id(self) -> str:
        selection = self.push_tree.selection()
        return str(selection[0]) if selection else ""

    def _refresh_push_controls(self, _event: Any = None) -> None:
        busy = self._push_busy()
        selected = bool(self._highlighted_channel_id())
        for button in (*self.push_mode_buttons, self.add_push_button, self.save_push_button):
            button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        for button in (self.edit_push_button, self.delete_push_button, self.test_selected_push_button):
            button.configure(state=tk.NORMAL if selected and not busy else tk.DISABLED)
        count = len(self.push_channels) if self.push_mode_var.get() == "all" else sum(
            channel["id"] in self.push_selected_ids for channel in self.push_channels
        )
        self.test_push_button.configure(state=tk.NORMAL if count and not busy else tk.DISABLED)

    def _refresh_push_channels(self) -> None:
        current_id = self._highlighted_channel_id()
        self.push_tree.delete(*self.push_tree.get_children())
        use_all = self.push_mode_var.get() == "all"
        for channel in self.push_channels:
            included = use_all or channel["id"] in self.push_selected_ids
            self.push_tree.insert(
                "", tk.END, iid=channel["id"], values=(
                    "☑" if included else "☐", channel["name"],
                    CHANNEL_TYPES[channel["type"]], self.push_results.get(channel["id"], "尚未测试"),
                ),
            )
        if current_id and self.push_tree.exists(current_id):
            self.push_tree.selection_set(current_id)
            self.push_tree.focus(current_id)
        count = len(self.push_channels) if use_all else sum(
            channel["id"] in self.push_selected_ids for channel in self.push_channels
        )
        detail = "全部渠道" if use_all else "自选渠道"
        suffix = "；当前不发送通知" if not count else ""
        self.push_count_var.set(f"{detail}：{count} / {len(self.push_channels)} 个渠道{suffix}")
        self._refresh_push_controls()

    def _on_push_click(self, event: tk.Event) -> Optional[str]:
        channel_id = self.push_tree.identify_row(event.y)
        if channel_id and self.push_tree.identify_column(event.x) == "#1":
            self.push_tree.selection_set(channel_id)
            self.push_tree.focus(channel_id)
            self._toggle_push_selection()
            return "break"
        return None

    def _toggle_push_selection(self, _event: Any = None) -> str:
        channel_id = self._highlighted_channel_id()
        if channel_id and not self._push_busy() and self.push_mode_var.get() == "selected":
            if channel_id in self.push_selected_ids:
                self.push_selected_ids.remove(channel_id)
            else:
                self.push_selected_ids.append(channel_id)
            self._refresh_push_channels()
        return "break"

    def _new_push_channel(self) -> None:
        self._open_channel_editor()

    def _edit_push_channel(self) -> None:
        channel_id = self._highlighted_channel_id()
        channel = next((item for item in self.push_channels if item["id"] == channel_id), None)
        if channel:
            self._open_channel_editor(channel)

    def _open_channel_editor(self, channel: Optional[Mapping[str, Any]] = None) -> None:
        if self._push_busy():
            return
        if self.channel_editor is not None:
            self.channel_editor.lift()
            return
        window = tk.Toplevel(self.root)
        self.channel_editor = window
        window.title("编辑渠道" if channel else "新增渠道")
        window.transient(self.root)
        window.configure(background=COLORS["surface"])
        window.geometry(f"{self._px(600)}x{min(self._px(520), get_work_area(self.root)[3])}")
        variables: Dict[str, tk.StringVar] = {}
        fields: Dict[str, Mapping[str, Any]] = {}
        entries: Dict[str, ttk.Entry] = {}
        type_var = tk.StringVar(value=CHANNEL_TYPES[channel["type"]] if channel else CHANNEL_TYPES["serverchan"])
        name_var = tk.StringVar(value=channel["name"] if channel else "")
        reveal_var = tk.BooleanVar(value=False)

        def close() -> None:
            self.channel_editor = None
            window.destroy()

        def build(form: ttk.Frame) -> None:
            form.grid_columnconfigure(1, weight=1)
            ttk.Label(form, text="渠道类型").grid(row=0, column=0, sticky="w", pady=self._px(7))
            type_combo = ttk.Combobox(form, values=tuple(CHANNEL_TYPES.values()), textvariable=type_var, state="readonly")
            type_combo.grid(row=0, column=1, sticky="ew", padx=self._px(10))
            ttk.Label(form, text="渠道名称").grid(row=1, column=0, sticky="w", pady=self._px(7))
            ttk.Entry(form, textvariable=name_var).grid(row=1, column=1, sticky="ew", padx=self._px(10))
            dynamic = ttk.Frame(form, style="Surface.TFrame")
            dynamic.grid(row=2, column=0, columnspan=2, sticky="ew")
            dynamic.grid_columnconfigure(1, weight=1)

            def render_fields(_event: Any = None) -> None:
                for child in dynamic.winfo_children():
                    child.destroy()
                variables.clear()
                fields.clear()
                entries.clear()
                provider = next(key for key, label in CHANNEL_TYPES.items() if label == type_var.get())
                config = channel["config"] if channel and channel["type"] == provider else {}
                for row, field in enumerate(CHANNEL_FIELDS[provider]):
                    key = field["key"]
                    value = config.get(key, field["default"])
                    if field.get("kind") == "json":
                        value = json.dumps(value, ensure_ascii=False)
                    variable = tk.StringVar(value=str(value))
                    variables[key] = variable
                    fields[key] = field
                    ttk.Label(dynamic, text=field["label"]).grid(row=row, column=0, sticky="w", pady=self._px(7))
                    entry = ttk.Entry(dynamic, textvariable=variable, show="●" if field["secret"] and not reveal_var.get() else "")
                    entry.grid(row=row, column=1, sticky="ew", padx=self._px(10), pady=self._px(7))
                    entries[key] = entry

            def reveal() -> None:
                for key, entry in entries.items():
                    entry.configure(show="●" if fields[key]["secret"] and not reveal_var.get() else "")

            def save() -> None:
                try:
                    provider = next(key for key, label in CHANNEL_TYPES.items() if label == type_var.get())
                    config = {key: value.get() for key, value in variables.items()}
                    for key, field in fields.items():
                        if field.get("kind") == "json":
                            try:
                                config[key] = json.loads(config[key])
                            except (ValueError, TypeError):
                                raise ValueError("自定义请求头必须是 JSON 对象") from None
                    self._upsert_push_channel({
                        "id": channel["id"] if channel else uuid.uuid4().hex,
                        "name": name_var.get(), "type": provider, "config": config,
                    })
                except ValueError as exc:
                    messagebox.showerror("无法保存渠道", str(exc), parent=window)
                    return
                close()

            type_combo.bind("<<ComboboxSelected>>", render_fields)
            render_fields()
            ttk.Checkbutton(form, text="显示凭据", variable=reveal_var, command=reveal).grid(row=3, column=1, sticky="w", pady=self._px(8))
            buttons = ttk.Frame(form, style="Surface.TFrame")
            buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=self._px(10))
            ttk.Button(buttons, text="取消", command=close).grid(row=0, column=0, padx=self._px(8))
            ttk.Button(buttons, text="确定", style="Primary.TButton", command=save).grid(row=0, column=1)

        self._build_scrollable_form(window, build, padding=self._px(16))
        window.protocol("WM_DELETE_WINDOW", close)
        window.bind("<Escape>", lambda _event: close())
        window.grab_set()
        window.focus_set()

    def _upsert_push_channel(self, values: Mapping[str, Any]) -> None:
        if self._push_busy():
            return
        channel = normalize_channel(values)
        channels = deepcopy(self.push_channels)
        for index, existing in enumerate(channels):
            if existing["id"] == channel["id"]:
                channels[index] = channel
                break
        else:
            channels.append(channel)
        candidate = self._push_values()
        candidate["channels"] = channels
        self.push_channels = normalize_push_config(candidate)["channels"]
        self.push_results.pop(channel["id"], None)
        self._refresh_push_channels()
        self.push_tree.selection_set(channel["id"])
        self._refresh_push_controls()

    def _delete_push_channel(self) -> None:
        if self._push_busy():
            return
        channel_id = self._highlighted_channel_id()
        channel = next((item for item in self.push_channels if item["id"] == channel_id), None)
        if not channel or not messagebox.askyesno(
            "删除渠道", f"确定删除“{channel['name']}”吗？", parent=self.root
        ):
            return
        self.push_channels = [item for item in self.push_channels if item["id"] != channel_id]
        self.push_selected_ids = [item for item in self.push_selected_ids if item != channel_id]
        self.push_results.pop(channel_id, None)
        self._refresh_push_channels()

    def _save_push_settings(self) -> bool:
        if self._push_busy():
            return False
        try:
            candidate = deepcopy(self.config)
            candidate.update(normalize_push_config(self._push_values()))
            save_config_atomic(self.config_path, candidate)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存推送设置", str(exc), parent=self.root)
            return False
        self.config.clear()
        self.config.update(candidate)
        self.push_test_result_var.set("推送设置已保存。")
        return True

    def _save_startup_drafts(self) -> bool:
        try:
            values = self._settings_values()
            values["close_action"] = self.config.get("close_action", "exit")
            settings = validate_gui_settings(values)
            for key in ("push_url", "sendkey", "push_uid"):
                settings.pop(key, None)
            push = normalize_push_config(self._push_values())
            candidate = deepcopy(self.config)
            candidate.update(settings)
            candidate.update(push)
            save_config_atomic(self.config_path, candidate)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法开始监控", str(exc), parent=self.root)
            return False
        self.config.clear()
        self.config.update(candidate)
        return True

    def _load_settings_into_form(self) -> None:
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
        close_action = self.config.get("close_action", "exit")
        self.close_action_var.set(CLOSE_ACTION_LABELS.get(close_action, "退出主程序"))
        if IS_WINDOWS:
            try:
                self.autostart_var.set(is_autostart_enabled(self.config_path))
            except (OSError, ValueError):
                self.autostart_var.set(False)

    def _settings_values(self) -> Dict[str, Any]:
        return {
            "check_interval": self.check_interval_var.get(),
            "repeat_notify_interval": self.repeat_interval_var.get(),
            "max_repeat_notifications": self.max_repeat_var.get(),
            "max_concurrent_checks": self.max_concurrent_var.get(),
            "notify_on_stream_end": self.notify_end_var.get(),
            "startup_notify": self.startup_notify_var.get(),
            "enable_daily_intimacy_reminder": self.daily_reminder_var.get(),
            "close_action": CLOSE_ACTION_VALUES.get(
                self.close_action_var.get(), ""
            ),
        }

    def _save_settings(
        self, show_success: bool = True, include_system: bool = True
    ) -> bool:
        if getattr(self, "testing_push", False):
            return False
        previous_config = dict(self.config)
        previous_autostart = None
        registry_touched = False
        try:
            values = self._settings_values()
            apply_system_settings = include_system and IS_WINDOWS
            if not apply_system_settings:
                values["close_action"] = self.config.get("close_action", "exit")
            normalized = validate_gui_settings(values)
            for key in ("push_url", "sendkey", "push_uid"):
                normalized.pop(key, None)
            if (
                apply_system_settings
                and normalized["close_action"] == "hide_to_tray"
                and not self.tray_available
            ):
                raise ValueError("任务栏托盘当前不可用，无法启用隐藏到托盘")
            if (
                apply_system_settings
                and self.autostart_var.get()
                and not self.tray_available
            ):
                raise ValueError("任务栏托盘当前不可用，无法启用静默开机自启")
            if apply_system_settings:
                previous_autostart = get_autostart_snapshot()
                registry_touched = True
                set_autostart_enabled(self.autostart_var.get(), self.config_path)
            self.config.update(normalized)
            save_config_atomic(self.config_path, self.config)
        except (OSError, ValueError) as exc:
            self.config.clear()
            self.config.update(previous_config)
            rollback_error = ""
            if IS_WINDOWS and registry_touched:
                try:
                    restore_autostart_value(previous_autostart)
                except OSError as rollback_exc:
                    rollback_error = f"\n恢复 Windows 自启设置也失败: {rollback_exc}"
            messagebox.showerror("无法保存设置", str(exc), parent=self.root)
            if rollback_error:
                messagebox.showerror(
                    "无法恢复自启设置", rollback_error.strip(), parent=self.root
                )
            return False
        if show_success:
            if self.monitoring_ui_active:
                messagebox.showinfo(
                    "设置已保存",
                    "窗口与系统设置已生效，监控参数将在下次启动时生效。",
                    parent=self.root,
                )
            else:
                self._set_global_status("stopped", "设置已保存")
        return True

    def _refresh_streamer_views(self) -> None:
        current_id = self.selected_streamer_id
        for tree in (self.streamer_tree, self.status_tree):
            for item in tree.get_children():
                tree.delete(item)
        for entry in self.entries:
            streamer_id = entry["id"]
            state, name, platform, last_check, detail = self._streamer_row_values(entry)
            self.streamer_tree.insert(
                "", tk.END, iid=streamer_id, values=(status_text(state), platform, name)
            )
            self.status_tree.insert(
                "",
                tk.END,
                iid=streamer_id,
                values=(
                    status_text(state),
                    platform,
                    name,
                    last_check,
                    detail,
                ),
                tags=(state,),
            )
        self.streamer_count_label.configure(text=f"{len(self.entries)} 个")
        if current_id and self.streamer_tree.exists(current_id):
            self.streamer_tree.selection_set(current_id)
            self.status_tree.selection_set(current_id)
            self.status_tree.focus(current_id)
        elif self.entries:
            first_id = self.entries[0]["id"]
            self.streamer_tree.selection_set(first_id)
            self.streamer_tree.focus(first_id)
            self._on_streamer_selected()

    def _streamer_row_values(self, entry: Mapping[str, Any]):
        streamer_id = str(entry["id"])
        runtime = self.runtime_by_id.get(streamer_id, {})
        state = runtime.get("status", "pending")
        if runtime.get("error_kind") == "restricted" and state in {
            "error", "retrying", "unknown",
        }:
            state = "restricted"
        if runtime.get("suspended") and not runtime.get("stopped_by_user"):
            state = "suspended"
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
        if runtime.get("next_retry"):
            detail += f" | 下次重试 {runtime['next_retry']}"
        if runtime.get("notification_status"):
            detail += f" | {runtime['notification_status']}"
        platform = gui_platform_name(
            str(runtime.get("platform") or entry.get("platform") or "douyin")
        )
        return state, name, platform, runtime.get("last_check", ""), detail

    def _refresh_runtime_row(self, streamer_id: str) -> None:
        entry = next(
            (item for item in self.entries if item["id"] == streamer_id), None
        )
        if entry is None:
            return
        state, name, platform, last_check, detail = self._streamer_row_values(entry)
        if self.streamer_tree.exists(streamer_id):
            self.streamer_tree.item(
                streamer_id, values=(status_text(state), platform, name)
            )
        if self.status_tree.exists(streamer_id):
            self.status_tree.item(
                streamer_id,
                values=(status_text(state), platform, name, last_check, detail),
                tags=(state,),
            )

    def _on_status_tree_double_click(self, event) -> None:
        streamer_id = self.status_tree.identify_row(event.y)
        if not streamer_id:
            return
        self.status_tree.selection_set(streamer_id)
        self.status_tree.focus(streamer_id)
        self._show_streamer_log(streamer_id)

    def _on_status_selection_changed(self, _event=None) -> None:
        selection = self.status_tree.selection()
        if selection:
            streamer_id = selection[0]
            if self.streamer_tree.exists(streamer_id):
                self.streamer_tree.selection_set(streamer_id)
                self.streamer_tree.focus(streamer_id)
            self._on_streamer_selected()
        self._update_stop_selected_button()

    def _update_stop_selected_button(self) -> None:
        running = bool(
            self.monitoring_ui_active
            and self.service is not None
            and self.service_thread is not None
            and self.service_thread.is_alive()
        )
        selection = self.status_tree.selection()
        runtime = self.runtime_by_id.get(selection[0], {}) if selection else {}
        can_stop = bool(
            running
            and runtime
            and not runtime.get("suspended", False)
            and runtime.get("status") != "stopped"
        )
        self.stop_selected_button.configure(
            state=tk.NORMAL if can_stop else tk.DISABLED
        )

    def _stop_selected_streamer(self) -> None:
        selection = self.status_tree.selection()
        if not selection or self.service is None:
            messagebox.showinfo(
                "停止主播", "请先在任务状态中选择一个正在监控的主播。", parent=self.root
            )
            return
        streamer_id = selection[0]
        entry = next(
            (item for item in self.entries if item["id"] == streamer_id), None
        )
        runtime = self.runtime_by_id.get(streamer_id, {})
        name = compact_ui_text(
            runtime.get("nickname")
            or (entry or {}).get("label")
            or "所选主播",
            80,
        )
        if not messagebox.askyesno(
            "停止所选主播",
            f"确定停止“{name}”的本次监控吗？\n\n其他主播会继续运行。",
            parent=self.root,
        ):
            return
        self.stop_selected_button.configure(state=tk.DISABLED)

        def stop_selected() -> None:
            service = self.service
            stopped = bool(service and service.stop_streamer(streamer_id))
            if not stopped:
                self.event_queue.put(
                    {"type": "streamer_stop_failed", "streamer_id": streamer_id}
                )

        threading.Thread(
            target=stop_selected,
            name=f"monitor-stop-{streamer_id}",
            daemon=True,
        ).start()

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
        platform = gui_platform_name(str(entry.get("platform") or "douyin"))
        window.title(f"{platform} · {name} · 实时运行日志")
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
            text=f"{platform} · 当前监控会话日志，停止后仍可查看。",
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
        if self.status_tree.exists(streamer_id):
            if self.status_tree.selection() != (streamer_id,):
                self.status_tree.selection_set(streamer_id)
            self.status_tree.focus(streamer_id)
        self._update_stop_selected_button()

    def _update_streamer_platform(self, *_args: Any) -> None:
        """Recognize public links locally as the user edits the form."""
        url = self.streamer_url_var.get().strip()
        color = COLORS["muted"]
        label = "输入链接后自动识别"
        if url:
            try:
                platform = infer_platform(url)
                validate_platform_url(url, platform)
                label = gui_platform_name(platform)
            except ValueError as exc:
                label = compact_ui_text(str(exc), 80)
                color = COLORS["warning"]
        self.streamer_platform_var.set(label)
        self.streamer_platform_label.configure(foreground=color)

    def _save_streamer(self) -> None:
        try:
            url = validate_platform_url(self.streamer_url_var.get())
            if self.selected_streamer_id:
                entry = update_streamer(
                    self.config,
                    self.selected_streamer_id,
                    url,
                    label=self.streamer_label_var.get(),
                    enabled=self.streamer_enabled_var.get(),
                )
            else:
                entry = add_streamer(
                    self.config,
                    url,
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

    def _start_monitoring(self, silent: bool = False) -> bool:
        if getattr(self, "testing_push", False):
            return False
        if getattr(self, "channel_editor", None) is not None:
            self.channel_editor.lift()
            return False
        if self.service_thread and self.service_thread.is_alive():
            self._stop_monitoring()
            return False
        if not self._save_startup_drafts():
            return False
        self.entries = list(self.config.get("streamers", []))
        active_entries = enabled_streamers(self.entries)
        if not active_entries:
            if silent:
                self._set_global_status("stopped", "没有已启用的主播")
            else:
                self._restore_window()
                messagebox.showwarning(
                    "无法开始监控", "请至少添加并启用一个主播。", parent=self.root
                )
            return False
        if not has_push_targets(self.config):
            if not silent:
                self._restore_window()
                proceed = messagebox.askyesno(
                    "尚未配置推送",
                    "当前推送范围没有接收渠道。\n"
                    "可以继续检测，但不会收到手机通知。\n\n仍然开始监控吗？",
                    parent=self.root,
                )
                if not proceed:
                    self.notebook.select(3)
                    self.add_push_button.focus_set()
                    return False

        try:
            self.service = create_monitor_service(
                self.config,
                active_entries,
                debug=self.debug,
                on_event=self.event_queue.put,
            )
        except (TypeError, ValueError) as exc:
            if silent:
                self._restore_window()
            messagebox.showerror("无法开始监控", str(exc), parent=self.root)
            return False
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
        return True

    def _stop_monitoring(self) -> None:
        if self.service is not None:
            self._set_global_status("starting", "正在停止监控")
            self.start_button.configure(state=tk.DISABLED)
            self.service.stop()

    def _set_running_ui(self, running: bool) -> None:
        self.monitoring_ui_active = bool(running)
        self.start_button.configure(
            text="停止全部" if running else "开始监控",
            command=self._stop_monitoring if running else self._start_monitoring,
            state=tk.DISABLED if self.testing_push else tk.NORMAL,
        )
        edit_state = tk.DISABLED if running or self.testing_push else tk.NORMAL
        for widget in (
            self.new_button,
            self.remove_button,
            self.label_entry,
            self.url_entry,
            self.enabled_check,
            self.save_streamer_button,
            self.notify_end_check,
            self.startup_check,
            self.daily_check,
            *self.setting_inputs,
        ):
            widget.configure(state=edit_state)
        self._update_stop_selected_button()
        self._refresh_push_controls()
        self._sync_tray_state()

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

    def _test_push(self, single: bool = False) -> None:
        if self._push_busy() or self.channel_editor is not None:
            return
        try:
            candidate = normalize_push_config(self._push_values())
            if single:
                channel_id = self._highlighted_channel_id()
                candidate["push_mode"] = "selected"
                candidate["selected_channel_ids"] = [channel_id] if channel_id else []
            if not selected_channels(candidate):
                messagebox.showwarning(
                    "无法测试推送", "请选择至少一个接收渠道。", parent=self.root
                )
                return
            self.push_test_cancel = threading.Event()
            notifier = create_notifier(candidate, cancelled=self.push_test_cancel.is_set)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("无法测试推送", str(exc), parent=self.root)
            return
        self.push_test_notifier = notifier
        self.testing_push = True
        self._set_running_ui(False)
        self.save_settings_button.configure(state=tk.DISABLED)
        self.push_test_result_var.set("正在测试所选渠道…" if single else "正在测试当前范围…")

        def verify() -> None:
            result: Dict[str, Any] = {"type": "push_test", "success": False}
            try:
                result["success"] = notifier.verify_connection()
                result["report"] = notifier.last_report
            except Exception:
                result["error"] = "推送测试未完成，请检查渠道配置和网络连接。"
            finally:
                try:
                    notifier.close()
                except Exception:
                    result["error"] = "推送连接关闭时发生异常。"
                finally:
                    self.event_queue.put(result)

        self.push_test_thread = threading.Thread(
            target=verify, name="monitor-push-test", daemon=True
        )
        self.push_test_thread.start()

    def _display_push_report(self, report: Any) -> str:
        if isinstance(report, Mapping):
            summary = str(report.get("summary", ""))
            results = report.get("results", [])
        else:
            summary = str(getattr(report, "summary", ""))
            results = getattr(report, "results", [])
        labels = {"accepted": "服务已接收", "rejected": "发送失败", "unknown": "结果未知", "cancelled": "已取消"}
        for result in results:
            channel_id = result.get("channel_id") if isinstance(result, Mapping) else getattr(result, "channel_id", "")
            status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", "")
            status = getattr(status, "value", status)
            if channel_id:
                text = labels.get(str(status), "发送失败")
                self.push_results[str(channel_id)] = text
                if self.push_tree.exists(str(channel_id)):
                    self.push_tree.set(str(channel_id), "result", text)
        self.push_test_result_var.set(summary)
        return summary

    def _cancel_push_test(self) -> None:
        cancel = getattr(self, "push_test_cancel", None)
        if cancel is not None:
            cancel.set()
        notifier = getattr(self, "push_test_notifier", None)
        dispatcher = getattr(notifier, "dispatcher", None)
        if dispatcher is not None:
            dispatcher.stop()

    def _join_push_test(self) -> None:
        self._cancel_push_test()
        thread = getattr(self, "push_test_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self.push_test_thread = None
        self.push_test_notifier = None

    def _drain_events(self) -> None:
        if self.instance_guard is not None:
            try:
                if self.instance_guard.activation_requested():
                    self._restore_window()
            except OSError as exc:
                self.instance_guard = None
                messagebox.showerror(
                    "无法响应重复启动", str(exc), parent=self.root
                )
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
            service_alive = bool(self.service_thread and self.service_thread.is_alive())
            test_thread = getattr(self, "push_test_thread", None)
            if not service_alive and not (test_thread and test_thread.is_alive()):
                self._finish_exit()
                return
        delay = 200 if (
            self.testing_push
            or self.pending_close
            or (self.service_thread and self.service_thread.is_alive())
        ) else 750
        self.root.after(delay, self._drain_events)

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "tray_ready":
            if self.tray_disabled_for_session:
                if self.tray_controller is not None:
                    self.tray_controller.stop(wait=False)
                return
            self.tray_available = True
            self.tray_error = ""
            self._sync_tray_state()
            if self.autostart_requested:
                self.autostart_requested = False
                self._start_monitoring(silent=True)
        elif event_type == "tray_failed":
            self.tray_available = False
            self.tray_disabled_for_session = True
            self.tray_error = str(event.get("error") or "未知错误")
            if self.tray_controller is not None:
                self.tray_controller.stop(wait=False)
            if not self.exiting:
                self._restore_window()
            if not self.tray_failure_reported and not self.exiting:
                self.tray_failure_reported = True
                messagebox.showerror(
                    "任务栏托盘不可用",
                    "程序已恢复主窗口，本次运行关闭窗口时将直接退出。\n\n"
                    f"详细信息: {self.tray_error}",
                    parent=self.root,
                )
        elif event_type == "tray_open":
            self._restore_window()
        elif event_type == "tray_start":
            self._start_monitoring()
        elif event_type == "tray_stop":
            self._stop_monitoring()
        elif event_type == "tray_exit":
            self._request_exit(restore_before_prompt=True)
        elif event_type == "log":
            streamer_id = str(event.get("streamer_id") or "")
            if streamer_id:
                self._append_streamer_log(streamer_id, event.get("message", ""))
        elif event_type in {
            "prepared",
            "prepare_error",
            "retrying",
            "notification",
            "status",
            "error",
            "suspended",
            "streamer_stopped",
        }:
            if not self.monitoring_ui_active:
                return
            if event_type == "notification" and event.get("delivery_report"):
                self._display_push_report(event["delivery_report"])
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
            self._update_stop_selected_button()
            self._sync_tray_state()
        elif event_type == "service_finished":
            self._detach_streamer_log_handler()
            self._set_running_ui(False)
            if event.get("success"):
                self.runtime_by_id = {}
                self._refresh_streamer_views()
                self._set_global_status("stopped", "监控已停止")
                self._sync_tray_state()
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
            self._join_push_test()
            self.testing_push = False
            if self.exiting:
                return
            self._set_running_ui(False)
            self.save_settings_button.configure(state=tk.NORMAL)
            summary = self._display_push_report(event["report"]) if event.get("report") is not None else ""
            if event.get("success"):
                messagebox.showinfo("测试推送", summary or "测试消息已被服务接收。", parent=self.root)
            else:
                detail = summary or event.get("error") or "请检查渠道配置和网络连接。"
                self.push_test_result_var.set(str(detail))
                messagebox.showwarning("测试推送结果", str(detail), parent=self.root)
        elif event_type == "streamer_stop_failed":
            self._update_stop_selected_button()
            messagebox.showinfo(
                "停止主播", "该主播尚未开始或已经停止。", parent=self.root
            )

    def _request_exit(self, restore_before_prompt: bool = False) -> None:
        if self.pending_close or self.runtime_closed:
            return
        service_running = bool(
            self.service_thread and self.service_thread.is_alive()
        )
        if service_running:
            if restore_before_prompt:
                self._restore_window()
            if not messagebox.askyesno(
                "退出程序",
                "监控仍在运行。确定停止全部任务并退出吗？",
                parent=self.root,
            ):
                return
        self.exiting = True
        self._cancel_push_test()
        test_thread = getattr(self, "push_test_thread", None)
        test_running = bool(test_thread and test_thread.is_alive())
        if service_running:
            self.pending_close = True
            self._stop_monitoring()
            return
        if test_running:
            self.pending_close = True
            return
        self._finish_exit()

    def _finish_exit(self) -> None:
        if self.runtime_closed:
            return
        self.runtime_closed = True
        self._join_push_test()
        self._detach_streamer_log_handler()
        if self.tray_controller is not None:
            self.tray_controller.stop()
        self.root.destroy()

    def shutdown_after_mainloop(self) -> None:
        """Release background integrations if Tk exits unexpectedly."""
        if self.runtime_closed:
            return
        self.runtime_closed = True
        self._join_push_test()
        self._detach_streamer_log_handler()
        if self.service is not None:
            self.service.stop()
        if (
            self.service_thread is not None
            and self.service_thread is not threading.current_thread()
        ):
            self.service_thread.join()
        if self.tray_controller is not None:
            self.tray_controller.stop()

    def _on_close(self) -> None:
        if (
            self.config.get("close_action", "exit") == "hide_to_tray"
            and self.tray_available
            and not self.exiting
        ):
            self.root.withdraw()
            return
        self._request_exit()


def run_gui(
    config_path: Optional[Path] = None,
    debug: bool = False,
    autostart: bool = False,
    instance_guard: Optional[WindowsInstanceGuard] = None,
) -> None:
    """Create and run the desktop application."""
    monitor.setup_logging(verbose=debug)
    enable_windows_high_dpi()
    root = tk.Tk()
    root.withdraw()
    try:
        app = MonitorGui(
            root,
            config_path or monitor.DEFAULT_CONFIG,
            debug=debug,
            autostart=autostart,
            instance_guard=instance_guard,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        messagebox.showerror("无法启动程序", str(exc), parent=root)
        root.destroy()
        return
    app.show_window()
    if not autostart:
        root.deiconify()
    try:
        root.mainloop()
    finally:
        app.shutdown_after_mainloop()


if __name__ == "__main__":
    run_gui()
