from dataclasses import dataclass
import threading

import pytest

from monitor_service import MonitorService


@dataclass
class FakeState:
    streamer_nickname: str
    status: str = "unknown"


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, title, desp=""):
        self.messages.append((title, desp))
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

    def prepare(self):
        return self.sec_uid

    def check_once(self):
        if self.error:
            raise self.error
        return dict(self.result)

    def stop(self):
        self.stopped = True


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


def test_startup_notification_is_aggregated_into_one_post():
    entries = [streamer("one", "主播甲"), streamer("two", "主播乙")]
    workers = {}

    def factory(entry):
        worker = FakeWorker(entry)
        workers[entry["id"]] = worker
        return worker

    service = MonitorService(
        entries,
        worker_factory=factory,
        startup_notify=True,
    )
    service.prepare_all()
    service._send_startup_notification()

    total_messages = sum(len(worker.notifier.messages) for worker in workers.values())
    assert total_messages == 1
    title, body = workers["one"].notifier.messages[0]
    assert "已启动" in title
    assert "主播甲" in body
    assert "主播乙" in body


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
