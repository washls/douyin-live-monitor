import logging
import threading

from streamer_logging import (
    RedactingFormatter,
    StreamerLogHandler,
    StreamerLogStore,
    compact_log_line,
    streamer_log_context,
)


def test_log_handler_keeps_concurrent_streamers_separate():
    events = []
    handler = StreamerLogHandler(events.append)
    logger = logging.getLogger("streamer-log-test")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    barrier = threading.Barrier(2)

    def emit(streamer_id, message):
        with streamer_log_context(streamer_id):
            barrier.wait()
            logger.info(message)

    threads = [
        threading.Thread(target=emit, args=("one", "主播一日志")),
        threading.Thread(target=emit, args=("two", "主播二日志")),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
    finally:
        logger.removeHandler(handler)

    assert {event["streamer_id"] for event in events} == {"one", "two"}
    assert any("主播一日志" in event["message"] for event in events)
    assert any("主播二日志" in event["message"] for event in events)


def test_log_store_is_bounded_and_compacts_multiline_messages():
    store = StreamerLogStore(max_lines=2)

    store.append("one", "第一行")
    store.append("one", "第二行\n继续")
    store.append("one", "第三行")

    assert store.get("one") == ["第二行 继续", "第三行"]
    assert store.count("one") == 2
    assert store.get("two") == []


def test_log_handler_ignores_records_without_streamer_context():
    events = []
    handler = StreamerLogHandler(events.append)
    record = logging.LogRecord(
        "plain", logging.INFO, __file__, 1, "全局日志", (), None
    )

    handler.emit(record)

    assert events == []


def test_log_lines_redact_credentials_and_request_tokens():
    line = compact_log_line(
        "GET https://123.push.ft07.com/send/private-key.send"
        "?msToken=private-token&a_bogus=private-signature "
        "Cookie: ttwid=private-cookie"
    )

    assert "private-key" not in line
    assert "private-token" not in line
    assert "private-signature" not in line
    assert "private-cookie" not in line
    assert line.count("<redacted>") == 4


def test_redacting_formatter_protects_console_and_file_messages():
    secret = "formatter-secret"
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1,
        f"failed https://1.push.ft07.com/send/{secret}.send", (), None,
    )

    rendered = RedactingFormatter("%(levelname)s %(message)s").format(record)

    assert secret not in rendered
    assert "<redacted>" in rendered
