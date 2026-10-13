import json

import pytest

from douyin_monitor.streamer_config import (
    add_streamer,
    migrate_legacy_streamer,
    normalize_app_config,
    normalize_streamers,
    remove_streamer,
    save_config_atomic,
    set_streamer_enabled,
    update_streamer,
)


def test_legacy_single_target_is_imported_without_mutating_state():
    config = {"streamers": [], "max_concurrent_checks": 2}
    legacy = {
        "target_url": "https://www.douyin.com/user/example",
        "nickname": "主播甲",
    }

    changed = migrate_legacy_streamer(config, legacy)

    assert changed is True
    assert legacy["target_url"].endswith("/example")
    assert len(config["streamers"]) == 1
    assert config["streamers"][0]["label"] == "主播甲"
    assert len(config["streamers"][0]["id"]) == 12


def test_normalize_rejects_duplicate_urls():
    config = {
        "streamers": [
            {"id": "one", "url": "https://v.douyin.com/example/"},
            {"id": "two", "url": "https://v.douyin.com/example/"},
        ]
    }

    with pytest.raises(ValueError, match="主播链接重复"):
        normalize_streamers(config)


def test_normalize_rejects_unbounded_streamer_list():
    config = {
        "streamers": [
            f"https://www.douyin.com/user/example-{index}"
            for index in range(101)
        ]
    }

    with pytest.raises(ValueError, match="不能超过 100"):
        normalize_streamers(config)


def test_streamer_url_rejects_unsupported_hostname():
    config = {"streamers": [], "max_concurrent_checks": 2}

    with pytest.raises(ValueError, match="暂不支持该平台"):
        add_streamer(config, "https://example.com/?next=douyin.com")


def test_streamer_url_rejects_embedded_credentials():
    config = {"streamers": [], "max_concurrent_checks": 2}

    with pytest.raises(ValueError, match="账号凭据"):
        add_streamer(config, "https://user:pass@www.douyin.com/user/example")


def test_streamer_label_length_is_bounded():
    config = {"streamers": [], "max_concurrent_checks": 2}

    with pytest.raises(ValueError, match="不能超过 80"):
        add_streamer(
            config,
            "https://www.douyin.com/user/example",
            label="a" * 81,
        )


def test_add_disable_enable_and_remove_streamer():
    config = {"streamers": [], "max_concurrent_checks": 2}
    entry = add_streamer(
        config,
        "https://www.douyin.com/user/example",
        label="主播甲",
    )

    assert set_streamer_enabled(config, entry["id"], False)["enabled"] is False
    assert set_streamer_enabled(config, entry["id"], True)["enabled"] is True
    assert remove_streamer(config, entry["id"])["id"] == entry["id"]
    assert config["streamers"] == []


def test_update_streamer_preserves_id_and_rejects_duplicate_url():
    config = {"streamers": [], "max_concurrent_checks": 2}
    first = add_streamer(config, "https://www.douyin.com/user/first")
    second = add_streamer(config, "https://www.douyin.com/user/second")

    updated = update_streamer(
        config,
        first["id"],
        "https://www.douyin.com/user/updated",
        label="更新后的主播",
        enabled=False,
    )

    assert updated["id"] == first["id"]
    assert updated["label"] == "更新后的主播"
    assert updated["enabled"] is False
    with pytest.raises(ValueError, match="已经在监控列表中"):
        update_streamer(config, first["id"], second["url"])


def test_remove_requires_exact_id():
    config = {"streamers": [], "max_concurrent_checks": 2}
    entry = add_streamer(config, "https://v.douyin.com/example/")

    with pytest.raises(ValueError, match="未找到主播 ID"):
        remove_streamer(config, entry["id"][:6])


def test_atomic_save_writes_valid_json_and_leaves_no_temp_file(tmp_path):
    config_path = tmp_path / "config.json"
    config = {
        "push_url": "",
        "streamers": [],
        "max_concurrent_checks": 2,
    }

    save_config_atomic(config_path, config)

    assert json.loads(config_path.read_text(encoding="utf-8")) == config
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_save_creates_private_temp_file(tmp_path, monkeypatch):
    original_open = __import__("os").open
    modes = []

    def recording_open(path, flags, mode):
        modes.append(mode)
        return original_open(path, flags, mode)

    monkeypatch.setattr("douyin_monitor.streamer_config.os.open", recording_open)

    save_config_atomic(tmp_path / "config.json", {"streamers": []})

    assert modes == [0o600]


