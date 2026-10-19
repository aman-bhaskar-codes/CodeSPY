"""Application scheduling only. PostgreSQL owns live lease/publication time."""

import time
from datetime import UTC, datetime
from typing import Protocol


class AppClock(Protocol):
    def wall_now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def wall_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
