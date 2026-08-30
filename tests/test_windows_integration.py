from pathlib import Path
from unittest.mock import Mock

from douyin_monitor import windows_integration
from douyin_monitor.windows_integration import WindowsInstanceGuard


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeRegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, *_args):
        return FakeRegistryKey()

    def CreateKeyEx(self, *_args):
        return FakeRegistryKey()

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.values[name] = (value, _kind)

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def fake_kernel(**overrides):
    defaults = {
        "CreateMutexW": Mock(return_value=101),
        "OpenMutexW": Mock(return_value=0),
        "CreateEventW": Mock(return_value=201),
        "OpenEventW": Mock(return_value=0),
        "WaitForSingleObject": Mock(return_value=windows_integration.WAIT_TIMEOUT),
        "SetEvent": Mock(return_value=True),
        "ReleaseMutex": Mock(return_value=True),
        "CloseHandle": Mock(return_value=True),
        "GetLastError": Mock(return_value=0),
    }
    defaults.update(overrides)
    return type("FakeKernel32", (), defaults)()


def test_gui_guard_owns_both_mutexes_and_consumes_activation_event():
    kernel = fake_kernel(
        CreateMutexW=Mock(side_effect=[101, 102]),
        WaitForSingleObject=Mock(
            side_effect=[windows_integration.WAIT_OBJECT_0, windows_integration.WAIT_TIMEOUT]
        ),
    )

    guard, owner = WindowsInstanceGuard.acquire_gui(kernel)

    assert owner == "acquired"
    assert guard is not None
    assert guard.activation_requested() is True
    assert guard.activation_requested() is False
    guard.close()
    assert kernel.ReleaseMutex.call_count == 2
    assert kernel.CloseHandle.call_count == 3


def test_second_gui_signals_existing_window_without_acquiring_monitor_lock():
    kernel = fake_kernel(
        CreateMutexW=Mock(return_value=301),
        GetLastError=Mock(return_value=windows_integration.ERROR_ALREADY_EXISTS),
        OpenMutexW=Mock(return_value=401),
        OpenEventW=Mock(return_value=402),
    )

    guard, owner = WindowsInstanceGuard.acquire_gui(kernel)

    assert guard is None
    assert owner == "gui"
    kernel.SetEvent.assert_called_once_with(402)


def test_monitor_guard_rejects_a_second_monitor():
    kernel = fake_kernel(
        CreateMutexW=Mock(return_value=301),
        GetLastError=Mock(return_value=windows_integration.ERROR_ALREADY_EXISTS),
    )

    assert WindowsInstanceGuard.acquire_monitor(kernel) is None


def test_autostart_command_quotes_paths_and_carries_config():
    executable = Path("C:/Program Folder/douyin monitor.exe")
    config = Path("C:/Config Folder/config.json")

    command = windows_integration.build_autostart_command(
        config, executable=executable, frozen=True
    )

    assert command.startswith(f'"{executable.resolve()}"')
    assert f'"{config.resolve()}"' in command
    assert command.endswith("--autostart")
    assert "--gui" in command


def test_source_autostart_command_uses_root_entry():
    command = windows_integration.build_autostart_command(
        Path("C:/Config/config.json"),
        executable=Path("C:/Python/python.exe"),
        frozen=False,
    )

    assert str(PROJECT_ROOT / "gui_entry.py") in command


def test_autostart_registry_is_idempotent_and_restorable(monkeypatch, tmp_path):
    registry = FakeRegistry()
    command = '"C:\\App\\monitor.exe" --gui --autostart'
    monkeypatch.setattr(
        windows_integration, "build_autostart_command", lambda _path: command
    )

    windows_integration.set_autostart_enabled(True, tmp_path, registry)
    windows_integration.set_autostart_enabled(True, tmp_path, registry)
    assert windows_integration.get_autostart_value(registry) == command
    assert windows_integration.is_autostart_enabled(tmp_path, registry) is True

    old_snapshot = ("old-command", 2)
    windows_integration.restore_autostart_value(old_snapshot, registry)
    assert windows_integration.get_autostart_value(registry) == "old-command"
    assert windows_integration.get_autostart_snapshot(registry) == old_snapshot
    windows_integration.set_autostart_enabled(False, Path(tmp_path), registry)
    assert windows_integration.get_autostart_value(registry) is None


def test_stale_autostart_path_is_not_reported_as_enabled(monkeypatch, tmp_path):
    registry = FakeRegistry()
    registry.values[windows_integration.AUTOSTART_VALUE_NAME] = (
        "old-command",
        registry.REG_SZ,
    )
    monkeypatch.setattr(
        windows_integration,
        "build_autostart_command",
        lambda _path: "current-command",
    )

    assert windows_integration.is_autostart_enabled(tmp_path, registry) is False


def test_autostart_uses_default_registry_module(monkeypatch, tmp_path):
    registry = FakeRegistry()
    monkeypatch.setattr(windows_integration, "_registry_module", lambda _value=None: registry)
    monkeypatch.setattr(
        windows_integration,
        "build_autostart_command",
        lambda _path: "current-command",
    )

    windows_integration.set_autostart_enabled(True, tmp_path)

    assert windows_integration.get_autostart_value() == "current-command"


def test_autostart_command_rejects_windows_run_key_overflow(tmp_path):
    long_name = "x" * 250
    with __import__("pytest").raises(ValueError, match="260"):
        windows_integration.build_autostart_command(
            tmp_path / long_name,
            executable=tmp_path / "monitor.exe",
            frozen=True,
        )