def test_app_config_normalizes_numeric_strings_and_http_urls():
    normalized = normalize_app_config({
        "check_interval": "60",
        "repeat_notify_interval": "120",
        "max_repeat_notifications": "4",
        "max_concurrent_checks": "3",
        "notify_on_stream_end": True,
        "startup_notify": False,
        "enable_daily_intimacy_reminder": True,
        "streamers": [{"id": "one", "url": "http://v.douyin.com/a/"}],
    })

    assert normalized["check_interval"] == 60
    assert normalized["streamers"][0]["url"] == "https://v.douyin.com/a/"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_app_config_rejects_non_boolean_flags(value):
    with pytest.raises(ValueError, match="必须是布尔值"):
        normalize_app_config({"startup_notify": value})


def test_app_config_defaults_and_validates_close_action():
    assert normalize_app_config({})["close_action"] == "exit"
    assert (
        normalize_app_config({"close_action": "hide_to_tray"})["close_action"]
        == "hide_to_tray"
    )

    with pytest.raises(ValueError, match="close_action"):
        normalize_app_config({"close_action": "minimize"})
    with pytest.raises(ValueError, match="close_action"):
        normalize_app_config({"close_action": []})


def test_posix_config_permissions_are_private(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("douyin_monitor.streamer_config._IS_POSIX", True)
    chmod_calls = []
    monkeypatch.setattr(
        "douyin_monitor.streamer_config.os.chmod",
        lambda path, mode: chmod_calls.append((path, mode)),
    )

    save_config_atomic(config_path, {"streamers": []})

    assert chmod_calls == [(config_path, 0o600)]


def test_push_upgrade_preserves_original_bytes_once_and_unknown_app_settings(tmp_path):
    path = tmp_path / "custom.json"
    original = b'{ "sendkey": "example-key", "push_uid": "example", "custom": {"keep": true}, "streamers": [] }\r\n'
    path.write_bytes(original)

    config = normalize_app_config(json.loads(original))
    save_config_atomic(path, config)

    backup = path.with_name("custom.json.pre-v1.9.0.bak")
    assert backup.read_bytes() == original
    assert config["custom"] == {"keep": True}
    assert config["channels"][0]["id"] == "legacy_serverchan"
    assert not {"sendkey", "push_uid", "push_url"} & config.keys()
    config["push_mode"] = "selected"
    save_config_atomic(path, config)
    assert backup.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_combined_push_and_platform_upgrade_keeps_both_original_backups(tmp_path):
    path = tmp_path / "config.json"
    original = b'{"push_url":"","streamers":[{"id":"one","url":"https://www.douyin.com/user/example"}]}'
    path.write_bytes(original)

    save_config_atomic(path, normalize_app_config(json.loads(original)))

    for version in ("1.8.0", "1.9.0"):
        assert path.with_name(f"config.json.pre-v{version}.bak").read_bytes() == original


@pytest.mark.parametrize("operation", ["fsync", "link"])
def test_push_upgrade_backup_failure_preserves_original_and_leaves_no_temp(tmp_path, monkeypatch, operation):
    path = tmp_path / "config.json"
    original = b'{"sendkey":"example-key","streamers":[]}'
    path.write_bytes(original)
    upgraded = normalize_app_config(json.loads(original))

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(f"douyin_monitor.streamer_config.os.{operation}", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        save_config_atomic(path, upgraded)

    assert path.read_bytes() == original
    assert not path.with_name("config.json.pre-v1.9.0.bak").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_failed_push_config_replace_preserves_original_and_complete_backup(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    original = b'{"streamers":[]}'
    path.write_bytes(original)

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr("douyin_monitor.streamer_config.os.replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        save_config_atomic(path, normalize_app_config(json.loads(original)))

    assert path.read_bytes() == original
    assert path.with_name("config.json.pre-v1.9.0.bak").read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_explicit_empty_push_list_discards_legacy_keys_without_restoring_a_channel():
    config = normalize_app_config({
        "channels": [], "push_mode": "selected", "selected_channel_ids": [],
        "push_url": "https://example.push.ft07.com/send/example-key.send",
    })
    assert config["channels"] == []
    assert config["push_mode"] == "selected"
    assert "push_url" not in config
