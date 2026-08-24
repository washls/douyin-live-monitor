import unittest
from unittest.mock import Mock, patch
import requests
import threading

from monitor import DouyinLiveMonitor, MonitorState
from notifier import ServerChanNotifier
from douyin_client import DouyinClient


class MonitorStateTests(unittest.TestCase):
    def test_first_notification_is_retryable_until_success(self):
        state = MonitorState()
        state.transition(True, {"nickname": "主播", "room_id": "1"})

        self.assertTrue(state.should_notify_first_live())
        self.assertTrue(state.should_notify_first_live())
        self.assertFalse(state.should_notify_repeat())

        with patch("monitor.time.time", return_value=100.0):
            state.mark_first_notification_sent()
            self.assertFalse(state.should_notify_first_live())
            self.assertFalse(state.should_notify_repeat())

    def test_new_room_resets_first_notification(self):
        state = MonitorState()
        state.transition(True, {"room_id": "old"})
        state.mark_first_notification_sent()

        transition = state.transition(True, {"room_id": "new"})

        self.assertEqual(transition, "went_live")
        self.assertTrue(state.should_notify_first_live())

    def test_offline_transition_is_emitted_once(self):
        state = MonitorState()
        state.transition(True, {"room_id": "1"})

        self.assertEqual(state.transition(False, {}), "went_offline")
        self.assertEqual(state.transition(False, {}), "no_change")


class NotifierTests(unittest.TestCase):
    def test_successful_send_posts_once(self):
        notifier = ServerChanNotifier(sendkey="key", uid="uid")
        response = Mock(text='{"code": 0, "message": "ok"}')
        response.json.return_value = {"code": 0, "message": "ok"}
        with patch("notifier.requests.post", return_value=response) as post:
            self.assertTrue(notifier.send("same notification"))
        self.assertEqual(post.call_count, 1)

    def test_timeout_is_not_retried(self):
        notifier = ServerChanNotifier(sendkey="key", uid="uid")
        with patch("notifier.requests.post", side_effect=requests.Timeout()) as post:
            self.assertFalse(notifier.send("same notification"))
        self.assertTrue(notifier.delivery_unknown)
        self.assertEqual(post.call_count, 1)

    def test_unexpected_send_error_is_not_retried(self):
        notifier = ServerChanNotifier(sendkey="key", uid="uid")
        with patch("notifier.requests.post", side_effect=RuntimeError("network failure")) as post:
            self.assertFalse(notifier.send("same notification"))
        self.assertEqual(post.call_count, 1)

    def test_custom_api_url_is_redacted_in_logs(self):
        notifier = ServerChanNotifier(sendkey="key", uid="uid")
        secret = "placeholder-secret-value"

        with self.assertLogs("notifier", level="INFO") as captured:
            notifier.set_api_url(
                f"https://12345.push.ft07.com/send/{secret}.send"
            )

        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertIn("<redacted>", output)

    def test_short_link_redirect_is_cached(self):
        client = DouyinClient()
        response = Mock(url="https://www.douyin.com/user/sec_uid_1")
        client.session.head = Mock(return_value=response)

        self.assertEqual(
            client.resolve_short_link("https://v.douyin.com/example/"),
            response.url,
        )
        self.assertEqual(
            client.resolve_short_link("https://v.douyin.com/example/"),
            response.url,
        )
        self.assertEqual(client.session.head.call_count, 1)


class MonitorControlTests(unittest.TestCase):
    def test_stop_marks_monitor_as_not_running(self):
        monitor = object.__new__(DouyinLiveMonitor)
        monitor.running = True
        monitor._stop_event = threading.Event()

        monitor.stop()

        self.assertFalse(monitor.running)
        self.assertTrue(monitor._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
