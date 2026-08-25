import unittest
from unittest.mock import Mock, patch
import requests
import threading

from monitor import (
    DouyinLiveMonitor,
    MonitorState,
    _configure_console_output,
    _confirm_remove,
    _load_streamer_entries,
    is_serverchan_configured,
)
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
    def test_push_url_requires_exact_https_serverchan_address(self):
        valid = "https://12345.push.ft07.com/send/example-key.send"

        self.assertEqual(
            ServerChanNotifier._parse_push_url(valid),
            {"uid": "12345", "sendkey": "example-key"},
        )
        for invalid in (
            "http://12345.push.ft07.com/send/example-key.send",
            f"{valid}.extra",
            "https://example.com/send/example-key.send",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    ServerChanNotifier._parse_push_url(invalid)

    def test_serverchan_configuration_uses_the_same_url_validation(self):
        self.assertTrue(
            is_serverchan_configured(
                {
                    "push_url": (
                        "https://12345.push.ft07.com/send/example-key.send"
                    )
                }
            )
        )
        self.assertFalse(
            is_serverchan_configured(
                {
                    "push_url": (
                        "http://12345.push.ft07.com/send/example-key.send"
                    )
                }
            )
        )

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
    def test_indeterminate_check_then_live_notifies_once(self):
        live_monitor = object.__new__(DouyinLiveMonitor)
        live_monitor.target_url = "https://www.douyin.com/user/recovering"
        live_monitor.client = Mock()
        live_monitor.client.check_live.side_effect = [
            {
                "is_live": False,
                "indeterminate": True,
                "error": "profile_api_empty_response",
            },
            {
                "is_live": True,
                "nickname": "恢复主播",
                "room_id": "room-1",
                "title": "直播标题",
            },
            {
                "is_live": True,
                "nickname": "恢复主播",
                "room_id": "room-1",
                "title": "直播标题",
            },
        ]
        live_monitor.state = MonitorState()
        live_monitor.notifier = Mock(delivery_unknown=False)
        live_monitor.notifier.send_live_notification.return_value = True
        live_monitor.config = {"enable_daily_intimacy_reminder": False}
        live_monitor.notify_on_end = True

        with self.assertRaisesRegex(RuntimeError, "profile_api_empty_response"):
            live_monitor.check_once()

        live_monitor.check_once()
        live_monitor.check_once()

        live_monitor.notifier.send_live_notification.assert_called_once()

    def test_windows_console_is_reconfigured_without_being_closed(self):
        stream = Mock()

        with patch("monitor.sys.platform", "win32"):
            _configure_console_output(stream)

        stream.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="replace"
        )
        stream.close.assert_not_called()

    def test_stop_marks_monitor_as_not_running(self):
        monitor = object.__new__(DouyinLiveMonitor)
        monitor.running = True
        monitor._stop_event = threading.Event()

        monitor.stop()

        self.assertFalse(monitor.running)
        self.assertTrue(monitor._stop_event.is_set())

    def test_remove_confirmation_eof_requires_yes_flag(self):
        stdin = Mock()
        stdin.isatty.return_value = True
        entry = {
            "id": "streamer-one",
            "url": "https://www.douyin.com/user/example",
            "label": "测试主播",
        }

        with patch("monitor.sys.stdin", stdin), patch(
            "builtins.input", side_effect=EOFError
        ):
            with self.assertRaisesRegex(ValueError, "--yes"):
                _confirm_remove(entry, assume_yes=False)

    def test_existing_streamers_do_not_log_legacy_migration(self):
        config = {
            "streamers": [
                {
                    "id": "streamer-one",
                    "url": "https://www.douyin.com/user/example",
                    "label": "现有主播",
                    "enabled": True,
                }
            ],
            "max_concurrent_checks": 2,
        }
        legacy = {"target_url": "https://v.douyin.com/old/"}

        with patch("monitor.load_monitor_state", return_value=legacy), patch(
            "monitor.logger.info"
        ) as log_info:
            entries = _load_streamer_entries(Mock(), config)

        self.assertEqual(len(entries), 1)
        self.assertFalse(
            any("迁移" in str(call) for call in log_info.call_args_list)
        )

    def test_invalid_legacy_streamer_does_not_block_empty_config(self):
        config = {"streamers": [], "max_concurrent_checks": 2}
        legacy = {"target_url": "https://example.com/not-douyin"}

        with patch("monitor.load_monitor_state", return_value=legacy), patch(
            "monitor.logger.warning"
        ) as log_warning:
            entries = _load_streamer_entries(Mock(), config)

        self.assertEqual(entries, [])
        self.assertTrue(log_warning.called)


if __name__ == "__main__":
    unittest.main()
