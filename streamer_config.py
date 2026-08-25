"""Persistent multi-streamer configuration helpers."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from urllib.parse import urlparse


STREAMERS_KEY = "streamers"
DEFAULT_MAX_CONCURRENT_CHECKS = 2
MAX_STREAMERS = 100
MAX_URL_LENGTH = 2048
MAX_LABEL_LENGTH = 80
STREAMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_streamer_url(url: str) -> str:
    """Return a trimmed Douyin URL or raise ``ValueError``."""
    value = (url or "").strip()
    if not value:
        raise ValueError("主播链接不能为空")
    if len(value) > MAX_URL_LENGTH:
        raise ValueError(f"主播链接不能超过 {MAX_URL_LENGTH} 个字符")
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = ("douyin.com", "iesdouyin.com")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("主播链接不能包含账号凭据")
    if parsed.scheme not in {"http", "https"} or not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in allowed_hosts
    ):
        raise ValueError("该链接不是可识别的抖音链接")
    return value


def make_streamer(url: str, label: str = "") -> Dict[str, Any]:
    """Create one persistent streamer entry with a stable local ID."""
    clean_label = (label or "").strip()
    if len(clean_label) > MAX_LABEL_LENGTH:
        raise ValueError(f"主播名称不能超过 {MAX_LABEL_LENGTH} 个字符")
    return {
        "id": uuid.uuid4().hex[:12],
        "url": validate_streamer_url(url),
        "label": clean_label,
        "enabled": True,
    }


def normalize_streamers(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """Validate streamer entries and add missing v1.2 fields in memory."""
    raw_entries = config.get(STREAMERS_KEY, [])
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise ValueError("配置项 streamers 必须是列表")
    if len(raw_entries) > MAX_STREAMERS:
        raise ValueError(f"主播数量不能超过 {MAX_STREAMERS} 个")

    normalized: List[Dict[str, Any]] = []
    ids = set()
    urls = set()
    changed = STREAMERS_KEY not in config

    for index, raw in enumerate(raw_entries, start=1):
        if isinstance(raw, str):
            entry = make_streamer(raw)
            changed = True
        elif isinstance(raw, Mapping):
            url = validate_streamer_url(str(raw.get("url", "")))
            streamer_id = str(raw.get("id", "")).strip()
            if not streamer_id:
                streamer_id = uuid.uuid4().hex[:12]
                changed = True
            elif not STREAMER_ID_PATTERN.fullmatch(streamer_id):
                raise ValueError(f"第 {index} 个主播的 ID 格式无效")
            enabled = raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError(f"第 {index} 个主播的 enabled 必须是布尔值")
            label = str(raw.get("label") or "").strip()
            if len(label) > MAX_LABEL_LENGTH:
                raise ValueError(
                    f"第 {index} 个主播名称不能超过 {MAX_LABEL_LENGTH} 个字符"
                )
            entry = dict(raw)
            entry.update(
                {
                    "id": streamer_id,
                    "url": url,
                    "label": label,
                    "enabled": enabled,
                }
            )
            if dict(raw) != entry:
                changed = True
        else:
            raise ValueError(f"第 {index} 个主播配置格式无效")

        if entry["id"] in ids:
            raise ValueError(f"主播 ID 重复: {entry['id']}")
        if entry["url"] in urls:
            raise ValueError(f"主播链接重复: {entry['url']}")
        ids.add(entry["id"])
        urls.add(entry["url"])
        normalized.append(entry)

    config[STREAMERS_KEY] = normalized
    max_checks = config.get(
        "max_concurrent_checks", DEFAULT_MAX_CONCURRENT_CHECKS
    )
    try:
        max_checks = int(max_checks)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_concurrent_checks 必须是整数") from exc
    if max_checks < 1 or max_checks > 8:
        raise ValueError("max_concurrent_checks 必须在 1 到 8 之间")
    if config.get("max_concurrent_checks") != max_checks:
        changed = True
    config["max_concurrent_checks"] = max_checks
    return normalized, changed


def migrate_legacy_streamer(
    config: Dict[str, Any], legacy_state: Mapping[str, Any]
) -> bool:
    """Import the old single target without deleting the legacy state."""
    entries, changed = normalize_streamers(config)
    if entries:
        return changed

    legacy_url = str(legacy_state.get("target_url", "")).strip()
    if not legacy_url:
        return changed

    label = str(legacy_state.get("nickname", "")).strip()
    entries.append(make_streamer(legacy_url, label=label))
    config[STREAMERS_KEY] = entries
    return True


def save_config_atomic(config_path: Path, config: Mapping[str, Any]) -> None:
    """Atomically replace a JSON config without leaving partial contents."""
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, config_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def add_streamer(
    config: Dict[str, Any], url: str, label: str = ""
) -> Dict[str, Any]:
    """Append a unique streamer entry and return it."""
    entries, _ = normalize_streamers(config)
    if len(entries) >= MAX_STREAMERS:
        raise ValueError(f"主播数量不能超过 {MAX_STREAMERS} 个")
    clean_url = validate_streamer_url(url)
    if any(entry["url"] == clean_url for entry in entries):
        raise ValueError("该主播链接已经在监控列表中")
    entry = make_streamer(clean_url, label=label)
    entries.append(entry)
    config[STREAMERS_KEY] = entries
    return entry


def remove_streamer(config: Dict[str, Any], streamer_id: str) -> Dict[str, Any]:
    """Remove one streamer by exact ID and return the removed entry."""
    entries, _ = normalize_streamers(config)
    exact_id = (streamer_id or "").strip()
    for index, entry in enumerate(entries):
        if entry["id"] == exact_id:
            removed = entries.pop(index)
            config[STREAMERS_KEY] = entries
            return removed
    raise ValueError(f"未找到主播 ID: {exact_id}")


def set_streamer_enabled(
    config: Dict[str, Any], streamer_id: str, enabled: bool
) -> Dict[str, Any]:
    """Enable or disable one streamer by exact ID."""
    entries, _ = normalize_streamers(config)
    exact_id = (streamer_id or "").strip()
    for entry in entries:
        if entry["id"] == exact_id:
            entry["enabled"] = bool(enabled)
            return entry
    raise ValueError(f"未找到主播 ID: {exact_id}")


def enabled_streamers(entries: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return enabled streamer entries as independent dictionaries."""
    return [dict(entry) for entry in entries if bool(entry.get("enabled", True))]
