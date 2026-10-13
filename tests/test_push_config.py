from copy import deepcopy

import pytest

from douyin_monitor.push_config import (
    CHANNEL_FIELDS,
    CHANNEL_TYPES,
    has_push_targets,
    normalize_channel,
    normalize_push_config,
    selected_channels,
)


def channel(channel_type="webhook", channel_id="one", **config):
    return {"id": channel_id, "type": channel_type, "name": "示例渠道", "config": config}


@pytest.mark.parametrize(("channel_type", "config"), [
    ("serverchan", {"push_url": "https://example.push.ft07.com/send/example-key.send"}),
    ("pushplus", {"token": "example-token"}),
    ("bark", {"device_key": "example-key"}),
    ("feishu", {"webhook_url": "https://example.invalid/feishu?token=example-token"}),
    ("dingtalk", {"webhook_url": "https://example.invalid/dingtalk?token=example-token"}),
    ("ntfy", {"topic": "example-topic"}),
    ("webhook", {"url": "https://example.invalid/webhook"}),
])
def test_every_supported_channel_normalizes_complete_defaults_without_mutation(channel_type, config):
    raw = channel(channel_type, **config)
    original = deepcopy(raw)

    normalized = normalize_channel(raw)

    assert normalized["type"] == channel_type
    assert normalized["id"] == "one"
    assert set(normalized) == {"id", "type", "name", "config"}
    assert set(normalized["config"]) == {field["key"] for field in CHANNEL_FIELDS[channel_type]}
    assert raw == original
    assert normalize_channel(normalized) == normalized


def test_channel_types_and_field_metadata_cover_exactly_the_same_providers():
    assert set(CHANNEL_TYPES) == set(CHANNEL_FIELDS)


def test_legacy_serverchan_migrates_once_with_a_stable_id_and_preserves_uid_only_compatibility():
    legacy = {"sendkey": "example-key", "push_uid": ""}

    normalized = normalize_push_config(legacy)

    assert normalized == {
        "channels": [{
            "id": "legacy_serverchan", "type": "serverchan", "name": "Server酱³",
            "config": {"push_url": "", "sendkey": "example-key", "uid": ""},
        }],
        "push_mode": "all", "selected_channel_ids": [],
    }
    assert normalize_push_config(normalized) == normalized
    assert legacy == {"sendkey": "example-key", "push_uid": ""}
    assert has_push_targets(legacy)


def test_url_legacy_migration_takes_precedence_over_separate_credentials():
    normalized = normalize_push_config({
        "push_url": "https://example.push.ft07.com/send/example-key.send/",
        "sendkey": "old-key", "push_uid": "old-uid",
    })
    assert normalized["channels"][0]["config"] == {
        "push_url": "https://example.push.ft07.com/send/example-key.send", "sendkey": "", "uid": "",
    }


def test_legacy_numeric_uid_keeps_the_previous_string_conversion():
    normalized = normalize_push_config({"sendkey": "example-key", "push_uid": 12345})
    assert normalized["channels"][0]["config"]["uid"] == "12345"


@pytest.mark.parametrize("legacy", [
    {},
    {"push_url": "YOUR_PUSH_URL_HERE", "sendkey": "YOUR_SENDKEY_HERE"},
    {"channels": [], "push_url": "https://example.push.ft07.com/send/example-key.send"},
])
def test_missing_credentials_and_explicit_empty_channels_do_not_enable_push(legacy):
    assert normalize_push_config(legacy)["channels"] == []
    assert not has_push_targets(legacy)


def test_all_mode_preserves_saved_selection_and_selected_mode_uses_channel_order():
    channels = [channel(channel_id=item, url=f"https://example.invalid/{item}") for item in ("one", "two", "three")]
    config = {"channels": channels, "push_mode": "all", "selected_channel_ids": ["three", "one"]}

    assert [item["id"] for item in selected_channels(config)] == ["one", "two", "three"]
    assert config["selected_channel_ids"] == ["three", "one"]
    config["push_mode"] = "selected"
    assert [item["id"] for item in selected_channels(config)] == ["one", "three"]
    config["selected_channel_ids"] = []
    assert selected_channels(config) == []
    assert not has_push_targets(config)


@pytest.mark.parametrize("settings", [
    {"channels": None},
    {"channels": {}},
    {"push_mode": "fallback"},
    {"push_mode": []},
    {"selected_channel_ids": "one"},
    {"selected_channel_ids": [1]},
    {"selected_channel_ids": ["one", "one"]},
    {"selected_channel_ids": ["missing"]},
])
def test_invalid_global_push_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        normalize_push_config({"channels": [channel(url="https://example.invalid/hook")], **settings})


@pytest.mark.parametrize("changes", [
    {"id": ""}, {"id": "bad id"}, {"id": "x" * 65}, {"type": "unknown"},
    {"name": "x" * 81}, {"config": []}, {"config": {"url": "https://example.invalid/hook", "typo": "value"}},
])
def test_invalid_channel_structure_is_rejected(changes):
    with pytest.raises(ValueError):
        normalize_channel({**channel(url="https://example.invalid/hook"), **changes})


def test_unselected_channels_still_require_complete_configuration():
    with pytest.raises(ValueError, match="不能为空"):
        normalize_push_config({
            "channels": [channel("pushplus", token="")],
            "push_mode": "selected", "selected_channel_ids": [],
        })


