from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
import logging
import threading
import time
from unittest.mock import Mock

import pytest

from douyin_monitor.monitor_service import LiveStatusUnknownError, MonitorService
from douyin_monitor.monitor_types import WorkerPreparation
from douyin_monitor.notifier import ServerChanNotifier
from douyin_monitor.streamer_logging import StreamerLogHandler


@dataclass
class FakeState:
    streamer_nickname: str
    status: str = "unknown"


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.daily_messages = []
        self.delivery_unknown = False

    def send(self, title, desp=""):
        self.messages.append((title, desp))
        return True

    def send_daily_intimacy_reminder_for_streamers(self, streamers, minute):
        self.daily_messages.append((list(streamers), minute))
        return True


class FakeWorker:
    def __init__(self, entry, sec_uid=None, result=None, error=None):
        self.entry = dict(entry)
        self.sec_uid = sec_uid or f"sec-{entry['id']}"
        self.result = result or {
            "nickname": entry.get("label") or entry["id"],
            "is_live": False,
            "method": "fake",
        }
        self.error = error
        self.state = FakeState(entry.get("label") or entry["id"])
        self.notifier = FakeNotifier()
        self.stopped = False
        self.closed = False

    def prepare(self):
        return self.sec_uid

    def check_once(self):
        if self.error:
            raise self.error
        return dict(self.result)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def test_console_stop_listener_is_opt_in(monkeypatch):
    calls = []
    service = MonitorService([], worker_factory=lambda entry: FakeWorker(entry))
    monkeypatch.setattr(service, "_start_stop_listener", lambda: calls.append(True))

    assert service.run() is False
    assert calls == []


def test_service_close_releases_prepared_workers():
    created = []
    service = MonitorService(
        [streamer("one")],
        worker_factory=lambda entry: created.append(FakeWorker(entry)) or created[-1],
    )
    service.prepare_all()

    service.close()

    assert created[0].stopped is True
    assert created[0].closed is True


def test_unknown_backoff_sequence_and_success_reset():
    service = MonitorService(
        [streamer("one")],
        worker_factory=lambda entry: FakeWorker(entry),
        check_interval=30,
    )
    service.prepare_all()

    delays = [
        service._record_error("one", LiveStatusUnknownError("unknown"))
        for _ in range(5)
    ]
    service._record_success(
        "one", {"nickname": "one", "is_live": False, "method": "test"}
    )

    assert delays == [60.0, 120.0, 240.0, 300.0, 300.0]
    assert service.snapshot()[0]["consecutive_unknowns"] == 0


def test_unknown_backoff_never_polls_faster_than_long_normal_interval():
    service = MonitorService(
        [streamer("one")],
        worker_factory=lambda entry: FakeWorker(entry),
        check_interval=600,
    )
    service.prepare_all()

    delays = [
        service._record_error("one", LiveStatusUnknownError("unknown"))
        for _ in range(5)
    ]

    assert delays == [600.0] * 5


def streamer(streamer_id, label=""):
    return {
        "id": streamer_id,
        "url": f"https://www.douyin.com/user/{streamer_id}",
        "label": label,
        "enabled": True,
    }


def test_duplicate_resolved_user_is_suspended():
    entries = [streamer("one"), streamer("two")]
    created = {}

    service = MonitorService(
        entries,
        worker_factory=lambda entry: created.setdefault(
            entry["id"], FakeWorker(entry, sec_uid="same-user")
        ),
    )

    assert service.prepare_all() == ["one"]
    snapshot = {item["id"]: item for item in service.snapshot()}
    assert snapshot["one"]["suspended"] is False
    assert snapshot["two"]["suspended"] is True
    assert "指向同一账号" in snapshot["two"]["last_error"]
    assert created["two"].closed is True


