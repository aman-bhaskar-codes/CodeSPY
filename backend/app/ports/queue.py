"""Receipt handles are private transport state, never job ownership."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Delivery:
    body: bytes
    receipt: str


class QueuePort(Protocol):
    async def send(self, body: bytes) -> None: ...
    async def receive(self, max_messages: int = 1, *, wait_seconds: int = 20) -> list[Delivery]: ...
    async def extend(self, receipt: str, seconds: int) -> None: ...
    async def ack(self, receipt: str) -> None: ...
