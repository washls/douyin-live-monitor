"""Bounded broadcast, cancellation and ownership regressions."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from unittest.mock import Mock

import pytest

from douyin_monitor.notifier import MultiChannelNotifier
from douyin_monitor.push_channels import PushChannel, PushDispatcher, PushMessage


def config(identifier="one"):
    return {"id": identifier, "name": identifier, "type": "webhook",
            "config": {"url": "https://example.invalid/" + identifier, "headers": {}}}


class FakeChannel(PushChannel):
    def __init__(self, identifier, send=None):
        super().__init__(config(identifier))
        self.calls = 0
        self.operation = send

    def send(self, message):
        self.calls += 1
        if self.operation is not None:
            return self.operation(self, message)
        return self.result("accepted")


def test_each_target_is_attempted_once_even_when_an_adapter_raises():
    def broken(_channel, _message):
        raise RuntimeError("private URL")

    channels = [FakeChannel(str(index), broken if index == 2 else None) for index in range(7)]
    with PushDispatcher() as dispatcher:
        report = dispatcher.send(PushMessage("live", "event"), channels)
    assert [channel.calls for channel in channels] == [1] * 7
    assert report.accepted_count == 6
    assert report.unknown_count == 1
    assert [item.channel_id for item in report.results] == [str(index) for index in range(7)]
    assert "private" not in repr(report)


def test_global_four_slots_bound_two_simultaneous_broadcasts():
    release = threading.Event()
    four_started = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    starts = 0

    def blocked(channel, _message):
        nonlocal active, maximum, starts
        with lock:
            active += 1
            starts += 1
            maximum = max(maximum, active)
            if starts == 4:
                four_started.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return channel.result("accepted")

    channels = [FakeChannel(str(index), blocked) for index in range(7)]
    with PushDispatcher() as dispatcher, ThreadPoolExecutor(max_workers=2) as callers:
        futures = [callers.submit(dispatcher.send, PushMessage("live", "event"), channels) for _ in range(2)]
        try:
            assert four_started.wait(2)
            assert starts == 4
            assert dispatcher._executor._work_queue.qsize() <= 4
        finally:
            release.set()
        assert all(future.result(timeout=3).all_accepted for future in futures)
    assert maximum == 4
    assert [channel.calls for channel in channels] == [2] * 7


@pytest.mark.parametrize("stop_dispatcher", [False, True])
def test_stop_while_waiting_for_slots_cancels_remaining_targets(stop_dispatcher):
    release = threading.Event()
    four_started = threading.Event()
    cancelled = threading.Event()
    lock = threading.Lock()
    starts = 0

    def blocked(channel, _message):
        nonlocal starts
        with lock:
            starts += 1
            if starts == 4:
                four_started.set()
        assert release.wait(3)
        return channel.result("accepted")

    channels = [FakeChannel(str(index), blocked) for index in range(7)]
    with PushDispatcher() as dispatcher, ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(dispatcher.send, PushMessage("live", "event"), channels,
                               cancelled=cancelled.is_set)
        try:
            assert four_started.wait(2)
            dispatcher.stop() if stop_dispatcher else cancelled.set()
        finally:
            release.set()
        report = future.result(timeout=3)
        assert [item.status for item in report.results] == ["accepted"] * 4 + ["cancelled"] * 3
        assert [channel.calls for channel in channels] == [1] * 4 + [0] * 3
        if not stop_dispatcher:
            assert dispatcher.send(PushMessage("live", "another streamer"), [FakeChannel("other")]).all_accepted


def test_stop_before_broadcast_calls_no_adapters_and_close_is_idempotent():
    dispatcher = PushDispatcher()
    channels = [FakeChannel("one")]
    dispatcher.stop()
    assert dispatcher.send(PushMessage("live", "event"), channels).results[0].status == "cancelled"
    dispatcher.close()
    dispatcher.close()
    assert dispatcher.send(PushMessage("live", "after close"), channels).results[0].status == "cancelled"
    assert channels[0].calls == 0


def test_cancellation_callback_can_stop_dispatcher_without_lock_recursion():
    with PushDispatcher() as dispatcher, ThreadPoolExecutor(max_workers=1) as caller:
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            if calls == 2:
                dispatcher.stop()
            return False

        channel = FakeChannel("one")
        report = caller.submit(dispatcher.send, PushMessage("live", "event"), [channel],
                               cancelled=cancelled).result(timeout=2)
        assert report.results[0].status == "cancelled"
        assert channel.calls == 0


def test_failed_cancellation_check_cancels_without_leaking_capacity():
    def broken():
        raise RuntimeError("private cancellation value")

    with PushDispatcher() as dispatcher:
        channel = FakeChannel("one")
        assert dispatcher.send(PushMessage("live", "event"), [channel], cancelled=broken).results[0].status == "cancelled"
        assert dispatcher.send(PushMessage("live", "other"), [channel]).all_accepted


def test_each_channel_task_receives_its_own_copy_of_calling_context():
    context = ContextVar("example_streamer", default="")
    token = context.set("streamer-one")
    seen = []

    def capture(channel, _message):
        seen.append(context.get())
        context.set("changed-inside-channel")
        return channel.result("accepted")

    try:
        with PushDispatcher() as dispatcher:
            assert dispatcher.send(PushMessage("live", "event"), [FakeChannel(str(i), capture) for i in range(7)]).all_accepted
        assert seen == ["streamer-one"] * 7
        assert context.get() == "streamer-one"
    finally:
        context.reset(token)


def test_clone_shares_dispatcher_without_sharing_report_or_ownership():
    with MultiChannelNotifier([config()]) as owner:
        clone = owner.clone()
        owner.channels = (FakeChannel("one"),)
        clone.channels = (FakeChannel("one", lambda channel, _message: channel.result("unknown")),)
        assert clone.dispatcher is owner.dispatcher
        assert owner.owns_dispatcher
        assert not clone.owns_dispatcher
        assert owner.send("owner")
        assert not clone.send("clone")
        assert owner.last_report.accepted_count == 1
        assert not owner.delivery_unknown
        assert clone.delivery_unknown
        clone.close()
        assert owner.send("still works")


@pytest.mark.parametrize("statuses,successful,unknown", [
    (["accepted", "accepted"], True, False),
    (["accepted", "rejected"], True, False),
    (["accepted", "unknown"], True, True),
    (["rejected", "rejected"], False, False),
    (["unknown", "rejected"], False, True),
])
def test_facade_reports_all_results_and_preserves_bool_compatibility(statuses, successful, unknown):
    received = []
    with MultiChannelNotifier([config(str(i)) for i in range(2)], on_report=received.append) as notifier:
        notifier.channels = tuple(FakeChannel(str(i), lambda channel, _message, status=status: channel.result(status))
                                  for i, status in enumerate(statuses))
        assert notifier.send("event") is successful
        assert notifier.delivery_unknown is unknown
        assert received == [notifier.last_report]
        assert [item.status for item in received[0].results] == statuses


def test_empty_selection_publishes_report_without_sending():
    received = []
    with MultiChannelNotifier([], on_report=received.append) as notifier:
        assert not notifier.send("event")
        assert received[0].results == ()
        assert received[0].event_id
        assert "不推送" in received[0].summary
        assert not notifier.verify_connection()


def test_verify_requires_all_targets_and_keeps_callback_failure_out_of_delivery():
    with MultiChannelNotifier([config("a"), config("b")]) as notifier:
        notifier.channels = (FakeChannel("a"), FakeChannel("b", lambda channel, _message: channel.result("unknown")))
        assert not notifier.verify_connection()
        notifier.channels = (FakeChannel("a"),)
        notifier.on_report = Mock(side_effect=RuntimeError("private value"))
        assert notifier.verify_connection()
        assert notifier.last_report.all_accepted
