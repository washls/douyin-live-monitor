import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import gui_app
import gui_entry
import monitor


PROJECT_ROOT = Path(__file__).resolve().parent


def test_unified_entry_uses_gui_without_cli_flags(monkeypatch):
    launched = []
    guard = Mock()
    monkeypatch.setattr(sys, "argv", ["gui_entry.py"])
    prepare_console = Mock()
    monkeypatch.setattr(gui_entry, "_prepare_frozen_cli_console", prepare_console)
    monkeypatch.setattr(
        gui_app,
        "run_gui",
        lambda config_path, **kwargs: launched.append((config_path, kwargs)),
    )
    monkeypatch.setattr(
        gui_entry.WindowsInstanceGuard,
        "acquire_gui",
        lambda: (guard, "acquired"),
    )

    gui_entry.main()

    assert launched == [
        (
            monitor.DEFAULT_CONFIG,
            {
                "debug": False,
                "autostart": False,
                "instance_guard": guard,
            },
        )
    ]
    guard.close.assert_called_once_with()
    prepare_console.assert_not_called()


def test_autostart_uses_hidden_gui_mode(monkeypatch, tmp_path):
    launched = []
    guard = Mock()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["gui_entry.py", "--gui", "--config", str(config_path), "--autostart"],
    )
    monkeypatch.setattr(
        gui_entry.WindowsInstanceGuard,
        "acquire_gui",
        lambda: (guard, "acquired"),
    )
    monkeypatch.setattr(
        gui_app,
        "run_gui",
        lambda path, **kwargs: launched.append((path, kwargs)),
    )

    gui_entry.main()

    assert launched[0][0] == config_path
    assert launched[0][1]["autostart"] is True
    assert launched[0][1]["instance_guard"] is guard
    guard.close.assert_called_once_with()


def test_second_gui_launch_only_activates_existing_instance(monkeypatch):
    launched = Mock()
    monkeypatch.setattr(sys, "argv", ["gui_entry.py"])
    monkeypatch.setattr(
        gui_entry.WindowsInstanceGuard,
        "acquire_gui",
        lambda: (None, "gui"),
    )
    monkeypatch.setattr(gui_app, "run_gui", launched)

    gui_entry.main()

    launched.assert_not_called()


def test_gui_reports_an_existing_cli_monitor(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(sys, "argv", ["gui_entry.py"])
    monkeypatch.setattr(
        gui_entry.WindowsInstanceGuard,
        "acquire_gui",
        lambda: (None, "cli"),
    )
    monkeypatch.setattr(gui_entry, "show_windows_warning", warning)

    gui_entry.main()

    warning.assert_called_once()
    assert "命令行监控实例" in warning.call_args.args[1]


def test_unified_entry_preserves_cli_mode_when_flags_are_present(monkeypatch):
    called = []
    monkeypatch.setattr(sys, "argv", ["gui_entry.py", "--version"])
    prepare_console = Mock()
    monkeypatch.setattr(gui_entry, "_prepare_frozen_cli_console", prepare_console)
    monkeypatch.setattr(monitor, "main", lambda: called.append(True))

    gui_entry.main()

    assert called == [True]
    prepare_console.assert_called_once_with()


def test_cli_attaches_to_nearest_ancestor_console(monkeypatch):
    kernel32 = SimpleNamespace(
        AttachConsole=Mock(side_effect=[False, True]),
        AllocConsole=Mock(),
    )
    bind_streams = Mock()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui_entry.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )
    monkeypatch.setattr(gui_entry, "_parent_process_ids", lambda _: [20, 10])
    monkeypatch.setattr(
        gui_entry, "_bind_inherited_standard_streams", lambda _: False
    )
    monkeypatch.setattr(gui_entry, "_bind_console_streams", bind_streams)

    assert gui_entry._prepare_frozen_cli_console() is True

    assert [item.args for item in kernel32.AttachConsole.call_args_list] == [
        (20,),
        (10,),
    ]
    kernel32.AllocConsole.assert_not_called()
    bind_streams.assert_called_once_with(kernel32)


def test_cli_allocates_console_when_no_ancestor_has_one(monkeypatch):
    kernel32 = SimpleNamespace(
        AttachConsole=Mock(return_value=False),
        AllocConsole=Mock(return_value=True),
    )
    bind_streams = Mock()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui_entry.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )
    monkeypatch.setattr(gui_entry, "_parent_process_ids", lambda _: [20, 10])
    monkeypatch.setattr(
        gui_entry, "_bind_inherited_standard_streams", lambda _: False
    )
    monkeypatch.setattr(gui_entry, "_bind_console_streams", bind_streams)

    assert gui_entry._prepare_frozen_cli_console() is True

    kernel32.AllocConsole.assert_called_once_with()
    bind_streams.assert_called_once_with(kernel32)


def test_cli_prefers_inherited_stdio_without_allocating_console(monkeypatch):
    kernel32 = SimpleNamespace(
        AttachConsole=Mock(),
        AllocConsole=Mock(),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui_entry.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )
    bind_inherited = Mock(return_value=True)
    monkeypatch.setattr(
        gui_entry, "_bind_inherited_standard_streams", bind_inherited
    )

    assert gui_entry._prepare_frozen_cli_console() is True

    bind_inherited.assert_called_once_with(kernel32)
    kernel32.AttachConsole.assert_not_called()
    kernel32.AllocConsole.assert_not_called()


def test_frozen_build_uses_the_windows_gui_subsystem():
    spec = (PROJECT_ROOT / "douyin-monitor.spec").read_text(encoding="utf-8")

    assert "console=False" in spec
    assert "hide_console" not in spec
    assert "pystray._win32" in spec
    assert "THIRD_PARTY_NOTICES.md" in spec


def test_release_version_metadata_is_synchronized():
    version = monitor.APP_VERSION
    spec = (PROJECT_ROOT / "douyin-monitor.spec").read_text(encoding="utf-8")
    manifest = (PROJECT_ROOT / "windows-dpi.manifest").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert f"name='douyin-monitor-v{version}'" in spec
    assert f'version="{version}.0"' in manifest
    assert f"version-{version}-orange.svg" in readme
    assert f"douyin-monitor-v{version}.exe" in readme


def test_windows_tray_dependencies_are_declared():
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert 'pystray==0.19.5; sys_platform == "win32"' in requirements
    assert 'Pillow>=11.3.0; sys_platform == "win32"' in requirements
