"""Turns a command's output chunks into the few, ordered events a client can show live.

Per the protocol, output is coalesced: a batch is sent when 1 KiB has gathered, when 200 ms have
passed since the last one, or when the stream switches between stdout and stderr. Each batch carries
the UTF-16 offset of its first character within its stream, so a duplicated or replayed batch is
harmless. Live output is capped; the result the model sees always has the head and the tail.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

FLUSH_BYTES = 1024
FLUSH_SECONDS = 0.2
MAX_EVENT_CHARS = 4096
MAX_LIVE_BYTES = 256 * 1024
TRUNCATED_NOTICE = "\n[live output stopped here; the full result follows]\n"


@dataclass(frozen=True)
class OutputBatch:
    stream: str
    offset: int
    text: str


OutputSink = Callable[[OutputBatch], Awaitable[None]]


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class OutputCoalescer:
    def __init__(
        self,
        sink: OutputSink,
        *,
        clock: Callable[[], float] = time.monotonic,
        flush_bytes: int = FLUSH_BYTES,
        flush_seconds: float = FLUSH_SECONDS,
        max_live_bytes: int = MAX_LIVE_BYTES,
    ) -> None:
        self._sink, self._clock = sink, clock
        self._flush_bytes, self._flush_seconds, self._max_live = (
            flush_bytes,
            flush_seconds,
            max_live_bytes,
        )
        self._stream: str | None = None
        self._parts: list[str] = []
        self._size = 0
        self._offsets: dict[str, int] = {}
        self._last = clock()
        self._live = 0
        self._stopped = False

    async def feed(self, stream: str, text: str) -> None:
        if not text or self._stopped:
            return
        if self._stream is not None and stream != self._stream:
            await self.flush()
        self._stream = stream
        self._live += len(text.encode())
        if self._live > self._max_live:
            await self.flush()
            await self._emit(stream, TRUNCATED_NOTICE)
            self._stopped = True
            return
        self._parts.append(text)
        self._size += len(text.encode())
        if self._size >= self._flush_bytes or self._clock() - self._last >= self._flush_seconds:
            await self.flush()

    async def flush_due(self) -> None:
        """Send what has waited long enough. A runner calls this on a timer for quiet commands."""
        if self._parts and self._clock() - self._last >= self._flush_seconds:
            await self.flush()

    async def flush(self) -> None:
        if not self._parts or self._stream is None:
            return
        text, stream = "".join(self._parts), self._stream
        self._parts, self._size = [], 0
        await self._emit(stream, text)

    async def close(self) -> None:
        await self.flush()

    async def _emit(self, stream: str, text: str) -> None:
        for start in range(0, len(text), MAX_EVENT_CHARS):
            piece = text[start : start + MAX_EVENT_CHARS]
            offset = self._offsets.get(stream, 0)
            await self._sink(OutputBatch(stream, offset, piece))
            self._offsets[stream] = offset + _utf16(piece)
        self._last = self._clock()
