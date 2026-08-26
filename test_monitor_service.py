from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

import pytest

from monitor_service import LiveStatusUnknownError, MonitorService
from streamer_logging import StreamerLogHandler


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


def streamer(streamer_id, label=""):
    return {
        "id": streamer_id,
        "url": f"https://www.douyin.com/user/{streamer_id}",
        "label": label,
        "enabled": True,
    }


def test_duplicate_resolved_user_is_suspended():
    entries = [streamer("one"), streamer("two")]

    service = MonitorService(
        entries,
        worker_factory=lambda entry: FakeWorker(entry, sec_uid="same-user"),
    )

    assert service.prepare_all() == ["one"]
    snapshot = {item["id"]: item for item in service.snapshot()}
    assert snapshot["one"]["suspended"] is False
    assert snapshot["two"]["suspended"] is True
    assert "指向同一账号" in snapshot["two"]["last_error"]


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

    monkeypatch.setattr("monitor_service.datetime", FixedDateTime)

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

    monkeypatch.setattr("monitor_service.datetime", FixedDateTime)
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

    monkeypatch.setattr("monitor_service.datetime", FixedDateTime)
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

    service._emit("probe", "one")

    assert events == [{"type": "probe", "streamer_id": "one"}]
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
    def factory(entry):
        worker = FakeWorker(entry)
        worker.prepare = lambda: None
        return worker

    service = MonitorService(
        [streamer("bad")],
        worker_factory=factory,
    )

    assert service.run() is False
