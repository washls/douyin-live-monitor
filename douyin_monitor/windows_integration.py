"""Windows-only single-instance and login-startup integration helpers."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Optional, Tuple


IS_WINDOWS = sys.platform == "win32"
MONITOR_MUTEX_NAME = r"Local\DouyinLiveMonitor.Monitor"
GUI_MUTEX_NAME = r"Local\DouyinLiveMonitor.Gui"
ACTIVATION_EVENT_NAME = r"Local\DouyinLiveMonitor.Activate"
AUTOSTART_VALUE_NAME = "DouyinLiveMonitor"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


def _configure_kernel32(kernel32: Any) -> Any:
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.restype = wintypes.DWORD
    return kernel32


def _windows_error(kernel32: Any, operation: str) -> OSError:
    code = int(kernel32.GetLastError())
    return OSError(code, f"{operation}失败，Windows 错误码 {code}")


class _NamedMutex:
    """Own one named Windows mutex until explicitly closed."""

    def __init__(self, handle: Any, kernel32: Any):
        self.handle = handle
        self.kernel32 = kernel32

    @classmethod
    def try_create(cls, name: str, kernel32: Any) -> Optional["_NamedMutex"]:
        handle = kernel32.CreateMutexW(None, True, name)
        if not handle:
            raise _windows_error(kernel32, "创建单实例锁")
        if int(kernel32.GetLastError()) == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return cls(handle, kernel32)

    def close(self) -> None:
        if self.handle:
            self.kernel32.ReleaseMutex(self.handle)
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class WindowsInstanceGuard:
    """Hold the shared monitor lock and optional GUI activation handles."""

    def __init__(
        self,
        monitor_mutex: Optional[_NamedMutex] = None,
        gui_mutex: Optional[_NamedMutex] = None,
        activation_event: Any = None,
        kernel32: Any = None,
    ):
        self.monitor_mutex = monitor_mutex
        self.gui_mutex = gui_mutex
        self.activation_event = activation_event
        self.kernel32 = kernel32

    @classmethod
    def acquire_monitor(cls, kernel32: Any = None) -> Optional["WindowsInstanceGuard"]:
        """Acquire the Windows monitor lock, or return ``None`` if occupied."""
        if not IS_WINDOWS and kernel32 is None:
            return cls()
        kernel32 = _configure_kernel32(kernel32 or ctypes.windll.kernel32)
        mutex = _NamedMutex.try_create(MONITOR_MUTEX_NAME, kernel32)
        if mutex is None:
            return None
        return cls(monitor_mutex=mutex, kernel32=kernel32)

    @classmethod
    def acquire_gui(
        cls, kernel32: Any = None
    ) -> Tuple[Optional["WindowsInstanceGuard"], str]:
        """Acquire GUI ownership and report ``acquired``, ``gui`` or ``cli``."""
        if not IS_WINDOWS and kernel32 is None:
            return cls(), "acquired"
        kernel32 = _configure_kernel32(kernel32 or ctypes.windll.kernel32)
        monitor_mutex = _NamedMutex.try_create(MONITOR_MUTEX_NAME, kernel32)
        if monitor_mutex is None:
            if _named_mutex_exists(GUI_MUTEX_NAME, kernel32):
                _signal_activation(kernel32)
                return None, "gui"
            return None, "cli"

        activation_event = kernel32.CreateEventW(
            None, False, False, ACTIVATION_EVENT_NAME
        )
        if not activation_event:
            monitor_mutex.close()
            raise _windows_error(kernel32, "创建窗口激活事件")

        try:
            gui_mutex = _NamedMutex.try_create(GUI_MUTEX_NAME, kernel32)
        except Exception:
            kernel32.CloseHandle(activation_event)
            monitor_mutex.close()
            raise
        if gui_mutex is None:
            kernel32.CloseHandle(activation_event)
            monitor_mutex.close()
            _signal_activation(kernel32)
            return None, "gui"

        return (
            cls(
                monitor_mutex=monitor_mutex,
                gui_mutex=gui_mutex,
                activation_event=activation_event,
                kernel32=kernel32,
            ),
            "acquired",
        )

    def activation_requested(self) -> bool:
        """Consume one pending request to restore the GUI window."""
        if not self.activation_event or self.kernel32 is None:
            return False
        result = int(self.kernel32.WaitForSingleObject(self.activation_event, 0))
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise _windows_error(self.kernel32, "读取窗口激活事件")

    def close(self) -> None:
        if self.activation_event and self.kernel32 is not None:
            self.kernel32.CloseHandle(self.activation_event)
            self.activation_event = None
        if self.gui_mutex is not None:
            self.gui_mutex.close()
            self.gui_mutex = None
        if self.monitor_mutex is not None:
            self.monitor_mutex.close()
            self.monitor_mutex = None


def _named_mutex_exists(name: str, kernel32: Any) -> bool:
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _signal_activation(kernel32: Any) -> bool:
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, ACTIVATION_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def show_windows_warning(title: str, message: str) -> None:
    """Show an error-style native dialog without constructing Tk."""
    if IS_WINDOWS:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x30)
    else:
        print(f"{title}: {message}", file=sys.stderr)


def build_autostart_command(
    config_path: Path,
    executable: Optional[Path] = None,
    frozen: Optional[bool] = None,
    source_entry: Optional[Path] = None,
) -> str:
    """Build the exact per-user Run-key command for this application."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable_path = Path(executable or sys.executable).resolve()
    arguments = []
    if is_frozen:
        arguments.append(str(executable_path))
    else:
        pythonw = executable_path.with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else executable_path
        default_entry = Path(__file__).resolve().parent.parent / "gui_entry.py"
        entry = Path(source_entry or default_entry).resolve()
        arguments.extend((str(interpreter), str(entry)))
    arguments.extend(
        ("--gui", "--config", str(Path(config_path).resolve()), "--autostart")
    )
    command = subprocess.list2cmdline(arguments)
    if len(command) > 260:
        raise ValueError("Windows 开机自启命令超过 260 个字符，请缩短程序或配置路径")
    return command


