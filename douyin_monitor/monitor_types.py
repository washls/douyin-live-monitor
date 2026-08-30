"""Immutable public state and event types for monitoring interfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class StreamerStatus(str, Enum):
    PENDING = "pending"
    UNKNOWN = "unknown"
    LIVE = "live"
    OFFLINE = "offline"
    ERROR = "error"
    STOPPED = "stopped"


class MonitorEventType(str, Enum):
    PREPARED = "prepared"
    PREPARE_ERROR = "prepare_error"
    STATUS = "status"
    ERROR = "error"
    SUSPENDED = "suspended"
    STOPPED = "stopped"
    STREAMER_STOPPED = "streamer_stopped"
    LOG = "log"


@dataclass(frozen=True)
class WorkerPreparation:
    sec_uid: str
    nickname: str = ""
    status: StreamerStatus = StreamerStatus.UNKNOWN


@dataclass(frozen=True)
class StreamerSnapshot:
    id: str
    url: str
    label: str
    nickname: str
    status: StreamerStatus
    method: str
    room_id: str
    title: str
    last_check: str
    last_error: str
    check_count: int
    consecutive_errors: int
    consecutive_unknowns: int
    suspended: bool
    stopped_by_user: bool

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    def __getitem__(self, key: str) -> Any:
        value = getattr(self, key)
        return value.value if isinstance(value, Enum) else value

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except AttributeError:
            return default


@dataclass(frozen=True)
class MonitorEvent:
    type: MonitorEventType
    streamer_id: str = ""
    data: Mapping[str, Any] = MappingProxyType({})

    @classmethod
    def create(
        cls, event_type: str, streamer_id: str = "", **data: Any
    ) -> "MonitorEvent":
        return cls(
            type=MonitorEventType(event_type),
            streamer_id=str(streamer_id),
            data=MappingProxyType(dict(data)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **dict(self.data),
            "type": self.type.value,
            "streamer_id": self.streamer_id,
        }

    def __getitem__(self, key: str) -> Any:
        if key == "type":
            return self.type.value
        if key == "streamer_id":
            return self.streamer_id
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default
