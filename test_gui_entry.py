import sys
from types import SimpleNamespace
from unittest.mock import Mock

import gui_app
import gui_entry
import monitor


def test_unified_entry_uses_gui_without_cli_flags(monkeypatch):
    launched = []
    monkeypatch.setattr(sys, "argv", ["gui_entry.py"])
    monkeypatch.setattr(gui_entry, "_hide_frozen_console", lambda: None)
    monkeypatch.setattr(
        gui_app,
        "run_gui",
        lambda config_path, debug=False: launched.append((config_path, debug)),
    )

    gui_entry.main()

    assert launched == [(monitor.DEFAULT_CONFIG, False)]


def test_unified_entry_preserves_cli_mode_when_flags_are_present(monkeypatch):
    called = []
    monkeypatch.setattr(sys, "argv", ["gui_entry.py", "--version"])
    monkeypatch.setattr(monitor, "main", lambda: called.append(True))

    gui_entry.main()

    assert called == [True]


def test_gui_does_not_hide_a_console_shared_with_the_parent(monkeypatch):
    kernel32 = SimpleNamespace(
        GetConsoleWindow=Mock(return_value=123),
        GetConsoleProcessList=Mock(return_value=2),
    )
    user32 = SimpleNamespace(ShowWindow=Mock())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui_entry.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32, user32=user32),
        raising=False,
    )

    gui_entry._hide_frozen_console()

    user32.ShowWindow.assert_not_called()


def test_gui_hides_its_private_frozen_console(monkeypatch):
    kernel32 = SimpleNamespace(
        GetConsoleWindow=Mock(return_value=123),
        GetConsoleProcessList=Mock(return_value=1),
    )
    user32 = SimpleNamespace(ShowWindow=Mock())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui_entry.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32, user32=user32),
        raising=False,
    )

    gui_entry._hide_frozen_console()

    user32.ShowWindow.assert_called_once_with(123, 0)
