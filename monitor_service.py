"""Bounded multi-streamer scheduling shared by all user interfaces."""

from __future__ import annotations

import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from monitor_types import (
    MonitorEvent,
    StreamerSnapshot,
    StreamerStatus,
    WorkerPreparation,
)
from streamer_logging import run_with_streamer_context, streamer_log_context


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
        self._daily_reminder_pending: Optional[tuple[str, int]] = None
        self._notification_executor: Optional[ThreadPoolExecutor] = None

        for entry in self.streamers:
            streamer_id = str(entry["id"])
            self._runtime[streamer_id] = {
                "id": streamer_id,
                "url": entry["url"],
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

        prepared_sec_uids: Dict[str, str] = {}
        if candidates:
            with ThreadPoolExecutor(
                max_workers=min(self.max_concurrent_checks, len(candidates)),
                thread_name_prefix="monitor-prepare",
            ) as executor:
                futures = {
                    executor.submit(
                        run_with_streamer_context,
                        streamer_id,
                        worker.prepare,
                    ): streamer_id
                    for streamer_id, worker in candidates.items()
                }
                for future in as_completed(futures):
                    streamer_id = futures[future]
                    try:
                        prepared = future.result()
                        if isinstance(prepared, WorkerPreparation):
                            sec_uid = prepared.sec_uid
                            preparation = prepared
                        else:
                            sec_uid = str(prepared or "")
                            preparation = WorkerPreparation(sec_uid=sec_uid)
                        if not sec_uid:
                            raise RuntimeError("无法解析主播")
                        prepared_sec_uids[streamer_id] = str(sec_uid)
                        with self._lock:
                            runtime = self._runtime[streamer_id]
                            runtime["nickname"] = (
                                preparation.nickname or runtime["label"]
                            )
                            runtime["status"] = preparation.status.value
                    except Exception as exc:
                        self._mark_prepare_error(streamer_id, exc)
                        self._close_candidate(
                            streamer_id, candidates[streamer_id]
                        )

        seen_sec_uids: Dict[str, str] = {}
        for entry in self.streamers:
            streamer_id = str(entry["id"])
            sec_uid = prepared_sec_uids.get(streamer_id)
            if not sec_uid:
                continue
            duplicate_of = seen_sec_uids.get(sec_uid)
            if duplicate_of:
                message = f"与主播 {duplicate_of} 指向同一账号，已跳过"
                self._mark_prepare_error(streamer_id, RuntimeError(message))
                self._close_candidate(streamer_id, candidates[streamer_id])
                continue
            seen_sec_uids[sec_uid] = streamer_id
            worker = candidates[streamer_id]
            with self._lock:
                self._workers[streamer_id] = worker
                runtime = self._runtime[streamer_id]
                runtime["last_error"] = ""
                nickname = runtime["nickname"]
            self._emit("prepared", streamer_id, nickname=nickname)

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
        message = str(exc)
        with streamer_log_context(streamer_id):
            logger.error("主播 %s 初始化失败: %s", streamer_id, message)
        with self._lock:
            runtime = self._runtime[streamer_id]
            runtime["status"] = "error"
            runtime["last_error"] = message
            runtime["suspended"] = True
        self._emit("prepare_error", streamer_id, error=message)

    def _send_startup_notification(self) -> None:
        if not self.startup_notify or not self._workers:
            return
        names = []
        for streamer_id in self._workers:
            runtime = self._runtime[streamer_id]
            names.append(runtime["nickname"] or runtime["label"] or streamer_id)
        self._get_service_notifier().send(
            title="[START] 抖音直播监听器已启动",
            desp=(
                f"**监控主播数**: {len(names)}\n"
                f"**主播**: {', '.join(names)}\n"
                f"**检测间隔**: {self.check_interval}s\n"
                f"**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
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
        if success:
            logger.info("[OK] 汇总亲密度提醒已发送 (23:%s)", minute)
        else:
            logger.error("[FAIL] 汇总亲密度提醒发送失败 (23:%s)", minute)
        return success, delivery_unknown

    def _collect_daily_reminder_result(self) -> None:
        future = self._daily_reminder_future
        if future is None or not future.done():
            return
        pending = self._daily_reminder_pending
        try:
            success, delivery_unknown = future.result()
        except Exception:
            logger.exception("汇总亲密度提醒发送异常")
            success, delivery_unknown = False, True
        if (
            pending is not None
            and pending[0] == self._daily_reminder_date
            and (success or delivery_unknown)
        ):
            self._daily_reminder_minutes_sent.add(pending[1])
        self._daily_reminder_future = None
        self._daily_reminder_pending = None

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
                    "nickname": runtime["nickname"] or runtime["label"] or streamer_id,
                    "room_id": runtime["room_id"],
                    "title": runtime["title"],
                }
                for streamer_id, runtime in self._runtime.items()
                if streamer_id in self._workers and runtime["status"] == "live"
            ]
        if not live_streamers:
            return

        notifier = self._get_service_notifier()
        if self.running and self._notification_executor is not None:
            self._daily_reminder_pending = (today, minute)
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

        success, delivery_unknown = self._send_daily_intimacy_reminder(
            notifier, live_streamers, minute
        )
        if success or delivery_unknown:
            self._daily_reminder_minutes_sent.add(minute)

    def check_all_once(self) -> Dict[str, Dict[str, Any]]:
        """Prepare and check every valid streamer once."""
        self.prepare_all()
        if not self._workers:
            return {}
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.max_concurrent_checks, len(self._workers)),
            thread_name_prefix="monitor-once",
        ) as executor:
            futures = {
                executor.submit(
                    run_with_streamer_context,
                    streamer_id,
                    worker.check_once,
                ): streamer_id
                for streamer_id, worker in self._workers.items()
            }
            for future in as_completed(futures):
                streamer_id = futures[future]
                try:
                    result = future.result()
                    results[streamer_id] = result
                    self._record_success(streamer_id, result)
                except Exception as exc:
                    self._record_error(streamer_id, exc)
        self._check_daily_intimacy_reminder()
        return results

    def run(self) -> bool:
        """Run all prepared workers until stopped or all are suspended."""
        self.running = True
        self._stopped_emitted = False
        self._stop_event.clear()
        self._wake_event.clear()
        if self.enable_console_stop:
            self._start_stop_listener()
        self.prepare_all()
        if not self._workers:
            logger.error("没有可运行的主播监控任务")
            self.running = False
            return False
        if not self.running:
            self.stop()
            return True

        failed_all = False
        try:
            self._send_startup_notification()
            worker_ids = list(self._workers)
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
                self._submit_due_checks()
                if not self._has_runnable_workers():
                    with self._lock:
                        all_stopped_by_user = all(
                            self._runtime[streamer_id]["stopped_by_user"]
                            for streamer_id in self._workers
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
                notification_executor.shutdown(wait=True, cancel_futures=False)
            self._collect_daily_reminder_result()
            self._notification_executor = None
            self.close()
            logger.info("多主播监听器已停止")
        return not failed_all

    def _submit_due_checks(self) -> None:
        if not self.running or self._executor is None:
            return
        in_flight = set(self._futures.values())
        now = time.monotonic()
        for streamer_id, worker in self._workers.items():
            runtime = self._runtime[streamer_id]
            if runtime["suspended"] or streamer_id in in_flight:
                continue
            if now < self._next_due.get(streamer_id, now):
                continue
            future = self._executor.submit(
                run_with_streamer_context,
                streamer_id,
                worker.check_once,
            )
            self._futures[future] = streamer_id
            self._next_due[streamer_id] = float("inf")
            future.add_done_callback(lambda _future: self._wake_event.set())

    def _collect_finished(self) -> None:
        collected = False
        for future, streamer_id in list(self._futures.items()):
            if not future.done():
                continue
            del self._futures[future]
            collected = True
            delay = float(self.check_interval)
            try:
                result = future.result()
                self._record_success(streamer_id, result)
            except Exception as exc:
                delay = self._record_error(streamer_id, exc)
            self._next_due[streamer_id] = time.monotonic() + delay
        if collected:
            self._check_daily_intimacy_reminder()

    def _record_success(self, streamer_id: str, result: Mapping[str, Any]) -> None:
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
            if runtime["stopped_by_user"]:
                return
            runtime["nickname"] = nickname
            runtime["status"] = "live" if is_live else "offline"
            runtime["method"] = method
            runtime["room_id"] = str(result.get("room_id") or "")
            runtime["title"] = str(result.get("title") or "")
            runtime["last_check"] = now_text
            runtime["last_error"] = ""
            runtime["check_count"] += 1
            runtime["consecutive_errors"] = 0
            runtime["consecutive_unknowns"] = 0
            count = runtime["check_count"]
        icon = "🔴" if is_live else "⚫"
        state_text = "直播中" if is_live else "未开播"
        line = (
            f"{icon} {now_text}  [{streamer_id}] #{count}  "
            f"{nickname} {state_text} | {method}"
        )
        title = str(result.get("title") or "")[:30]
        if is_live and title:
            line += f" | {title}"
        with streamer_log_context(streamer_id):
            logger.info(line)
        if is_live or count % 10 == 1:
            print(line)
        self._emit("status", streamer_id, result=dict(result))

    def _record_error(self, streamer_id: str, exc: Exception) -> float:
        message = str(exc)
        if isinstance(exc, MonitorCheckCancelled):
            with streamer_log_context(streamer_id):
                logger.info("主播 %s 的在途检测已取消", streamer_id)
            return float(self.check_interval)
        if isinstance(exc, LiveStatusUnknownError):
            with self._lock:
                runtime = self._runtime[streamer_id]
                if runtime["stopped_by_user"]:
                    return float(self.check_interval)
                runtime["consecutive_unknowns"] += 1
                unknown_count = runtime["consecutive_unknowns"]
                runtime["last_error"] = message
                runtime["status"] = "error"
                count = runtime["consecutive_errors"]
            delay = min(
                max(
                    float(self.check_interval),
                    (60.0, 120.0, 240.0, 300.0)[
                        min(unknown_count - 1, 3)
                    ],
                ),
                float(max(300, self.check_interval)),
            )
            with streamer_log_context(streamer_id):
                logger.warning("主播 %s 状态暂时无法确认: %s", streamer_id, message)
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
                return float(self.check_interval)
            runtime["consecutive_errors"] += 1
            runtime["consecutive_unknowns"] = 0
            count = runtime["consecutive_errors"]
            runtime["last_error"] = message
            runtime["status"] = "error"
            if count >= 10:
                runtime["suspended"] = True
        with streamer_log_context(streamer_id):
            logger.error(
                "主播 %s 检测异常 (连续错误 %s/10): %s",
                streamer_id,
                count,
                message,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        if count >= 10:
            with streamer_log_context(streamer_id):
                logger.error("主播 %s 已暂停，其他主播继续监控", streamer_id)
            self._emit("suspended", streamer_id, error=message)
        else:
            self._emit("error", streamer_id, error=message, count=count)
        return float(self.check_interval)

    def _has_runnable_workers(self) -> bool:
        return any(
            not self._runtime[streamer_id]["suspended"]
            for streamer_id in self._workers
        )

    def _seconds_until_next_due(self) -> float:
        due_times = [
            due
            for streamer_id, due in self._next_due.items()
            if not self._runtime[streamer_id]["suspended"]
        ]
        if not due_times:
            return 0.1
        timeout = min(due_times) - time.monotonic()
        return max(0.05, min(timeout, float(self.check_interval)))

    def stop(self) -> None:
        """Wake the scheduler and request every worker to stop."""
        self.running = False
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            workers = list(self._workers.items())
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
            worker = self._workers.get(streamer_id)
            runtime = self._runtime.get(streamer_id)
            if worker is None or runtime is None or runtime["stopped_by_user"]:
                return False
            runtime["stopped_by_user"] = True
            runtime["suspended"] = True
            runtime["status"] = "stopped"
            runtime["last_error"] = ""
        stop_and_wait = getattr(worker, "stop_and_wait", None)
        if callable(stop_and_wait):
            run_with_streamer_context(streamer_id, stop_and_wait)
        else:
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
