"""Unified frozen entry point: GUI by default, CLI when flags are supplied."""

from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

import monitor


def _hide_frozen_console() -> None:
    """Hide the bundled console only for the default GUI launch path."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        console = kernel32.GetConsoleWindow()
        process_ids = (ctypes.c_ulong * 4)()
        process_count = kernel32.GetConsoleProcessList(process_ids, len(process_ids))
        if console and process_count == 1:
            ctypes.windll.user32.ShowWindow(console, 0)
    except (AttributeError, OSError):
        pass


def main() -> None:
    if not sys.argv[1:] or "--gui" in sys.argv[1:]:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--gui", action="store_true")
        parser.add_argument("--config", default=str(monitor.DEFAULT_CONFIG))
        parser.add_argument("--debug", action="store_true")
        args, unknown = parser.parse_known_args()
        if unknown:
            parser.error(f"GUI 模式不支持参数: {' '.join(unknown)}")
        _hide_frozen_console()
        from gui_app import run_gui

        run_gui(Path(args.config), debug=args.debug)
        return
    monitor.main()


if __name__ == "__main__":
    main()