def test_one_worker_failure_does_not_block_other_worker():
    entries = [streamer("good", "正常主播"), streamer("bad", "异常主播")]

    def factory(entry):
        if entry["id"] == "bad":
            return FakeWorker(entry, error=RuntimeError("broken"))
        return FakeWorker(
            entry,
            result={"nickname": "正常主播", "is_live": True, "method": "fake"},
        )

    service = MonitorService(entries, worker_factory=factory)
    results = service.check_all_once()

    assert list(results) == ["good"]
    snapshot = {item["id"]: item for item in service.snapshot()}
    assert snapshot["good"]["status"] == "live"
    assert snapshot["bad"]["status"] == "error"
    assert snapshot["bad"]["consecutive_errors"] == 1
    assert snapshot["bad"]["suspended"] is False


def test_four_streamers_recover_one_failed_worker_without_restart():
    entries = [streamer(f"streamer-{index}") for index in range(4)]

    class RecoveringWorker(FakeWorker):
        def __init__(self, entry):
            super().__init__(entry)
            self.calls = 0

        def check_once(self):
            self.calls += 1
            if self.entry["id"] == "streamer-2" and self.calls == 1:
                raise RuntimeError("profile_api_empty_response")
            return {
                "nickname": self.entry["id"],
                "is_live": self.entry["id"] in {"streamer-0", "streamer-2"},
                "method": "api",
            }

    events = []
    service = MonitorService(
        entries,
        worker_factory=RecoveringWorker,
        max_concurrent_checks=2,
        on_event=events.append,
    )

    first = service.check_all_once()
    first_snapshot = {item["id"]: item for item in service.snapshot()}
    second = service.check_all_once()
    second_snapshot = {item["id"]: item for item in service.snapshot()}

    assert set(first) == {"streamer-0", "streamer-1", "streamer-3"}
    assert first_snapshot["streamer-0"]["status"] == "live"
    assert first_snapshot["streamer-2"]["status"] == "error"
    assert first_snapshot["streamer-3"]["status"] == "offline"
    assert set(second) == {entry["id"] for entry in entries}
    assert second_snapshot["streamer-2"]["status"] == "live"
    assert second_snapshot["streamer-2"]["consecutive_errors"] == 0
    assert any(
        event["type"] == "status"
        and event["streamer_id"] == "streamer-2"
        for event in events
    )


def test_worker_logs_receive_the_matching_streamer_context():
    events = []
    handler = StreamerLogHandler(events.append)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    class LoggingWorker(FakeWorker):
        def check_once(self):
            logging.getLogger("worker-test").warning(
                "检测 %s", self.entry["id"]
            )
            return super().check_once()

    try:
        service = MonitorService(
            [streamer("one"), streamer("two")],
            worker_factory=LoggingWorker,
            max_concurrent_checks=2,
        )
        service.check_all_once()
    finally:
        root_logger.removeHandler(handler)

    messages_by_id = {
        event["streamer_id"]: event["message"] for event in events
    }
    assert "检测 one" in messages_by_id["one"]
    assert "检测 two" in messages_by_id["two"]


def test_startup_notification_is_aggregated_into_one_post():
    entries = [streamer("one", "主播甲"), streamer("two", "主播乙")]
    workers = {}
    service_notifier = FakeNotifier()

    def factory(entry):
        worker = FakeWorker(entry)
        workers[entry["id"]] = worker
        return worker

    service = MonitorService(
        entries,
        worker_factory=factory,
        startup_notify=True,
        service_notifier=service_notifier,
    )
    service.prepare_all()
    service._send_startup_notification()

    assert len(service_notifier.messages) == 1
    assert all(not worker.notifier.messages for worker in workers.values())
    title, body = service_notifier.messages[0]
    assert "已启动" in title
    assert "主播甲" in body
    assert "主播乙" in body


