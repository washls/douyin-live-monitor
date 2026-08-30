"""Unified frozen entry point: GUI by default, CLI when flags are supplied."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

import monitor
from douyin_monitor.windows_integration import (
    WindowsInstanceGuard,
    show_windows_warning,
)

if sys.platform == "win32":
    import msvcrt


_TH32CS_SNAPPROCESS = 0x00000002
_DUPLICATE_SAME_ACCESS = 0x00000002
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


def _parent_process_ids(kernel32) -> list[int]:
    """Return the current Windows process ancestry, nearest parent first."""
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = kernel32.Process32FirstW.argtypes
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return []

    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    parents: dict[int, int] = {}
    try:
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    current = int(kernel32.GetCurrentProcessId())
    ancestors: list[int] = []
    seen = {current}
    while current in parents:
        parent = parents[current]
        if not parent or parent in seen:
            break
        ancestors.append(parent)
        seen.add(parent)
        current = parent
    return ancestors


def _bind_console_streams(kernel32) -> None:
    """Bind Python stdio after attaching or allocating a Windows console."""
    kernel32.SetConsoleCP(65001)
    kernel32.SetConsoleOutputCP(65001)
    stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    stdout = open(
        "CONOUT$",
        "w",
        encoding="utf-8",
        errors="replace",
        buffering=1,
    )
    stderr = open(
        "CONOUT$",
        "w",
        encoding="utf-8",
        errors="replace",
        buffering=1,
    )
    sys.stdin = sys.__stdin__ = stdin
    sys.stdout = sys.__stdout__ = stdout
    sys.stderr = sys.__stderr__ = stderr


def _open_inherited_stream(kernel32, standard_handle: int, mode: str):
    """Wrap one inherited Windows standard handle as a Python text stream."""
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    source = kernel32.GetStdHandle(standard_handle)
    if source in (None, 0, ctypes.c_void_p(-1).value):
        return None

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    duplicate = wintypes.HANDLE()
    if not kernel32.DuplicateHandle(
        process,
        source,
        process,
        ctypes.byref(duplicate),
        0,
        True,
        _DUPLICATE_SAME_ACCESS,
    ):
        return None

    flags = os.O_RDONLY if mode == "r" else os.O_WRONLY
    try:
        descriptor = msvcrt.open_osfhandle(duplicate.value, flags)
    except OSError:
        kernel32.CloseHandle(duplicate)
        raise
    buffering = -1 if mode == "r" else 1
    return open(
        descriptor,
        mode,
        encoding="utf-8",
        errors="replace",
        buffering=buffering,
    )


def _bind_inherited_standard_streams(kernel32) -> bool:
    """Use redirected or inherited stdio without creating a console window."""
    stdin = _open_inherited_stream(kernel32, _STD_INPUT_HANDLE, "r")
    stdout = _open_inherited_stream(kernel32, _STD_OUTPUT_HANDLE, "w")
    stderr = _open_inherited_stream(kernel32, _STD_ERROR_HANDLE, "w")
    if stdout is None or stderr is None:
        for stream in (stdin, stdout, stderr):
            if stream is not None:
                stream.close()
        return False
    if stdin is not None:
        sys.stdin = sys.__stdin__ = stdin
    sys.stdout = sys.__stdout__ = stdout
    sys.stderr = sys.__stderr__ = stderr
    return True


def _prepare_frozen_cli_console() -> bool:
    """Attach the windowed frozen executable to a CLI console on demand."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return False

    try:
        kernel32 = ctypes.windll.kernel32
        if _bind_inherited_standard_streams(kernel32):
            return True
        kernel32.AttachConsole.argtypes = (wintypes.DWORD,)
        kernel32.AttachConsole.restype = wintypes.BOOL
        kernel32.AllocConsole.restype = wintypes.BOOL
        attached = any(
            kernel32.AttachConsole(process_id)
            for process_id in _parent_process_ids(kernel32)
        )
        if not attached and not kernel32.AllocConsole():
            return False
        _bind_console_streams(kernel32)
        return True
    except (AttributeError, OSError):
        return False


def main() -> None:
    if (
        not sys.argv[1:]
        or "--gui" in sys.argv[1:]
        or "--autostart" in sys.argv[1:]
    ):
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--gui", action="store_true")
        parser.add_argument("--config", default=str(monitor.DEFAULT_CONFIG))
        parser.add_argument("--debug", action="store_true")
        parser.add_argument("--autostart", action="store_true")
        args, unknown = parser.parse_known_args()
        if unknown:
            _prepare_frozen_cli_console()
            parser.error(f"GUI 模式不支持参数: {' '.join(unknown)}")
        from douyin_monitor.gui_app import run_gui

        try:
            instance_guard, existing_owner = WindowsInstanceGuard.acquire_gui()
        except OSError as exc:
            show_windows_warning("无法启动程序", str(exc))
            return
        if instance_guard is None:
            if existing_owner == "cli":
                show_windows_warning(
                    "程序已在运行",
                    "已有命令行监控实例正在运行，请先停止该实例。",
                )
            return
        try:
            run_gui(
                Path(args.config),
                debug=args.debug,
                autostart=args.autostart,
                instance_guard=instance_guard,
            )
        finally:
            instance_guard.close()
        return
    _prepare_frozen_cli_console()
    monitor.main()


if __name__ == "__main__":
    main()
