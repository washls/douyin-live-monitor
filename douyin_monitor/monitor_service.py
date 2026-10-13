"""Bounded multi-streamer scheduling shared by all user interfaces."""

from __future__ import annotations

import logging
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from .monitor_types import (
    MonitorEvent,
    StreamerSnapshot,
    StreamerStatus,
    WorkerPreparation,
)
from .streamer_logging import run_with_streamer_context, streamer_log_context
from .monitor_errors import ProfileLinkError, failure_reason, transient_network_error
from .platform_clients import MIN_CHECK_INTERVALS, platform_name
from .notifier import MultiChannelNotifier


logger = logging.getLogger(__name__)

WorkerFactory = Callable[[Mapping[str, Any]], Any]
EventCallback = Callable[[MonitorEvent], None]


class LiveStatusUnknownError(RuntimeError):
    """The current polling round could not determine live or offline state."""


class MonitorCheckCancelled(RuntimeError):
    """A polling round finished after its monitor had been stopped."""


class MonitorService:
    """Coordinate multiple independent monitor workers with bounded concurrency."""

    def __init__(
        self,
        streamers: List[Mapping[str, Any]],
        worker_factory: WorkerFactory,
        check_interval: int = 30,
        max_concurrent_checks: int = 2,
        startup_notify: bool = False,
        on_event: Optional[EventCallback] = None,
        enable_daily_intimacy_reminder: bool = True,
        service_notifier: Optional[Any] = None,
        enable_console_stop: bool = False,
        push_dispatcher: Optional[Any] = None,
    ):
        self.streamers = [dict(entry) for entry in streamers]
        self.worker_factory = worker_factory
        self.check_interval = int(check_interval)
        if self.check_interval < 1 or self.check_interval > 86400:
            raise ValueError("check_interval 必须在 1 到 86400 之间")
        self.max_concurrent_checks = int(max_concurrent_checks)
        if self.max_concurrent_checks < 1 or self.max_concurrent_checks > 8:
            raise ValueError("max_concurrent_checks 必须在 1 到 8 之间")
        self.startup_notify = bool(startup_notify)
        self.enable_daily_intimacy_reminder = bool(
            enable_daily_intimacy_reminder
        )
        self.on_event = on_event
        self._service_notifier = service_notifier
        self._push_dispatcher = push_dispatcher
        if not isinstance(enable_console_stop, bool):
            raise ValueError("enable_console_stop 必须是布尔值")
        self.enable_console_stop = enable_console_stop

        self.running = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._workers: Dict[str, Any] = {}
        self._runtime: Dict[str, Dict[str, Any]] = {}
        self._futures: Dict[Future, str] = {}
        self._next_due: Dict[str, float] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self._stopped_emitted = False
        self._daily_reminder_date = ""
        self._daily_reminder_minutes_sent: set[int] = set()
        self._daily_reminder_future: Optional[Future] = None
        self._notification_executor: Optional[ThreadPoolExecutor] = None
        self._preparing: Dict[str, Any] = {}
        self._preparation_futures: set[Future] = set()
        self._resolved_users: Dict[str, tuple[str, str]] = {}
        self._platform_cooldown: Dict[str, float] = {}
        self._prepare_attempts: Dict[str, int] = {}
        self._faults: Dict[str, Dict[str, Any]] = {}
        self._pending_alerts: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._alert_future: Optional[Future] = None
        self._alert_streamer = ""
        self._initial_attempts: set[str] = set()
        self._startup_sent = False

        for entry in self.streamers:
            streamer_id = str(entry["id"])
            self._runtime[streamer_id] = {
                "id": streamer_id,
                "platform": entry.get("platform", "douyin"),
                "url": entry["url"],
                "live_url": "",
                "session_id": "",
                "error_kind": "",
                "label": entry.get("label", ""),
                "nickname": entry.get("label", ""),
                "status": "pending",
                "method": "",
                "room_id": "",
                "title": "",
                "last_check": "",
                "last_error": "",
                "check_count": 0,
                "consecutive_errors": 0,
                "consecutive_unknowns": 0,
                "suspended": False,
                "stopped_by_user": False,
                "next_retry": "",
                "notification_status": "",
            }

    def _emit(self, event_type: str, streamer_id: str = "", **data: Any) -> None:
        if self.on_event is None:
            return
        event = MonitorEvent.create(event_type, streamer_id, **data)
        try:
            self.on_event(event)
        except Exception:
            logger.exception("监控事件回调异常")

    def snapshot(self) -> List[StreamerSnapshot]:
        """Return immutable thread-safe per-streamer snapshots."""
        with self._lock:
            snapshots = []
            for entry in self.streamers:
                runtime = dict(self._runtime[str(entry["id"])])
                try:
                    runtime["status"] = StreamerStatus(runtime["status"])
                except ValueError:
                    runtime["status"] = StreamerStatus.UNKNOWN
                snapshots.append(StreamerSnapshot(**runtime))
            return snapshots

    @staticmethod
    def initial_offsets(count: int, check_interval: int) -> List[float]:
        """Spread initial checks without delaying small lists excessively."""
        if count <= 0:
            return []
        spacing = min(2.0, max(0.25, float(check_interval) / count))
        return [index * spacing for index in range(count)]

    def _platform(self, streamer_id: str) -> str:
        return str(self._runtime[streamer_id]["platform"] or "douyin")

    def _check_delay(self, streamer_id: str) -> float:
        return float(max(self.check_interval, MIN_CHECK_INTERVALS.get(self._platform(streamer_id), 0)))

    @staticmethod
    def _preparation(prepared: Any) -> WorkerPreparation:
        if not isinstance(prepared, WorkerPreparation):
            prepared = WorkerPreparation(sec_uid=str(prepared or ""))
        if not prepared.identity[1]:
            raise ProfileLinkError("无法从主播链接中提取账号，请使用主播主页或直播间链接")
        return prepared

    def _bounded_futures(self, executor: ThreadPoolExecutor, jobs: Mapping[str, Callable]):
        """Submit only runnable jobs; the executor never receives a waiting backlog."""
        pending = list(jobs)
        active: Dict[Future, str] = {}
        while pending or active:
            busy = {self._platform(streamer_id) for streamer_id in active.values()}
            for streamer_id in list(pending):
                if self._stop_event.is_set():
                    break
                if len(active) >= self.max_concurrent_checks:
                    break
                platform = self._platform(streamer_id)
                if platform in busy or self._runtime[streamer_id]["stopped_by_user"]:
                    continue
                pending.remove(streamer_id)
                cooldown = self._platform_cooldown.get(platform, 0) - time.monotonic()
                if cooldown > 0:
                    exc = LiveStatusUnknownError("平台请求频率受限")
                    exc.error_kind, exc.cooldown = "rate_limited", cooldown
                    future = Future()
                    future.set_exception(exc)
                    yield streamer_id, future
                    continue
                active[executor.submit(run_with_streamer_context, streamer_id, jobs[streamer_id])] = streamer_id
                busy.add(platform)
            if not active:
                break
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                yield active.pop(future), future

    def prepare_all(self) -> List[str]:
        """Create and resolve workers, then drop duplicate resolved users."""
        with self._lock:
            if self._workers:
                return list(self._workers)

        candidates: Dict[str, Any] = {}
        for entry in self.streamers:
            streamer_id = str(entry["id"])
            try:
                candidates[streamer_id] = run_with_streamer_context(
                    streamer_id, self.worker_factory, entry
                )
            except Exception as exc:
                self._mark_prepare_error(streamer_id, exc)

        prepared_identities: Dict[str, tuple[str, str]] = {}
        handled: set[str] = set()
        if candidates:
            with self._lock:
                self._preparing.update(candidates)
            with ThreadPoolExecutor(
                max_workers=min(self.max_concurrent_checks, len(candidates)),
                thread_name_prefix="monitor-prepare",
            ) as executor:
                jobs = {streamer_id: worker.prepare for streamer_id, worker in candidates.items()}
                for streamer_id, future in self._bounded_futures(executor, jobs):
                    handled.add(streamer_id)
                    try:
                        preparation = self._preparation(future.result())
                        if self._stop_event.is_set() or self._runtime[streamer_id]["stopped_by_user"]:
                            self._close_candidate(streamer_id, candidates[streamer_id])
                            continue
                        prepared_identities[streamer_id] = preparation.identity
                        with self._lock:
                            runtime = self._runtime[streamer_id]
                            runtime["platform"] = preparation.platform
                            runtime["url"] = preparation.url or runtime["url"]
                            runtime["nickname"] = (
                                preparation.nickname or runtime["label"]
                            )
                            runtime["status"] = preparation.status.value
                    except Exception as exc:
                        self._mark_prepare_error(streamer_id, exc)
                        self._close_candidate(
                            streamer_id, candidates[streamer_id]
                        )
                    finally:
                        with self._lock:
                            self._preparing.pop(streamer_id, None)
            for streamer_id in candidates.keys() - handled:
                self._close_candidate(streamer_id, candidates[streamer_id])
                with self._lock:
                    self._preparing.pop(streamer_id, None)

        seen_identities: Dict[tuple[str, str], str] = {}
        for entry in self.streamers:
            streamer_id = str(entry["id"])
            identity = prepared_identities.get(streamer_id)
            if not identity:
                continue
            duplicate_of = seen_identities.get(identity)
            if duplicate_of:
                message = f"与主播 {duplicate_of} 指向同一账号，已跳过"
                self._mark_prepare_error(streamer_id, RuntimeError(message))
                self._close_candidate(streamer_id, candidates[streamer_id])
                continue
            seen_identities[identity] = streamer_id
            worker = candidates[streamer_id]
            with self._lock:
                self._workers[streamer_id] = worker
                runtime = self._runtime[streamer_id]
                runtime["last_error"] = ""
                nickname = runtime["nickname"]
            self._emit("prepared", streamer_id, nickname=nickname)
            self._resolved_users[streamer_id] = identity

        with self._lock:
            return list(self._workers)

    @staticmethod
    def _close_candidate(streamer_id: str, worker: Any) -> None:
        """Release a worker rejected during preparation."""
        stop_and_wait = getattr(worker, "stop_and_wait", None)
        if callable(stop_and_wait):
            run_with_streamer_context(streamer_id, stop_and_wait)
            return
        run_with_streamer_context(streamer_id, worker.stop)
        close = getattr(worker, "close", None)
        if callable(close):
            run_with_streamer_context(streamer_id, close)

    def _mark_prepare_error(self, streamer_id: str, exc: Exception) -> None:
        message = self._error_reason(exc)
        self._apply_platform_cooldown(streamer_id, exc)
        if "指向同一账号" in str(exc):
            message = "与已解析主播指向同一账号，已跳过"
        with streamer_log_context(streamer_id):
            logger.error("主播 %s 初始化失败: %s", streamer_id, message)
        with self._lock:
            runtime = self._runtime[streamer_id]
            if runtime["stopped_by_user"] or self._stop_event.is_set():
                return
            runtime["status"] = "error"
            runtime["last_error"] = message
            runtime["error_kind"] = str(getattr(exc, "error_kind", ""))
            runtime["suspended"] = True
            runtime["next_retry"] = ""
        self._emit("prepare_error", streamer_id, error=message)

    def _prepare_worker(self, entry: Mapping[str, Any]) -> tuple[Any, WorkerPreparation]:
        streamer_id = str(entry["id"])
        worker = self.worker_factory(entry)
        with self._lock:
            self._preparing[streamer_id] = worker
            cancelled = self._stop_event.is_set() or self._runtime[streamer_id]["stopped_by_user"]
        try:
            if cancelled:
                raise MonitorCheckCancelled("监控任务已停止")
            prepared = self._preparation(worker.prepare())
            return worker, prepared
        except Exception:
            self._close_candidate(streamer_id, worker)
            raise
        finally:
            with self._lock:
                self._preparing.pop(streamer_id, None)

    def _collect_preparation(self, streamer_id: str, future: Future) -> float:
        self._initial_attempts.add(streamer_id)
        runtime = self._runtime[streamer_id]
        cancelled = self._stop_event.is_set() or runtime["stopped_by_user"]
        try:
            worker, prepared = future.result()
        except Exception as exc:
            if cancelled or isinstance(exc, MonitorCheckCancelled):
                return 0.0
            error_kind = str(getattr(exc, "error_kind", ""))
            self._apply_platform_cooldown(streamer_id, exc)
            if transient_network_error(exc) or error_kind in {"network", "restricted", "rate_limited", "parse", "parse_error"}:
                attempts = self._prepare_attempts.get(streamer_id, 0)
                delay = (10.0, 30.0, 60.0, 300.0)[min(attempts, 3)]
                delay = max(delay, self._platform_cooldown.get(self._platform(streamer_id), 0) - time.monotonic())
                self._prepare_attempts[streamer_id] = attempts + 1
                message = self._error_reason(exc)
                with self._lock:
                    if runtime["stopped_by_user"] or self._stop_event.is_set():
                        return 0.0
                    runtime.update(status="retrying", last_error=message, error_kind=error_kind,
                                   next_retry=datetime.fromtimestamp(time.time() + delay).strftime("%H:%M:%S"))
                self._start_fault(streamer_id, message)
                logger.warning("主播 %s 初始化暂时失败: %s，%s 秒后重试", streamer_id, message, delay)
                self._emit("retrying", streamer_id, error=message, retry_in=delay)
                return delay
            self._mark_prepare_error(streamer_id, exc)
            self._start_fault(streamer_id, failure_reason(exc))
            self._queue_fault_alert(streamer_id, "suspended")
            return 0.0
        if cancelled:
            self._close_candidate(streamer_id, worker)
            return 0.0
        if prepared.identity in self._resolved_users.values():
            self._mark_prepare_error(streamer_id, RuntimeError("指向同一账号"))
            self._faults.pop(streamer_id, None)
            self._discard_alerts(streamer_id)
            self._close_candidate(streamer_id, worker)
            return 0.0
        with self._lock:
            # stop_streamer may have run while future.result() was collected.
            cancelled = runtime["stopped_by_user"] or self._stop_event.is_set()
            if not cancelled:
                self._workers[streamer_id] = worker
                self._resolved_users[streamer_id] = prepared.identity
                runtime.update(status=prepared.status.value, last_error="", next_retry="",
                               nickname=prepared.nickname or runtime["label"], platform=prepared.platform,
                               url=prepared.url or runtime["url"], error_kind="")
        if cancelled:
            self._close_candidate(streamer_id, worker)
        else:
            self._emit("prepared", streamer_id, nickname=runtime["nickname"])
        return 0.0

    def _start_fault(self, streamer_id: str, reason: str) -> None:
        fault = self._faults.setdefault(streamer_id, {
            "started": time.monotonic(), "alerted": False, "suspended": False,
        })
        fault["reason"] = reason

    def _queue_fault_alert(self, streamer_id: str, kind: str) -> None:
        fault = self._faults[streamer_id]
        flag = "suspended" if kind == "suspended" else "alerted"
        if fault[flag] or self._runtime[streamer_id]["stopped_by_user"]:
            return
        fault[flag] = True
        self._queue_alert(streamer_id, kind, fault)

    def _queue_alert(self, streamer_id: str, kind: str, fault: Mapping[str, Any]) -> None:
        runtime = self._runtime[streamer_id]
        # At most three pending events per streamer; newer events of the same
        # kind replace older unsent events and retain chronological ordering.
        key = (streamer_id, kind)
        if kind != "recovery":
            self._pending_alerts.pop((streamer_id, "recovery"), None)
        self._pending_alerts.pop(key, None)
        self._pending_alerts[key] = {
            "kind": kind,
            "nickname": runtime["nickname"] or runtime["label"] or "未命名主播",
            "reason": fault["reason"],
            "duration": max(0, int(time.monotonic() - fault["started"])),
            "platform": runtime["platform"],
            "live_url": runtime["live_url"] or runtime["url"],
        }

    def _discard_alerts(self, streamer_id: str) -> None:
        for key in list(self._pending_alerts):
            if key[0] == streamer_id:
                self._pending_alerts.pop(key, None)

    def record_delivery(self, streamer_id: str, report: Any) -> None:
        """Publish a credential-free immutable delivery result to frontends."""
        results = [
            {
                "channel_id": item.channel_id, "name": item.name, "type": item.type,
                "status": getattr(item.status, "value", item.status),
                "http_status": item.http_status, "error_code": item.error_code,
                "elapsed_ms": item.elapsed_ms,
            }
            for item in report.results
        ]
        with self._lock:
            if streamer_id in self._runtime:
                self._runtime[streamer_id]["notification_status"] = report.summary
        logger.info("推送结果: %s", report.summary)
        self._emit(
            "notification", streamer_id,
            delivery_report={"event_id": report.event_id, "summary": report.summary, "results": results},
        )

    def _send_alert(self, notifier: Any, event: Mapping[str, Any], streamer_id: str) -> tuple[bool, bool]:
        if isinstance(notifier, MultiChannelNotifier):
            notifier = notifier.clone()
            notifier.streamer_id = streamer_id
            notifier.cancelled = lambda: self._stop_event.is_set() or self._runtime[streamer_id]["stopped_by_user"]
            notifier.on_report = lambda report: self.record_delivery(streamer_id, report)
        if self._stop_event.is_set() or self._runtime[streamer_id]["stopped_by_user"]:
            return False, False
        with streamer_log_context(streamer_id):
            if event["kind"] == "recovery":
                success = notifier.send_monitor_recovery(
                    event["nickname"], event["duration"],
                    platform=event["platform"], live_url=event["live_url"],
                )
            else:
                success = notifier.send_monitor_alert(
                    event["nickname"], event["reason"], event["duration"], event["kind"] != "suspended",
                    platform=event["platform"], live_url=event["live_url"],
                )
        return success, bool(notifier.delivery_unknown)

    def _check_fault_notifications(self) -> None:
        future = self._alert_future
        if future is not None and future.done():
            try:
                success, unknown = future.result()
            except Exception:
                success, unknown = False, True
            message = "异常/恢复通知已发送" if success else (
                "异常/恢复通知结果未知，不重发" if unknown else "异常/恢复通知发送失败，不重发"
            )
            if isinstance(self._service_notifier, MultiChannelNotifier):
                message = self._runtime[self._alert_streamer]["notification_status"] or message
            if future.cancelled() or (not success and not unknown and (
                self._stop_event.is_set() or self._runtime[self._alert_streamer]["stopped_by_user"]
            )):
                message = "异常/恢复通知已取消"
            with self._lock:
                self._runtime[self._alert_streamer]["notification_status"] = message
            logger.info("主播 %s: %s", self._alert_streamer, message)
            self._emit("notification", self._alert_streamer)
            self._alert_future = None
        if self._stop_event.is_set():
            return
        for streamer_id, fault in list(self._faults.items()):
            if not self._runtime[streamer_id]["suspended"] and time.monotonic() - fault["started"] >= 300:
                self._queue_fault_alert(streamer_id, "error")
        if self._alert_future is not None or not self._pending_alerts or self._notification_executor is None:
            return
        key = next(iter(self._pending_alerts))
        event = self._pending_alerts.pop(key)
        if self._runtime[key[0]]["stopped_by_user"]:
            return
        self._alert_streamer = key[0]
        try:
            notifier = self._get_service_notifier()
            self._alert_future = self._notification_executor.submit(self._send_alert, notifier, event, key[0])
        except Exception:
            self._alert_future = Future()
            self._alert_future.set_result((False, False))
        self._alert_future.add_done_callback(lambda _future: self._wake_event.set())

    def _send_startup_notification(self) -> None:
        if not self.startup_notify or not self._workers or self._stop_event.is_set():
            return
        names = []
        with self._lock:
            for streamer_id in self._workers:
                runtime = self._runtime[streamer_id]
                if runtime["stopped_by_user"]:
                    continue
                name = runtime["nickname"] or runtime["label"] or "未命名主播"
                names.append(f"[{platform_name(runtime['platform'])}] {name}")
        if not names:
            return
        self._get_service_notifier().send(
            title="[START] 多平台直播监听器已启动",
            desp=(
                f"**监控主播数**: {len(names)}\n"
                f"**主播**: {', '.join(names)}\n"
                f"**检测间隔**: {self.check_interval}s\n"
                f"**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            **({"event_type": "startup"} if isinstance(self._get_service_notifier(), MultiChannelNotifier) else {}),
        )

    def _get_service_notifier(self) -> Any:
        """Return a notifier reserved for service-level notifications."""
        if self._service_notifier is not None:
            return self._service_notifier
        first_notifier = next(iter(self._workers.values())).notifier
        clone = getattr(first_notifier, "clone", None)
        if not callable(clone):
            raise RuntimeError(
                "服务级通知需要独立通知器，请传入 service_notifier"
            )
        self._service_notifier = clone()
        return self._service_notifier

    def _send_daily_intimacy_reminder(
        self,
        notifier: Any,
        live_streamers: List[Dict[str, str]],
        minute: int,
    ) -> tuple[bool, bool]:
        if self._stop_event.is_set():
            return False, False
        with self._lock:
            if self._stop_event.is_set() or minute in self._daily_reminder_minutes_sent:
                return False, False
            live_streamers = [entry for entry in live_streamers
                              if not self._runtime[entry["id"]]["stopped_by_user"]]
            if not live_streamers:
                return False, False
            # Commit the attempt immediately before dispatch, including explicit rejection.
            self._daily_reminder_minutes_sent.add(minute)
        logger.info(
            "[亲密度提醒] 23:%s 汇总 %s 位直播主播",
            minute,
            len(live_streamers),
        )
        success = notifier.send_daily_intimacy_reminder_for_streamers(
            streamers=live_streamers,
            minute=minute,
        )
        delivery_unknown = bool(notifier.delivery_unknown)
        if isinstance(notifier, MultiChannelNotifier):
            logger.info("汇总亲密度提醒: %s (23:%s)", notifier.last_report.summary, minute)
        elif success:
            logger.info("[OK] 汇总亲密度提醒已发送 (23:%s)", minute)
        else:
            logger.error("[FAIL] 汇总亲密度提醒发送失败 (23:%s)", minute)
        return success, delivery_unknown

    def _collect_daily_reminder_result(self) -> None:
        future = self._daily_reminder_future
        if future is None or not future.done():
            return
        try:
            future.result()
        except Exception:
            logger.exception("汇总亲密度提醒发送异常")
        self._daily_reminder_future = None

    def _check_daily_intimacy_reminder(self) -> None:
        """Send one reminder per minute containing every known live streamer."""
        self._collect_daily_reminder_result()
        if not self.enable_daily_intimacy_reminder or not self._workers:
            return
        if self._stop_event.is_set():
            return
        if self._daily_reminder_future is not None:
            return
        now = datetime.now()
        minute = now.minute
        if now.hour != 23 or minute not in (57, 58, 59):
            return
        today = now.strftime("%Y-%m-%d")
        if self._daily_reminder_date != today:
            self._daily_reminder_date = today
            self._daily_reminder_minutes_sent.clear()
        if minute in self._daily_reminder_minutes_sent:
            return

        with self._lock:
            live_streamers = [
                {
                    "id": streamer_id,
                    "nickname": runtime["nickname"] or runtime["label"] or streamer_id,
                    "room_id": runtime["room_id"],
                    "title": runtime["title"],
                    "platform": runtime["platform"],
                    "live_url": runtime["live_url"],
                }
                for streamer_id, runtime in self._runtime.items()
                if (streamer_id in self._workers and runtime["status"] == "live"
                    and runtime["platform"] == "douyin")
            ]
        if not live_streamers:
            return

        notifier = self._get_service_notifier()
        if self.running and self._notification_executor is not None:
            self._daily_reminder_future = self._notification_executor.submit(
                self._send_daily_intimacy_reminder,
                notifier,
                live_streamers,
                minute,
            )
            self._daily_reminder_future.add_done_callback(
                lambda _future: self._wake_event.set()
            )
            return

        self._send_daily_intimacy_reminder(notifier, live_streamers, minute)

    def check_all_once(self) -> Dict[str, Dict[str, Any]]:
        """Prepare and check every valid streamer once."""
        self.prepare_all()
        for streamer_id, runtime in self._runtime.items():
            if runtime["suspended"] and not runtime["stopped_by_user"] and "同一账号" not in runtime["last_error"]:
                self._start_fault(streamer_id, runtime["last_error"])
                self._queue_fault_alert(streamer_id, "suspended")
        if not self._workers:
            self._deliver_once_alerts()
            return {}
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.max_concurrent_checks, len(self._workers)),
            thread_name_prefix="monitor-once",
        ) as executor:
            jobs = {streamer_id: worker.check_once for streamer_id, worker in self._workers.items()}
            for streamer_id, future in self._bounded_futures(executor, jobs):
                try:
                    result = future.result()
                    if self._stop_event.is_set() or self._runtime[streamer_id]["stopped_by_user"]:
                        continue
                    delay = self._record_success(streamer_id, result)
                    if delay is None:
                        results[streamer_id] = result
                except Exception as exc:
                    self._record_error(streamer_id, exc)
        self._check_daily_intimacy_reminder()
        self._deliver_once_alerts()
        return results

    def _deliver_once_alerts(self) -> None:
        """Finish this single round's alerts without scheduling retries."""
        if not self._pending_alerts:
            return
        with ThreadPoolExecutor(max_workers=1) as executor:
            self._notification_executor = executor
            while (self._pending_alerts or self._alert_future is not None) and not self._stop_event.is_set():
                self._check_fault_notifications()
                self._wake_event.wait(0.05)
                self._wake_event.clear()
        self._notification_executor = None

    def run(self) -> bool:
        """Run all prepared workers until stopped or all are suspended."""
        self.running = True
        self._stopped_emitted = False
        self._stop_event.clear()
        self._wake_event.clear()
        if self.enable_console_stop:
            self._start_stop_listener()
        if not self.streamers:
            self.running = False
            return False

        failed_all = False
        try:
            worker_ids = [str(entry["id"]) for entry in self.streamers]
            offsets = self.initial_offsets(len(worker_ids), self.check_interval)
            started_at = time.monotonic()
            self._next_due = {
                streamer_id: started_at + offsets[index]
                for index, streamer_id in enumerate(worker_ids)
            }
            self._executor = ThreadPoolExecutor(
                max_workers=min(self.max_concurrent_checks, len(worker_ids)),
                thread_name_prefix="monitor-check",
            )
            self._notification_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="monitor-notify",
            )
            print(
                f"监控已启动，共 {len(worker_ids)} 个主播；"
                "输入 q 后回车可终止并退出，也可按 Ctrl+C。"
            )
            while self.running:
                self._collect_finished()
                if not self.running:
                    break
                self._check_fault_notifications()
                if not self._startup_sent and len(self._initial_attempts) == len(self.streamers):
                    self._startup_sent = True
                    if self.startup_notify and self._workers:
                        self._notification_executor.submit(self._send_startup_notification)
                self._submit_due_checks()
                if not self._has_runnable_workers():
                    if self._pending_alerts or self._alert_future is not None:
                        self._wake_event.wait(0.1)
                        self._wake_event.clear()
                        continue
                    with self._lock:
                        all_stopped_by_user = all(
                            self._runtime[streamer_id]["stopped_by_user"]
                            for streamer_id in self._runtime
                        )
                    if all_stopped_by_user:
                        logger.info("所有主播任务均已由用户停止，监控结束")
                    else:
                        logger.error("所有主播任务都已暂停，监控结束")
                        failed_all = True
                    break
                timeout = self._seconds_until_next_due()
                self._wake_event.wait(timeout)
                self._wake_event.clear()
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在退出")
        finally:
            self.stop()
            executor = self._executor
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            self._collect_finished()
            self._executor = None
            notification_executor = self._notification_executor
            if notification_executor is not None:
                notification_executor.shutdown(wait=True, cancel_futures=True)
            self._collect_daily_reminder_result()
            self._check_fault_notifications()
            self._pending_alerts.clear()
            self._notification_executor = None
            self.close()
            logger.info("多主播监听器已停止")
        return not failed_all

    def _submit_due_checks(self) -> None:
        if not self.running or self._executor is None:
            return
        in_flight = set(self._futures.values())
        busy_platforms = {self._platform(streamer_id) for streamer_id in in_flight}
        now = time.monotonic()
        for entry in sorted(self.streamers, key=lambda entry: self._next_due.get(str(entry["id"]), now)):
            if len(self._futures) >= self.max_concurrent_checks:
                break
            streamer_id = str(entry["id"])
            worker = self._workers.get(streamer_id)
            runtime = self._runtime[streamer_id]
            if runtime["suspended"] or streamer_id in in_flight:
                continue
            platform = self._platform(streamer_id)
            if platform in busy_platforms or now < self._platform_cooldown.get(platform, 0):
                continue
            if now < self._next_due.get(streamer_id, now):
                continue
            future = self._executor.submit(
                run_with_streamer_context,
                streamer_id,
                worker.check_once if worker is not None else self._prepare_worker,
                *(() if worker is not None else (entry,)),
            )
            if worker is None:
                self._preparation_futures.add(future)
            self._futures[future] = streamer_id
            busy_platforms.add(platform)
            self._next_due[streamer_id] = float("inf")
            future.add_done_callback(lambda _future: self._wake_event.set())

    def _collect_finished(self) -> None:
        collected = False
        for future, streamer_id in list(self._futures.items()):
            if not future.done():
                continue
            del self._futures[future]
            collected = True
            if future in self._preparation_futures:
                self._preparation_futures.remove(future)
                delay = self._collect_preparation(streamer_id, future)
                self._next_due[streamer_id] = time.monotonic() + delay
                continue
            if self._stop_event.is_set() or self._runtime[streamer_id]["stopped_by_user"]:
                continue
            delay = self._check_delay(streamer_id)
            try:
                result = future.result()
                delay = self._record_success(streamer_id, result) or delay
            except Exception as exc:
                delay = self._record_error(streamer_id, exc)
            self._next_due[streamer_id] = time.monotonic() + delay
        if collected:
            self._check_daily_intimacy_reminder()

    def _record_success(self, streamer_id: str, result: Mapping[str, Any]) -> Optional[float]:
        if result.get("indeterminate") or result.get("is_live") is None:
            exc = LiveStatusUnknownError(str(result.get("error") or "直播状态暂时无法确认"))
            exc.error_kind = str(result.get("error_kind") or "unknown")
            exc.cooldown = result.get("cooldown", 0)
            return self._record_error(streamer_id, exc)
        now_text = datetime.now().strftime("%m-%d %H:%M")
        nickname = (
            result.get("nickname")
            or self._runtime[streamer_id]["label"]
            or streamer_id
        )
        is_live = bool(result.get("is_live", False))
        method = str(result.get("method", "unknown"))
        with self._lock:
            runtime = self._runtime[streamer_id]
            if runtime["stopped_by_user"] or self._stop_event.is_set():
                return
            self._apply_platform_cooldown(streamer_id, result)
            runtime["nickname"] = nickname
            runtime["status"] = "live" if is_live else "offline"
            runtime["method"] = method
            runtime["room_id"] = str(result.get("room_id") or "")
            runtime["title"] = str(result.get("title") or "")
            runtime["live_url"] = str(result.get("live_url") or runtime["url"])
            runtime["session_id"] = str(result.get("session_id") or "")
            runtime["error_kind"] = ""
            runtime["last_check"] = now_text
            runtime["last_error"] = ""
            runtime["next_retry"] = ""
            runtime["check_count"] += 1
            runtime["consecutive_errors"] = 0
            runtime["consecutive_unknowns"] = 0
            count = runtime["check_count"]
        fault = self._faults.pop(streamer_id, None)
        if fault and (fault["alerted"] or fault["suspended"]):
            self._queue_alert(streamer_id, "recovery", fault)
        icon = "🔴" if is_live else "⚫"
        state_text = "直播中" if is_live else "未开播"
        line = (
            f"{icon} {now_text}  [{streamer_id}] #{count}  "
            f"[{platform_name(runtime['platform'])}] {nickname} {state_text} | {method}"
        )
        title = str(result.get("title") or "")[:30]
        if is_live and title:
            line += f" | {title}"
        with streamer_log_context(streamer_id):
            logger.info(line)
        if is_live or count % 10 == 1:
            print(line)
        self._emit("status", streamer_id, result=dict(result))

    @staticmethod
    def _error_reason(exc: Exception) -> str:
        kinds = {"restricted": "平台访问受限", "rate_limited": "平台请求频率受限",
                 "parse": "平台响应暂时无法解析", "parse_error": "平台响应暂时无法解析",
                 "invalid_target": "主播链接无效或未通过安全校验"}
        kind = str(getattr(exc, "error_kind", ""))
        if kind in kinds:
            return kinds[kind]
        message = failure_reason(exc)
        if isinstance(exc, LiveStatusUnknownError):
            message = "直播状态暂时无法确认"
            for reason in ("DNS 域名解析失败", "网络请求超时", "网络连接失败", "服务请求失败"):
                if reason in str(exc):
                    message = reason
                    break
        elif kind == "network" and message == "监控任务异常":
            message = "网络请求失败"
        return message

    def _apply_platform_cooldown(self, streamer_id: str, source: Any) -> None:
        cooldown_value = source.get("cooldown", 0) if isinstance(source, Mapping) else getattr(source, "cooldown", 0)
        error_kind = source.get("error_kind", "") if isinstance(source, Mapping) else getattr(source, "error_kind", "")
        try:
            cooldown = min(86400.0, max(0.0, float(cooldown_value)))
        except (TypeError, ValueError):
            cooldown = 0.0
        if error_kind == "rate_limited" and not cooldown:
            cooldown = 60.0
        if cooldown:
            platform = self._platform(streamer_id)
            self._platform_cooldown[platform] = max(
                self._platform_cooldown.get(platform, 0), time.monotonic() + cooldown,
            )

    def _record_error(self, streamer_id: str, exc: Exception) -> float:
        message = self._error_reason(exc)
        interval = self._check_delay(streamer_id)
        if self._runtime[streamer_id]["stopped_by_user"] or self._stop_event.is_set():
            return interval
        if isinstance(exc, MonitorCheckCancelled):
            with streamer_log_context(streamer_id):
                logger.info("主播 %s 的在途检测已取消", streamer_id)
            return interval
        self._apply_platform_cooldown(streamer_id, exc)
        self._start_fault(streamer_id, message)
        error_kind = str(getattr(exc, "error_kind", ""))
        permanent = error_kind == "invalid_target" or isinstance(exc, ProfileLinkError)
        if not permanent and (isinstance(exc, LiveStatusUnknownError) or transient_network_error(exc)
                              or error_kind in {"network", "restricted", "rate_limited", "parse", "parse_error"}):
            with self._lock:
                runtime = self._runtime[streamer_id]
                if runtime["stopped_by_user"]:
                    return interval
                runtime["consecutive_unknowns"] += 1
                unknown_count = runtime["consecutive_unknowns"]
                runtime["last_error"] = message
                runtime["error_kind"] = error_kind
                runtime["status"] = "error"
                count = runtime["consecutive_errors"]
            delay = min(
                max(
                    interval,
                    (60.0, 120.0, 240.0, 300.0)[
                        min(unknown_count - 1, 3)
                    ],
                ),
                max(300.0, interval),
            )
            delay = max(delay, self._platform_cooldown.get(self._platform(streamer_id), 0) - time.monotonic())
            with streamer_log_context(streamer_id):
                logger.warning("主播 %s 状态暂时无法确认: %s", streamer_id, message)
            with self._lock:
                if not runtime["stopped_by_user"]:
                    runtime["next_retry"] = datetime.fromtimestamp(time.time() + delay).strftime("%H:%M:%S")
            self._emit(
                "error",
                streamer_id,
                error=message,
                count=count,
                transient=True,
                retry_in=delay,
            )
            return delay
        with self._lock:
            runtime = self._runtime[streamer_id]
            if runtime["stopped_by_user"]:
                return interval
            runtime["consecutive_errors"] += 1
            runtime["consecutive_unknowns"] = 0
            count = runtime["consecutive_errors"]
            runtime["last_error"] = message
            runtime["error_kind"] = error_kind
            runtime["status"] = "error"
            if permanent or count >= 10:
                runtime["suspended"] = True
                runtime["next_retry"] = ""
        with streamer_log_context(streamer_id):
            logger.error(
                "主播 %s 检测异常 (连续错误 %s/10): %s",
                streamer_id,
                count,
                message,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        if permanent or count >= 10:
            self._queue_fault_alert(streamer_id, "suspended")
            with streamer_log_context(streamer_id):
                logger.error("主播 %s 已暂停，其他主播继续监控", streamer_id)
            self._emit("suspended", streamer_id, error=message)
        else:
            self._emit("error", streamer_id, error=message, count=count)
        return interval

    def _has_runnable_workers(self) -> bool:
        return any(
            not self._runtime[streamer_id]["suspended"]
            for streamer_id in self._runtime
        )

    def _seconds_until_next_due(self) -> float:
        busy = {self._platform(streamer_id) for streamer_id in self._futures.values()}
        due_times = []
        if len(self._futures) < self.max_concurrent_checks:
            due_times = [
                max(due, self._platform_cooldown.get(self._platform(streamer_id), 0))
                for streamer_id, due in self._next_due.items()
                if not self._runtime[streamer_id]["suspended"]
                and self._platform(streamer_id) not in busy
            ]
        due_times.extend(
            fault["started"] + 300 for streamer_id, fault in self._faults.items()
            if not fault["alerted"] and not self._runtime[streamer_id]["suspended"]
        )
        if not due_times:
            return min(1.0, float(self.check_interval))
        timeout = min(due_times) - time.monotonic()
        return max(0.05, min(timeout, float(self.check_interval)))

    def stop(self) -> None:
        """Wake the scheduler and request every worker to stop."""
        self.running = False
        if self._push_dispatcher is not None:
            self._push_dispatcher.stop()
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            workers = list(self._workers.items()) + list(self._preparing.items())
            emit_stopped = not self._stopped_emitted
            self._stopped_emitted = True
        for streamer_id, worker in workers:
            run_with_streamer_context(streamer_id, worker.stop)
        if emit_stopped:
            self._emit("stopped")

    def stop_streamer(self, streamer_id: str) -> bool:
        """Stop one prepared streamer while leaving the other tasks running."""
        streamer_id = str(streamer_id or "")
        with self._lock:
            worker = self._workers.get(streamer_id) or self._preparing.get(streamer_id)
            runtime = self._runtime.get(streamer_id)
            if runtime is None or runtime["stopped_by_user"]:
                return False
            runtime["stopped_by_user"] = True
            runtime["suspended"] = True
            runtime["status"] = "stopped"
            runtime["last_error"] = ""
            runtime["next_retry"] = ""
        stop_and_wait = getattr(worker, "stop_and_wait", None)
        if callable(stop_and_wait):
            run_with_streamer_context(streamer_id, stop_and_wait)
        elif worker is not None:
            run_with_streamer_context(streamer_id, worker.stop)
        with streamer_log_context(streamer_id):
            logger.info("主播 %s 已由用户停止，其他主播继续监控", streamer_id)
        self._emit("streamer_stopped", streamer_id)
        self._wake_event.set()
        return True

    def close(self) -> None:
        """Stop workers and release their network resources."""
        self.stop()
        with self._lock:
            workers = list(self._workers.items())
        for streamer_id, worker in workers:
            stop_and_wait = getattr(worker, "stop_and_wait", None)
            if callable(stop_and_wait):
                run_with_streamer_context(streamer_id, stop_and_wait)
                continue
            close = getattr(worker, "close", None)
            if callable(close):
                run_with_streamer_context(streamer_id, close)
        if self._push_dispatcher is not None:
            self._push_dispatcher.close()

    def _start_stop_listener(self) -> None:
        stdin = getattr(sys, "stdin", None)
        if stdin is None or not hasattr(stdin, "isatty") or not stdin.isatty():
            return

        def listen() -> None:
            while self.running:
                try:
                    command = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return
                if command in {"q", "quit", "exit", "stop", "退出"}:
                    print("正在停止全部监控任务...")
                    self.stop()
                    return
                if command:
                    print("未知命令；输入 q 后回车退出。")

        threading.Thread(
            target=listen,
            name="monitor-service-stop-listener",
            daemon=True,
        ).start()