def test_daily_intimacy_reminder_is_aggregated_once_per_minute(monkeypatch):
    entries = [
        streamer("one", "主播甲"),
        streamer("two", "主播乙"),
        streamer("offline", "未开播主播"),
    ]
    workers = {}
    service_notifier = FakeNotifier()

    def factory(entry):
        worker = FakeWorker(entry)
        workers[entry["id"]] = worker
        return worker

    service = MonitorService(
        entries,
        worker_factory=factory,
        enable_daily_intimacy_reminder=True,
        service_notifier=service_notifier,
    )
    service.prepare_all()
    service._record_success(
        "one",
        {
            "nickname": "主播甲",
            "is_live": True,
            "method": "api",
            "room_id": "101",
            "title": "甲的直播间",
        },
    )
    service._record_success(
        "two",
        {
            "nickname": "主播乙",
            "is_live": True,
            "method": "api",
            "room_id": "202",
            "title": "乙的直播间",
        },
    )
    service._record_success(
        "offline",
        {"nickname": "未开播主播", "is_live": False, "method": "api"},
    )

    class FixedDateTime:
        @classmethod
        def now(cls):
            return __import__("datetime").datetime(2026, 8, 26, 23, 57, 10)

    monkeypatch.setattr("douyin_monitor.monitor_service.datetime", FixedDateTime)

    service._check_daily_intimacy_reminder()
    service._check_daily_intimacy_reminder()

    all_daily_messages = service_notifier.daily_messages
    assert len(all_daily_messages) == 1
    live_streamers, minute = all_daily_messages[0]
    assert minute == 57
    assert [item["nickname"] for item in live_streamers] == ["主播甲", "主播乙"]
    assert all(item["nickname"] != "未开播主播" for item in live_streamers)


def test_daily_intimacy_reminder_respects_disabled_setting(monkeypatch):
    worker = FakeWorker(streamer("one", "主播甲"))
    service = MonitorService(
        [streamer("one", "主播甲")],
        worker_factory=lambda _entry: worker,
        enable_daily_intimacy_reminder=False,
    )
    service.prepare_all()
    service._record_success(
        "one", {"nickname": "主播甲", "is_live": True, "method": "api"}
    )

    class FixedDateTime:
        @classmethod
        def now(cls):
            return __import__("datetime").datetime(2026, 8, 26, 23, 57, 10)

    monkeypatch.setattr("douyin_monitor.monitor_service.datetime", FixedDateTime)
    service._check_daily_intimacy_reminder()

    assert worker.notifier.daily_messages == []


