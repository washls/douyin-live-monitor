"""Regression coverage for preparation recovery and one-shot fault alerts."""

from concurrent.futures import Future, ThreadPoolExecutor
import socket
import threading
from unittest.mock import Mock

import pytest
import requests

import monitor
from douyin_monitor import douyin_client, monitor_service
from douyin_monitor.douyin_client import DouyinClient
from douyin_monitor.monitor_errors import ProfileDNSError
from douyin_monitor.monitor_service import LiveStatusUnknownError, MonitorService
from douyin_monitor.monitor_types import WorkerPreparation
from douyin_monitor.notifier import ServerChanNotifier
from test_monitor_service import FakeWorker, streamer


class AlertNotifier:
    def __init__(self, success=True, unknown=False):
        self.events = []
        self.success = success
        self.delivery_unknown = unknown

    def send_monitor_alert(self, name, reason, duration, retrying):
        self.events.append(("error" if retrying else "suspended", name, reason, duration))
        return self.success

    def send_monitor_recovery(self, name, duration):
        self.events.append(("recovery", name, duration))
        return self.success


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(monitor_service.time, "monotonic", lambda: now[0])
    return now


def failed_future(exc):
    future = Future()
    future.set_exception(exc)
    return future


def flush_alerts(service):
    with ThreadPoolExecutor(max_workers=1) as executor:
        service._notification_executor = executor
        service._check_fault_notifications()
        for _ in range(10):
            if service._alert_future is not None:
                service._alert_future.result(timeout=2)
            service._check_fault_notifications()
            if not service._pending_alerts and service._alert_future is None:
                break
        else:
            pytest.fail("notification queue did not drain")
    service._notification_executor = None


