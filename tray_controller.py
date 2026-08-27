"""Windows system-tray controller isolated from the Tk event loop."""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Dict, Optional


TRAY_SIZE = 64


def create_tray_image(image_module: Any = None, draw_module: Any = None) -> Any:
    """Create the blue live-status mark used by the Tk and tray icons."""
    if image_module is None or draw_module is None:
        from PIL import Image, ImageDraw

        image_module = Image
        draw_module = ImageDraw
    image = image_module.new("RGBA", (TRAY_SIZE, TRAY_SIZE), (0, 0, 0, 0))
    draw = draw_module.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=7, fill="#0067c0")
    draw.ellipse((21, 21, 43, 43), fill="#ffffff")
    draw.ellipse((27, 27, 37, 37), fill="#c42b1c")
    return image


class TrayController:
    """Run pystray on its Windows-safe worker thread and emit UI commands."""

    def __init__(
        self,
        command_sink: Callable[[Dict[str, Any]], None],
        pystray_module: Any = None,
    ):
        self.command_sink = command_sink
        self.pystray_module = pystray_module
        self.icon: Any = None
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._running = False
        self._live_count = 0
        self._ready = False
        self._stopping = False

    def start(self) -> bool:
        if sys.platform != "win32" and self.pystray_module is None:
            return False
        try:
            pystray = self.pystray_module
            if pystray is None:
                import pystray
            menu = pystray.Menu(
                pystray.MenuItem(
                    "打开主窗口", self._open_window, default=True
                ),
                pystray.MenuItem(
                    "开始监控", self._start_monitoring, enabled=self._can_start
                ),
                pystray.MenuItem(
                    "停止监控", self._stop_monitoring, enabled=self._can_stop
                ),
                pystray.MenuItem(
                    self._online_text, self._ignore, enabled=False
                ),
                pystray.MenuItem("退出程序", self._exit_program),
            )
            self.icon = pystray.Icon(
                "douyin-live-monitor",
                icon=create_tray_image(),
                title="抖音直播监听器",
                menu=menu,
            )
            self.thread = threading.Thread(
                target=self._run,
                name="monitor-system-tray",
                daemon=True,
            )
            self.thread.start()
            return True
        except Exception as exc:
            self.command_sink({"type": "tray_failed", "error": str(exc)})
            return False

    def _run(self) -> None:
        try:
            self.icon.run(setup=self._setup)
            if not self._stopping:
                self.command_sink(
                    {"type": "tray_failed", "error": "托盘事件循环意外结束"}
                )
        except Exception as exc:
            if not self._stopping:
                self.command_sink({"type": "tray_failed", "error": str(exc)})

    def _setup(self, icon: Any) -> None:
        try:
            icon.visible = True
            with self._lock:
                self._ready = True
            self.command_sink({"type": "tray_ready"})
        except Exception as exc:
            self.command_sink({"type": "tray_failed", "error": str(exc)})

    def update_state(self, running: bool, live_count: int) -> None:
        with self._lock:
            self._running = bool(running)
            self._live_count = max(0, int(live_count))
            ready = self._ready
        if ready and self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception as exc:
                self.command_sink({"type": "tray_failed", "error": str(exc)})

    def stop(self, wait: bool = True) -> None:
        self._stopping = True
        icon = self.icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        thread = self.thread
        if (
            wait
            and thread is not None
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=5)

    def _can_start(self, _item: Any) -> bool:
        with self._lock:
            return not self._running

    def _can_stop(self, _item: Any) -> bool:
        with self._lock:
            return self._running

    def _online_text(self, _item: Any) -> str:
        with self._lock:
            return f"当前在线主播数量：{self._live_count}"

    def _emit(self, event_type: str) -> None:
        self.command_sink({"type": event_type})

    def _open_window(self, _icon: Any, _item: Any) -> None:
        self._emit("tray_open")

    def _start_monitoring(self, _icon: Any, _item: Any) -> None:
        self._emit("tray_start")

    def _stop_monitoring(self, _icon: Any, _item: Any) -> None:
        self._emit("tray_stop")

    def _exit_program(self, _icon: Any, _item: Any) -> None:
        self._emit("tray_exit")

    @staticmethod
    def _ignore(_icon: Any, _item: Any) -> None:
        return None
