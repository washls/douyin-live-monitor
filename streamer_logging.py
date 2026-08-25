"""Per-streamer logging context shared by the scheduler and desktop UI."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Iterator, List


CURRENT_STREAMER_ID: ContextVar[str] = ContextVar(
    "current_streamer_id", default=""
)


def compact_log_line(value: Any, limit: int = 400) -> str:
    """Collapse one log entry to a bounded single line."""
    return " ".join(str(value or "").split())[:limit].rstrip()


@contextmanager
def streamer_log_context(streamer_id: str) -> Iterator[None]:
    """Attach a streamer id to every log emitted in this execution context."""
    token = CURRENT_STREAMER_ID.set(str(streamer_id))
    try:
        yield
    finally:
        CURRENT_STREAMER_ID.reset(token)


def run_with_streamer_context(
    streamer_id: str, callback: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run one worker operation with an isolated logging context."""
    with streamer_log_context(streamer_id):
        return callback(*args, **kwargs)


class StreamerLogHandler(logging.Handler):
    """Forward contextual log records to the GUI event queue."""

    def __init__(self, callback: Callable[[Dict[str, Any]], None]):
        super().__init__(level=logging.DEBUG)
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        streamer_id = CURRENT_STREAMER_ID.get()
        if not streamer_id:
            return
        try:
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            message = compact_log_line(record.getMessage())
            self.callback(
                {
                    "type": "log",
                    "streamer_id": streamer_id,
                    "message": f"{timestamp} [{record.levelname}] {message}",
                }
            )
        except Exception:
            self.handleError(record)


class StreamerLogStore:
    """Bounded current-session logs keyed by local streamer id."""

    def __init__(self, max_lines: int = 500):
        if int(max_lines) < 1:
            raise ValueError("max_lines 必须大于 0")
        self.max_lines = int(max_lines)
        self._logs: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=self.max_lines)
        )

    def append(self, streamer_id: str, message: Any) -> str:
        line = compact_log_line(message)
        self._logs[str(streamer_id)].append(line)
        return line

    def get(self, streamer_id: str) -> List[str]:
        return list(self._logs.get(str(streamer_id), ()))

    def clear(self) -> None:
        self._logs.clear()
