"""Deterministic fixture transport. Durable effects live in PostgreSQL."""

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

from app.ports.queue import Delivery


@dataclass
class Message:
    body: bytes
    receipt: str = ""
    visible_at: float = 0


class MemoryQueue:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.lock = asyncio.Lock()
        self.available = asyncio.Event()

    async def send(self, body: bytes) -> None:
        if not body or len(body) > 4096:
            raise ValueError("bounded queue message required")
        async with self.lock:
            self.messages.append(Message(body))
            self.available.set()

    async def receive(self, max_messages: int = 1, *, wait_seconds: int = 20) -> list[Delivery]:
        if not 1 <= max_messages <= 2 or not 0 <= wait_seconds <= 20:
            raise ValueError("invalid receive bounds")
        async with self.lock:
            now = time.monotonic()
            result = []
            for message in self.messages:
                if message.visible_at <= now:
                    message.receipt = uuid4().hex
                    message.visible_at = now + 120
                    result.append(Delivery(message.body, message.receipt))
                    if len(result) == max_messages:
                        break
            self.available.clear()
        if result or wait_seconds == 0:
            return result
        try:
            async with asyncio.timeout(wait_seconds):
                await self.available.wait()
        except TimeoutError:
            return []
        return await self.receive(max_messages, wait_seconds=0)

    async def extend(self, receipt: str, seconds: int) -> None:
        if not 0 <= seconds <= 120:
            raise ValueError("invalid visibility bound")
        async with self.lock:
            for message in self.messages:
                if message.receipt == receipt:
                    message.visible_at = time.monotonic() + seconds
                    return

    async def ack(self, receipt: str) -> None:
        async with self.lock:
            self.messages[:] = [m for m in self.messages if m.receipt != receipt]