def test_daily_intimacy_reminder_does_not_block_scheduler(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingNotifier(FakeNotifier):
        def send_daily_intimacy_reminder_for_streamers(self, streamers, minute):
            started.set()
            release.wait(timeout=2)
            return super().send_daily_intimacy_reminder_for_streamers(
                streamers, minute
            )

    notifier = BlockingNotifier()
    worker = FakeWorker(streamer("one", "主播甲"))
    service = MonitorService(
        [streamer("one", "主播甲")],
        worker_factory=lambda _entry: worker,
        enable_daily_intimacy_reminder=True,
        service_notifier=notifier,
    )
    service.prepare_all()
    service._record_success(
        "one", {"nickname": "主播甲", "is_live": True, "method": "api"}
    )

    class FixedDateTime:
        @classmethod
        def now(cls):
            return __import__("datetime").datetime(2026, 8, 26, 23, 57, 10)

    monkeypatch.setattr("douyin_monitor.monitor_service.datetime", FixedDateTime)
    service.running = True
    service._notification_executor = ThreadPoolExecutor(max_workers=1)
    try:
        began = time.monotonic()
        service._check_daily_intimacy_reminder()
        elapsed = time.monotonic() - began

        assert elapsed < 0.2
        assert started.wait(timeout=1)
        service._check_daily_intimacy_reminder()
        assert worker.notifier.daily_messages == []
    finally:
        release.set()
        service._notification_executor.shutdown(wait=True)
        service._collect_daily_reminder_result()

    assert len(notifier.daily_messages) == 1


def test_monitor_service_keeps_legacy_on_event_positional_argument():
    events = []
    service = MonitorService(
        [streamer("one")],
        FakeWorker,
        30,
        2,
        False,
        events.append,
    )

    service._emit("prepared", "one")

    assert events[0]["type"] == "prepared"
    assert events[0]["streamer_id"] == "one"
    assert service.enable_daily_intimacy_reminder is True


def test_ten_errors_suspend_only_the_failing_worker():
    entries = [streamer("good"), streamer("bad")]
    service = MonitorService(entries, worker_factory=FakeWorker)
    service.prepare_all()

    for _ in range(10):
        service._record_error("bad", RuntimeError("broken"))

    snapshot = {item["id"]: item for item in service.snapshot()}
    assert snapshot["bad"]["suspended"] is True
    assert snapshot["good"]["suspended"] is False
    assert service._has_runnable_workers() is True


def test_unknown_live_status_never_suspends_and_can_recover():
    events = []
    service = MonitorService(
        [streamer("recovering")],
        worker_factory=FakeWorker,
        on_event=events.append,
    )
    service.prepare_all()

    for _ in range(12):
        service._record_error(
            "recovering",
            LiveStatusUnknownError("profile_api_empty_response"),
        )

    snapshot = service.snapshot()[0]
    assert snapshot["suspended"] is False
    assert snapshot["consecutive_errors"] == 0
    assert snapshot["status"] == "error"
    assert all(event.get("transient") for event in events if event["type"] == "error")

    service._record_success(
        "recovering",
        {"nickname": "恢复主播", "is_live": True, "method": "api"},
    )

    recovered = service.snapshot()[0]
    assert recovered["status"] == "live"
    assert recovered["last_error"] == ""


def test_stop_reaches_every_worker():
    entries = [streamer("one"), streamer("two")]
    workers = {}

    def factory(entry):
        worker = FakeWorker(entry)
        workers[entry["id"]] = worker
        return worker

    service = MonitorService(entries, worker_factory=factory)
    service.prepare_all()
    service.running = True

    service.stop()

    assert service.running is False
    assert all(worker.stopped for worker in workers.values())


def test_stop_streamer_stops_only_selected_worker_and_preserves_status():
    entries = [streamer("one", "主播甲"), streamer("two", "主播乙")]
    workers = {}
    events = []

    def factory(entry):
        worker = FakeWorker(entry)
        workers[entry["id"]] = worker
        return worker

    service = MonitorService(
        entries,
        worker_factory=factory,
        on_event=events.append,
    )
    service.prepare_all()
    service.running = True

    assert service.stop_streamer("one") is True
    assert workers["one"].stopped is True
    assert workers["two"].stopped is False
    assert service._has_runnable_workers() is True

    service._record_success(
        "one", {"nickname": "主播甲", "is_live": True, "method": "late-result"}
    )
    snapshot = {item["id"]: item for item in service.snapshot()}
    assert snapshot["one"]["status"] == "stopped"
    assert snapshot["one"]["stopped_by_user"] is True
    assert snapshot["one"]["suspended"] is True
    assert snapshot["two"]["suspended"] is False
    assert any(
        event["type"] == "streamer_stopped" and event["streamer_id"] == "one"
        for event in events
    )


def test_stop_streamer_rejects_unknown_or_already_stopped_worker():
    service = MonitorService([streamer("one")], worker_factory=FakeWorker)
    service.prepare_all()
    service.running = True

    assert service.stop_streamer("missing") is False
    assert service.stop_streamer("one") is True
    assert service.stop_streamer("one") is False


def test_stop_event_is_emitted_only_once():
    events = []
    service = MonitorService(
        [streamer("one")],
        worker_factory=FakeWorker,
        on_event=events.append,
    )
    service.prepare_all()
    service.running = True

    service.stop()
    service.stop()

    assert [event["type"] for event in events].count("stopped") == 1


def test_initial_checks_are_staggered_and_bounded():
    assert MonitorService.initial_offsets(0, 30) == []
    assert MonitorService.initial_offsets(3, 30) == [0.0, 2.0, 4.0]
    assert MonitorService.initial_offsets(4, 1) == [0.0, 0.25, 0.5, 0.75]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("check_interval", 0),
        ("check_interval", 86401),
        ("max_concurrent_checks", 0),
        ("max_concurrent_checks", 9),
    ],
)
def test_scheduler_rejects_out_of_range_limits(field, value):
    kwargs = {field: value}
    with pytest.raises(ValueError):
        MonitorService([streamer("one")], worker_factory=FakeWorker, **kwargs)