def test_dns_failure_retains_retryable_type_and_safe_reason(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(douyin_client.socket, "getaddrinfo", fail)
    with pytest.raises(ProfileDNSError, match="DNS") as caught:
        DouyinClient._validate_redirect_target("https://v.douyin.com/example/")
    assert isinstance(caught.value.__cause__, socket.gaierror)


def test_worker_prepare_propagates_network_error():
    client = Mock()
    client.resolve_short_link.side_effect = ProfileDNSError("DNS")
    worker = monitor.DouyinLiveMonitor(
        monitor._default_config(), "https://v.douyin.com/example/",
        client=client, notifier=Mock(),
    )
    with pytest.raises(ProfileDNSError):
        worker.prepare()
    worker.close()


def test_retry_delays_and_five_minute_deadline_before_next_attempt(clock):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one")], FakeWorker, service_notifier=notifier)
    delays = []
    for elapsed in (0, 10, 40, 100):
        clock[0] = 100 + elapsed
        delay = service._collect_preparation("one", failed_future(ProfileDNSError("secret")))
        delays.append(delay)
    assert delays == [10, 30, 60, 300]
    assert service.snapshot()[0]["status"] == "retrying"
    assert service.snapshot()[0]["suspended"] is False
    assert service._has_runnable_workers()
    service._next_due["one"] = 500
    clock[0] = 399
    assert service._seconds_until_next_due() == 1
    flush_alerts(service)
    assert notifier.events == []
    clock[0] = 400
    flush_alerts(service)
    assert notifier.events == [("error", "未命名主播", "DNS 域名解析失败", 300)]
    flush_alerts(service)
    assert len(notifier.events) == 1


@pytest.mark.parametrize("success,unknown", [(True, False), (False, False), (False, True)])
def test_fault_recovery_and_new_fault_never_replay_alert(clock, success, unknown):
    notifier = AlertNotifier(success, unknown)
    service = MonitorService([streamer("one", "测试主播")], FakeWorker, service_notifier=notifier)
    service.prepare_all()
    service._record_error("one", LiveStatusUnknownError("网络连接失败"))
    clock[0] += 200
    service._record_error("one", LiveStatusUnknownError("其他错误"))
    clock[0] += 100
    flush_alerts(service)
    flush_alerts(service)
    assert len(notifier.events) == 1
    clock[0] += 30
    service._record_success("one", {"is_live": False, "method": "dual_source"})
    service._record_success("one", {"is_live": False})
    flush_alerts(service)
    assert [item[0] for item in notifier.events] == ["error", "recovery"]
    assert notifier.events[-1][-1] == 330
    service._record_error("one", LiveStatusUnknownError("unknown"))
    clock[0] += 300
    flush_alerts(service)
    assert [item[0] for item in notifier.events] == ["error", "recovery", "error"]
    assert service.snapshot()[0]["notification_status"]


def test_recovery_requires_confirmed_status_not_only_profile_resolution(clock):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one")], FakeWorker, service_notifier=notifier)
    service._collect_preparation("one", failed_future(requests.Timeout("private URL")))
    clock[0] += 300
    flush_alerts(service)
    future = Future()
    worker = FakeWorker(streamer("one"))
    future.set_result((worker, WorkerPreparation("sec-test")))
    service._collect_preparation("one", future)
    flush_alerts(service)
    assert [item[0] for item in notifier.events] == ["error"]
    service._record_success("one", worker.result)
    flush_alerts(service)
    assert [item[0] for item in notifier.events] == ["error", "recovery"]


def test_transient_fault_under_threshold_and_successful_fallback_are_silent(clock):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one")], FakeWorker, service_notifier=notifier)
    service._record_error("one", requests.ConnectionError("secret"))
    clock[0] += 299
    service._record_success("one", {"is_live": False, "method": "full_check"})
    clock[0] += 300
    flush_alerts(service)
    assert not notifier.events


def test_permanent_error_and_ten_errors_send_single_suspension(clock):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one"), streamer("two")], FakeWorker, service_notifier=notifier)
    service._collect_preparation("one", failed_future(ValueError("https://private.invalid/token")))
    for _ in range(10):
        service._record_error("two", RuntimeError("private diagnostic"))
    flush_alerts(service)
    assert [item[0] for item in notifier.events] == ["suspended", "suspended"]
    assert "private" not in repr(notifier.events)
    assert not service._has_runnable_workers()


def test_fault_then_suspension_sends_one_additional_event(clock):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one")], FakeWorker, service_notifier=notifier)
    service._record_error("one", LiveStatusUnknownError("unknown"))
    clock[0] += 300
    flush_alerts(service)
    for _ in range(11):
        service._record_error("one", RuntimeError("error"))
    flush_alerts(service)
    assert [item[0] for item in notifier.events] == ["error", "suspended"]


def test_duplicate_after_retry_is_closed_without_fault_alert(clock):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one"), streamer("two")], FakeWorker, service_notifier=notifier)
    service._resolved_users["one"] = "same"
    service._collect_preparation("two", failed_future(ProfileDNSError("DNS")))
    worker = FakeWorker(streamer("two"))
    future = Future()
    future.set_result((worker, WorkerPreparation("same")))
    service._collect_preparation("two", future)
    clock[0] += 400
    flush_alerts(service)
    assert worker.closed
    assert service.snapshot()[1]["suspended"]
    assert not notifier.events


@pytest.mark.parametrize("stop_all", [False, True])
def test_stopped_preparation_cannot_publish_or_alert(clock, stop_all):
    notifier = AlertNotifier()
    service = MonitorService([streamer("one")], FakeWorker, service_notifier=notifier)
    service.running = True
    if stop_all:
        service.stop()
    else:
        assert service.stop_streamer("one")
    worker = FakeWorker(streamer("one"))
    future = Future()
    future.set_result((worker, WorkerPreparation("sec-test")))
    service._collect_preparation("one", future)
    service._collect_preparation("one", failed_future(ProfileDNSError("DNS")))
    flush_alerts(service)
    assert not service._workers
    assert worker.closed
    assert not notifier.events


def test_failed_candidates_close_and_normal_streamer_keeps_running():
    created = []
    holder = {}

    def factory(entry):
        worker = FakeWorker(entry)
        if entry["id"] == "bad":
            worker.prepare = Mock(side_effect=ProfileDNSError("DNS"))
        created.append(worker)
        return worker

    def on_event(event):
        if event["type"] == "status":
            holder["service"].stop()

    service = MonitorService([streamer("bad"), streamer("good")], factory,
                             check_interval=1, on_event=on_event)
    holder["service"] = service
    assert service.run()
    assert service._runtime["good"]["check_count"] == 1
    assert all(worker.closed for worker in created)


def test_all_permanent_failures_notify_before_service_finishes():
    notifier = AlertNotifier()
    def factory(entry):
        worker = FakeWorker(entry)
        worker.prepare = Mock(side_effect=ValueError("invalid profile"))
        return worker
    service = MonitorService([streamer("one"), streamer("two")], factory,
                             check_interval=1, service_notifier=notifier)
    assert service.run() is False
    assert [item[0] for item in notifier.events] == ["suspended", "suspended"]


def test_running_service_recovers_all_failed_preparations(monkeypatch):
    attempts = [0]
    holder = {}
    def factory(entry):
        attempts[0] += 1
        worker = FakeWorker(entry)
        if attempts[0] == 1:
            worker.prepare = Mock(side_effect=ProfileDNSError("DNS"))
        return worker
    def on_event(event):
        if event["type"] == "status":
            holder["service"].stop()
    service = MonitorService([streamer("one")], factory, check_interval=1, on_event=on_event)
    original = service._collect_preparation
    def collect(streamer_id, future):
        original(streamer_id, future)
        return 0.0
    monkeypatch.setattr(service, "_collect_preparation", collect)
    holder["service"] = service
    thread = threading.Thread(target=service.run)
    thread.start()
    thread.join(3)
    if thread.is_alive():
        service.stop()
        thread.join(3)
        pytest.fail("all failed preparations did not recover")
    assert attempts == [2]
    assert service._runtime["one"]["check_count"] == 1


def test_pending_alerts_remain_bounded(clock):
    service = MonitorService([streamer("one")], FakeWorker)
    for _ in range(100):
        service._start_fault("one", "网络连接失败")
        clock[0] += 300
        service._check_fault_notifications()
        service._record_success("one", {"is_live": False})
    assert len(service._pending_alerts) <= 3


def test_once_failure_attempts_only_once_and_notifies():
    notifier = AlertNotifier()
    worker = FakeWorker(streamer("one"))
    worker.prepare = Mock(side_effect=ProfileDNSError("DNS"))
    service = MonitorService([streamer("one")], lambda entry: worker, service_notifier=notifier)
    assert service.check_all_once() == {}
    assert worker.prepare.call_count == 1
    assert worker.closed
    assert len(notifier.events) == 1
    assert not service._futures


def test_inflight_preparation_stopped_before_return_closes_worker():
    entered, release = threading.Event(), threading.Event()
    worker = FakeWorker(streamer("one"))
    def prepare():
        entered.set()
        assert release.wait(3)
        return "sec-test"
    worker.prepare = prepare
    service = MonitorService([streamer("one")], lambda entry: worker)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service._prepare_worker, streamer("one"))
        assert entered.wait(2)
        assert service.stop_streamer("one")
        release.set()
        future.result(timeout=2)
        service._collect_preparation("one", future)
    assert service.snapshot()[0]["status"] == "stopped"
    assert worker.closed
    assert not service._workers