def test_duplicate_ids_and_equivalent_destinations_are_rejected_without_echoing_secrets():
    first = channel(url="https://example.invalid:443/hook?a=1&b=2")
    with pytest.raises(ValueError, match="ID 重复"):
        normalize_push_config({"channels": [first, channel(url="https://example.invalid/other")]})
    with pytest.raises(ValueError, match="目的地重复"):
        normalize_push_config({"channels": [first, channel(channel_id="two", url="https://EXAMPLE.invalid/hook?b=2&a=1")]})
    with pytest.raises(ValueError, match="目的地重复"):
        normalize_push_config({"channels": [
            channel("serverchan", push_url="https://example.push.ft07.com/send/example-key.send"),
            channel("serverchan", channel_id="two", sendkey="example-key", uid="example"),
        ]})


def test_same_provider_can_target_multiple_devices_and_topics():
    config = {"channels": [
        channel("bark", "one", device_key="device-one"),
        channel("bark", "two", device_key="device-two"),
        channel("ntfy", "three", topic="topic-one"),
        channel("ntfy", "four", topic="topic-two"),
    ]}
    assert len(selected_channels(config)) == 4


@pytest.mark.parametrize("second", [
    {"push_url": "https://EXAMPLE.push.ft07.com/send/example-key.send"},
    {"sendkey": "example-key", "uid": "EXAMPLE"},
])
def test_serverchan_destination_uid_is_case_insensitive_for_urls_and_legacy_fields(second):
    with pytest.raises(ValueError, match="目的地重复"):
        normalize_push_config({"channels": [
            channel("serverchan", push_url="https://example.push.ft07.com/send/example-key.send"),
            channel("serverchan", "two", **second),
        ]})


def test_destination_tokens_and_paths_remain_case_sensitive():
    config = {"channels": [
        channel("serverchan", "one", sendkey="Example-Key", uid="example"),
        channel("serverchan", "two", sendkey="example-key", uid="example"),
        channel("pushplus", "three", token="Example-Token"),
        channel("pushplus", "four", token="example-token"),
        channel("webhook", "five", url="https://example.invalid/Hook"),
        channel("webhook", "six", url="https://example.invalid/hook"),
    ]}
    assert len(selected_channels(config)) == 6


@pytest.mark.parametrize(("channel_type", "config", "field"), [
    ("bark", {"server_url": "http://127.0.0.1:8080/proxy/bark/", "device_key": "example-key"}, "server_url"),
    ("ntfy", {"server_url": "http://127.0.0.1:8080/proxy/ntfy/", "topic": "example-topic"}, "server_url"),
    ("webhook", {"url": "http://127.0.0.1:8080/proxy/hook?key=example-key"}, "url"),
])
def test_self_hosted_services_preserve_reverse_proxy_prefixes(channel_type, config, field):
    result = normalize_channel(channel(channel_type, **config))
    assert result["config"][field] == config[field].rstrip("/")


@pytest.mark.parametrize(("channel_type", "config"), [
    ("serverchan", {"push_url": "http://example.push.ft07.com/send/example-key.send"}),
    ("feishu", {"webhook_url": "http://example.invalid/hook"}),
    ("dingtalk", {"webhook_url": "http://example.invalid/hook"}),
    ("bark", {"server_url": "http://api.day.app", "device_key": "example-key"}),
    ("ntfy", {"server_url": "http://ntfy.sh", "topic": "example-topic"}),
    ("ntfy", {"topic": "invalid/topic"}),
    ("webhook", {"url": "https://user:example-secret@example.invalid/hook"}),
    ("webhook", {"url": "https://example.invalid/hook#example-secret"}),
    ("webhook", {"url": "https://example.invalid:99999/hook"}),
    ("webhook", {"url": "https://example.invalid/hook\nexample-secret"}),
    ("webhook", {"url": "https://example.invalid\\hook"}),
    ("webhook", {"url": "https://<example.invalid>/hook"}),
])
def test_unsafe_urls_and_topics_raise_redacted_errors(channel_type, config):
    with pytest.raises(ValueError) as caught:
        normalize_channel(channel(channel_type, **config))
    assert "example-secret" not in str(caught.value)


@pytest.mark.parametrize("headers", [
    [], {"Host": "example.invalid"}, {"Content-Type": "text/plain"},
    {"Content-Length": "4"}, {"Transfer-Encoding": "chunked"},
    {"Proxy-Authorization": "example-secret"}, {"X\nBad": "value"},
    {"X-Test": "example-secret\r\ninjected: true"}, {"X-Test": 123},
    {"X-Test": "中文"}, {"X-Test": "one", "x-test": "two"},
])
def test_webhook_headers_reject_overrides_injection_and_unencodable_values(headers):
    with pytest.raises(ValueError) as caught:
        normalize_channel(channel(url="https://example.invalid/hook", headers=headers))
    assert "example-secret" not in str(caught.value)


def test_webhook_authentication_headers_are_copied_and_not_shared_with_defaults():
    headers = {"Authorization": "Bearer example-token", "X-Client": "monitor"}
    result = normalize_channel(channel(url="https://example.invalid/hook", headers=headers))
    result["config"]["headers"]["X-Client"] = "changed"
    assert headers["X-Client"] == "monitor"
    assert normalize_channel(channel(url="https://example.invalid/other"))["config"]["headers"] == {}