def _registry_module(registry: Any = None) -> Any:
    if registry is not None:
        return registry
    if not IS_WINDOWS:
        raise OSError("Windows 开机自启仅支持 Windows")
    import winreg

    return winreg


def get_autostart_value(registry: Any = None) -> Optional[str]:
    """Return the raw Run-key command registered for this application."""
    snapshot = get_autostart_snapshot(registry)
    return str(snapshot[0]) if snapshot is not None else None


def get_autostart_snapshot(
    registry: Any = None,
) -> Optional[Tuple[Any, int]]:
    """Return the Run-key value and its registry type for exact rollback."""
    registry = _registry_module(registry)
    try:
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_QUERY_VALUE
        ) as key:
            value, kind = registry.QueryValueEx(key, AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        return None
    return value, int(kind)


def is_autostart_enabled(config_path: Path, registry: Any = None) -> bool:
    """Return whether the Run key exactly targets this executable and config."""
    return get_autostart_value(registry) == build_autostart_command(config_path)


def restore_autostart_value(
    snapshot: Optional[Tuple[Any, int]], registry: Any = None
) -> None:
    """Restore a previously captured Run-key value."""
    registry = _registry_module(registry)
    with registry.CreateKeyEx(
        registry.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE,
    ) as key:
        if snapshot is None:
            try:
                registry.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass
        else:
            value, kind = snapshot
            registry.SetValueEx(
                key, AUTOSTART_VALUE_NAME, 0, kind, value
            )


def set_autostart_enabled(
    enabled: bool, config_path: Path, registry: Any = None
) -> None:
    """Idempotently enable or disable per-user Windows login startup."""
    registry = _registry_module(registry)
    desired = build_autostart_command(config_path) if enabled else None
    current = get_autostart_value(registry)
    if current == desired:
        return
    snapshot = None if desired is None else (desired, registry.REG_SZ)
    restore_autostart_value(snapshot, registry)
