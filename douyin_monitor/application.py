"""UI-independent application assembly for CLI and desktop frontends."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .douyin_client import DouyinClient
from .monitor_service import EventCallback, MonitorService
from .notifier import MultiChannelNotifier, PushDispatcher, ServerChanNotifier
from .push_config import selected_channels
from .streamer_config import (
    migrate_legacy_streamer,
    normalize_app_config,
    normalize_streamers,
    save_config_atomic,
)


logger = logging.getLogger(__name__)


def create_notifier(
    config: Mapping[str, Any], *, dispatcher=None, on_report=None, cancelled=None,
) -> MultiChannelNotifier:
    """Create a notification client from normalized application config."""
    return MultiChannelNotifier(
        selected_channels(config), dispatcher=dispatcher,
        on_report=on_report, cancelled=cancelled,
    )


def load_streamer_entries(
    config_path: Path,
    config: dict[str, Any],
    legacy_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize configured streamers and migrate the legacy target once."""
    normalized_config = normalize_app_config(config)
    config_changed = normalized_config != config
    config.clear()
    config.update(normalized_config)
    entries, changed = normalize_streamers(config)
    changed = changed or config_changed
    migrated = False
    if not entries and legacy_state.get("target_url"):
        try:
            migrated = migrate_legacy_streamer(config, legacy_state)
            entries, normalized = normalize_streamers(config)
            changed = changed or migrated or normalized
        except ValueError as exc:
            logger.warning("旧版主播记录无效，已跳过迁移: %s", exc)
    if changed:
        save_config_atomic(config_path, config)
    if migrated:
        logger.info("已将上次监控的主播迁移到多主播配置")
    return entries


def create_monitor_worker(
    config: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    debug: bool = False,
    client: Optional[DouyinClient] = None,
    notifier: Optional[ServerChanNotifier] = None,
    clock: Optional[Callable[[], float]] = None,
):
    """Create one notification-state worker without importing a UI module."""
    from monitor import DouyinLiveMonitor

    return DouyinLiveMonitor(
        dict(config),
        target_url=str(entry["url"]),
        debug=debug,
        daily_reminder_managed_externally=True,
        client=client,
        notifier=notifier,
        clock=clock,
        platform=str(entry.get("platform") or ""),
    )


def create_monitor_service(
    config: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    *,
    debug: bool = False,
    on_event: Optional[EventCallback] = None,
    enable_console_stop: bool = False,
) -> MonitorService:
    """Assemble clients, notifiers, workers and the shared scheduler."""
    normalized = normalize_app_config(config)
    dispatcher = PushDispatcher()
    service = None

    def report(streamer_id, delivery):
        if service is not None:
            service.record_delivery(streamer_id, delivery)

    def worker_factory(entry: Mapping[str, Any]):
        worker = create_monitor_worker(
            normalized,
            entry,
            debug=debug,
            notifier=create_notifier(
                normalized, dispatcher=dispatcher,
                on_report=lambda delivery: report(str(entry["id"]), delivery),
            ),
        )
        worker.notifier.cancelled = lambda: worker._stop_event.is_set() or (
            service is not None and service._stop_event.is_set()
        )
        worker.notifier.streamer_id = str(entry["id"])
        return worker

    try:
        service = MonitorService(
            entries,
            worker_factory=worker_factory,
            check_interval=normalized["check_interval"],
            max_concurrent_checks=normalized["max_concurrent_checks"],
            startup_notify=normalized["startup_notify"],
            on_event=on_event,
            enable_daily_intimacy_reminder=normalized["enable_daily_intimacy_reminder"],
            service_notifier=create_notifier(
                normalized, dispatcher=dispatcher,
                on_report=lambda delivery: report("", delivery),
                cancelled=lambda: service is not None and service._stop_event.is_set(),
            ),
            enable_console_stop=enable_console_stop,
            push_dispatcher=dispatcher,
        )
    except Exception:
        dispatcher.close()
        raise
    return service