def test_one_inflight_task_per_streamer():
    entered, release = threading.Event(), threading.Event()
    worker = FakeWorker(streamer("one"))
    def prepare():
        entered.set()
        assert release.wait(3)
        return "sec-test"
    worker.prepare = Mock(side_effect=prepare)
    service = MonitorService([streamer("one")], lambda entry: worker)
    service.running = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        service._executor = executor
        service._submit_due_checks()
        assert entered.wait(2)
        service._submit_due_checks()
        assert len(service._futures) == 1
        release.set()
    service.stop()
    service._collect_finished()
    assert worker.prepare.call_count == 1
    assert worker.closed


def test_runtime_request_failures_keep_network_reason(monkeypatch):
    client = DouyinClient()
    monkeypatch.setattr(client, "_ensure_cookies", lambda: True)
    monkeypatch.setattr(client.session, "get", Mock(side_effect=requests.ConnectionError("secret")))
    result = client.check_live(sec_uid="example")
    assert result["indeterminate"]
    assert result["error"] == "网络连接失败"
    client.close()


def test_waiting_retry_ui_exposes_reason_time_and_notification():
    from douyin_monitor.gui_app import MonitorGui, status_text
    gui = object.__new__(MonitorGui)
    gui.runtime_by_id = {"one": {
        "status": "retrying", "last_error": "DNS 域名解析失败",
        "next_retry": "12:05:00", "notification_status": "异常/恢复通知发送失败，不重发",
    }}
    state, _, _, detail = gui._streamer_row_values(streamer("one", "测试主播"))
    assert status_text(state) == "等待重试"
    assert "DNS" in detail and "12:05:00" in detail and "通知发送失败" in detail


@pytest.mark.parametrize("outcome", ["success", "rejected", "timeout", "connection", "http500"])
def test_actual_fault_notifier_submits_one_post(monkeypatch, outcome):
    response = Mock()
    response.json.return_value = {"code": 0 if outcome == "success" else 1}
    if outcome == "http500":
        response.status_code = 500
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    post = Mock(return_value=response)
    if outcome == "timeout":
        post.side_effect = requests.Timeout()
    if outcome == "connection":
        post.side_effect = requests.ConnectionError()
    monkeypatch.setattr(requests, "post", post)
    notifier = ServerChanNotifier(sendkey="example-key", uid="example")
    notifier.send_monitor_alert("测试主播", "DNS 域名解析失败", 300, True)
    assert post.call_count == 1
    assert notifier.delivery_unknown == (outcome in {"timeout", "connection", "http500"})