def test_scheduler_checks_all_workers_and_stops_from_event():
    entries = [streamer("one"), streamer("two")]
    status_ids = set()
    holder = {}

    def on_event(event):
        if event["type"] != "status":
            return
        status_ids.add(event["streamer_id"])
        if status_ids == {"one", "two"}:
            holder["service"].stop()

    service = MonitorService(
        entries,
        worker_factory=FakeWorker,
        check_interval=1,
        max_concurrent_checks=2,
        on_event=on_event,
    )
    holder["service"] = service
    run_result = []
    thread = threading.Thread(target=lambda: run_result.append(service.run()))

    thread.start()
    thread.join(timeout=3)

    assert thread.is_alive() is False
    assert status_ids == {"one", "two"}
    assert service.running is False
    assert run_result == [True]


def test_run_reports_failure_when_no_worker_can_prepare():
    created = []

    def factory(entry):
        worker = FakeWorker(entry)
        worker.prepare = lambda: None
        created.append(worker)
        return worker

    service = MonitorService(
        [streamer("bad")],
        worker_factory=factory,
    )

    assert service.run() is False
    assert created[0].closed is True


def platform_streamer(streamer_id, platform):
    return dict(streamer(streamer_id), platform=platform,
                url=f"https://live.example/{streamer_id}")


class PlatformWorker(FakeWorker):
    def prepare(self):
        return WorkerPreparation(platform=self.entry["platform"], target_id=self.sec_uid,
                                 url=self.entry["url"])


def test_platform_identity_keeps_cross_platform_ids_and_drops_aliases():
    entries = [platform_streamer("one", "bilibili"), platform_streamer("two", "huya"),
               platform_streamer("alias", "bilibili")]
    workers = {}
    service = MonitorService(entries, lambda entry: workers.setdefault(
        entry["id"], PlatformWorker(entry, sec_uid="123")))

    assert service.prepare_all() == ["one", "two"]
    assert service._resolved_users == {"one": ("bilibili", "123"), "two": ("huya", "123")}
    assert workers["alias"].closed
    assert service.snapshot()[0]["platform"] == "bilibili"
    service.close()


class RecordingExecutor:
    """Keep admitted tasks unfinished to expose scheduler queue growth."""
    def __init__(self):
        self.submitted = []

    def submit(self, *args):
        future = Future()
        self.submitted.append((future, args))
        return future


def test_hundred_tasks_are_fair_and_never_queue_more_than_capacity(monkeypatch):
    now = 200.0
    monkeypatch.setattr("douyin_monitor.monitor_service.time.monotonic", lambda: now)
    platforms = ["douyin", "bilibili", "huya", "douyu", "kuaishou"]
    entries = [platform_streamer(str(index), platforms[index % 5]) for index in range(100)]
    service = MonitorService(entries, PlatformWorker, max_concurrent_checks=2,
                             enable_daily_intimacy_reminder=False)
    service.running = True
    service._workers = {entry["id"]: PlatformWorker(entry) for entry in entries}
    service._next_due = {entry["id"]: 100 - int(entry["id"]) for entry in entries}
    service._executor = RecordingExecutor()
    checked = []
    for _ in range(50):
        service._submit_due_checks()
        assert len(service._futures) == 2
        assert len({service._platform(item) for item in service._futures.values()}) == 2
        submitted = len(service._executor.submitted)
        service._submit_due_checks()
        assert len(service._executor.submitted) == submitted
        assert service._seconds_until_next_due() == 1.0
        for future, streamer_id in service._futures.items():
            checked.append(streamer_id)
            future.set_result(service._workers[streamer_id].result)
        service._collect_finished()
    assert checked == [str(index) for index in reversed(range(100))]
    assert all(runtime["check_count"] == 1 for runtime in service._runtime.values())
    assert not service._futures
    service.close()


