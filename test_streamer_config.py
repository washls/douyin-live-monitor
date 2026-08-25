import json

import pytest

from streamer_config import (
    add_streamer,
    migrate_legacy_streamer,
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


def test_streamer_url_rejects_non_douyin_hostname():
    config = {"streamers": [], "max_concurrent_checks": 2}

    with pytest.raises(ValueError, match="不是可识别的抖音链接"):
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