def test_preparation_and_checks_share_platform_slot(monkeypatch):
    monkeypatch.setattr("douyin_monitor.monitor_service.time.monotonic", lambda: 100.0)
    entries = [platform_streamer("prepared", "huya"), platform_streamer("pending", "huya"),
               platform_streamer("other", "douyu")]
    service = MonitorService(entries, PlatformWorker, max_concurrent_checks=8)
    service.running = True
    service._executor = RecordingExecutor()
    service._workers["prepared"] = PlatformWorker(entries[0])
    service._submit_due_checks()
    assert set(service._futures.values()) == {"prepared", "other"}
    assert len(service._preparation_futures) == 1
    assert service._seconds_until_next_due() == 1.0
    service.stop()


@pytest.mark.parametrize("platform,expected", [("douyin", 1), ("bilibili", 30), ("huya", 30),
                                                ("douyu", 30), ("kuaishou", 60)])
def test_platform_minimum_interval(platform, expected):
    service = MonitorService([platform_streamer("one", platform)], PlatformWorker, check_interval=1)
    assert service._check_delay("one") == expected
    service.check_interval = 600
    assert service._check_delay("one") == 600


def test_unknown_result_cools_only_its_platform_and_preserves_live_metadata(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("douyin_monitor.monitor_service.time.monotonic", lambda: now[0])
    entries = [platform_streamer("limited", "huya"), platform_streamer("same", "huya"),
               platform_streamer("other", "douyu")]
    service = MonitorService(entries, PlatformWorker, max_concurrent_checks=2)
    service._record_success("limited", {"is_live": True, "room_id": "123", "title": "直播",
                                         "session_id": "session", "live_url": entries[0]["url"]})
    delay = service._record_success("limited", {"indeterminate": True, "is_live": False,
                                                "error_kind": "rate_limited", "cooldown": 120,
                                                "error": "https://private.invalid/token"})
    snapshot = service.snapshot()[0]
    assert delay == 120
    assert snapshot["room_id"] == "123" and snapshot["session_id"] == "session"
    assert snapshot["status"] == "error" and snapshot["check_count"] == 1
    assert snapshot["error_kind"] == "rate_limited"
    assert "private" not in snapshot["last_error"]
    service.running = True
    service._executor = RecordingExecutor()
    service._next_due = {entry["id"]: 100.0 for entry in entries}
    service._submit_due_checks()
    assert list(service._futures.values()) == ["other"]
    assert service._seconds_until_next_due() == 30.0
    now[0] = 220.0
    service._submit_due_checks()
    assert set(service._futures.values()) == {"other", "limited"}
    service.stop()


def test_once_omits_indeterminate_result_for_partial_failure():
    entries = [streamer("unknown"), streamer("good")]
    def factory(entry):
        return FakeWorker(entry, result={"indeterminate": True, "is_live": False}) if entry["id"] == "unknown" else FakeWorker(entry)
    service = MonitorService(entries, factory)
    assert set(service.check_all_once()) == {"good"}
    assert service.snapshot()[0]["status"] == "error"
    service.close()


def test_daily_reminder_only_includes_douyin_with_watch_link(monkeypatch):
    entries = [platform_streamer("douyin", "douyin"), platform_streamer("bili", "bilibili")]
    notifier = FakeNotifier()
    service = MonitorService(entries, PlatformWorker, service_notifier=notifier)
    service.prepare_all()
    for entry in entries:
        service._record_success(entry["id"], {"is_live": True, "live_url": entry["url"]})
    class FixedDateTime:
        @classmethod
        def now(cls):
            return __import__("datetime").datetime(2026, 10, 2, 23, 57, 10)
    monkeypatch.setattr("douyin_monitor.monitor_service.datetime", FixedDateTime)
    service._check_daily_intimacy_reminder()
    assert len(notifier.daily_messages) == 1
    payload = notifier.daily_messages[0][0]
    assert len(payload) == 1 and payload[0]["platform"] == "douyin"
    assert payload[0]["live_url"] == entries[0]["url"]
    service.close()


def test_once_rate_limit_skips_remaining_platform_requests_without_blocking_others():
    entries = [platform_streamer("limited", "huya"), platform_streamer("same", "huya"),
               platform_streamer("other", "douyu")]
    workers = {}
    def factory(entry):
        worker = workers[entry["id"]] = PlatformWorker(entry)
        worker.calls = 0
        def check():
            worker.calls += 1
            if entry["id"] == "limited":
                return {"is_live": None, "indeterminate": True,
                        "error_kind": "rate_limited", "cooldown": 60}
            return worker.result
        worker.check_once = check
        return worker
    service = MonitorService(entries, factory)
    assert set(service.check_all_once()) == {"other"}
    assert workers["limited"].calls == 1 and workers["same"].calls == 0
    assert workers["other"].calls == 1
    assert service.snapshot()[1]["error_kind"] == "rate_limited"
    service.close()


def test_queued_daily_and_startup_notifications_honor_stop(monkeypatch):
    entry = streamer("one")
    notifier = FakeNotifier()
    service = MonitorService([entry], FakeWorker, startup_notify=True, service_notifier=notifier)
    service.prepare_all()
    service.stop_streamer("one")
    assert service._send_daily_intimacy_reminder(notifier, [{"id": "one"}], 57) == (False, False)
    service._send_startup_notification()
    assert not notifier.messages and not notifier.daily_messages
    service.close()


def test_once_skipped_request_keeps_original_cooldown_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("douyin_monitor.monitor_service.time.monotonic", lambda: now[0])
    entry = platform_streamer("one", "huya")
    service = MonitorService([entry], PlatformWorker)
    service.prepare_all()
    service._platform_cooldown["huya"] = 160.0
    now[0] = 150.0
    assert service.check_all_once() == {}
    assert service._platform_cooldown["huya"] == 160.0
    service.close()


def test_authoritative_success_still_respects_metadata_rate_limit(monkeypatch):
    monkeypatch.setattr("douyin_monitor.monitor_service.time.monotonic", lambda: 100.0)
    entries = [platform_streamer("one", "bilibili"), platform_streamer("same", "bilibili"),
               platform_streamer("other", "huya")]
    service = MonitorService(entries, PlatformWorker)
    service._record_success("one", {"is_live": True, "room_id": "123", "session_id": "session-a",
                                    "error_kind": "rate_limited", "cooldown": 120})
    snapshot = service.snapshot()[0]
    assert snapshot["status"] == "live" and snapshot["session_id"] == "session-a"
    assert not snapshot["last_error"] and not service._faults
    assert service._platform_cooldown == {"bilibili": 220.0}
    service.running = True
    service._executor = RecordingExecutor()
    service._submit_due_checks()
    assert list(service._futures.values()) == ["other"]
    service.stop()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_daily_rejected_notification_is_submitted_once_per_minute(monkeypatch, asynchronous):
    response = Mock()
    response.json.return_value = {"code": 1}
    post = Mock(return_value=response)
    monkeypatch.setattr("douyin_monitor.notifier.requests.post", post)
    class FixedDateTime:
        @classmethod
        def now(cls):
            return __import__("datetime").datetime(2026, 10, 2, 23, 57, 10)
    monkeypatch.setattr("douyin_monitor.monitor_service.datetime", FixedDateTime)
    notifier = ServerChanNotifier(sendkey="example-key", uid="example-uid")
    service = MonitorService([streamer("one")], FakeWorker, service_notifier=notifier)
    service.prepare_all()
    service._runtime["one"].update(status="live", room_id="123")
    with ThreadPoolExecutor(max_workers=1) as executor:
        service.running = asynchronous
        service._notification_executor = executor if asynchronous else None
        service._check_daily_intimacy_reminder()
        if service._daily_reminder_future is not None:
            service._daily_reminder_future.result(timeout=2)
            service._collect_daily_reminder_result()
        service._check_daily_intimacy_reminder()
    assert post.call_count == 1
    assert service._daily_reminder_minutes_sent == {57}
    service.close()
