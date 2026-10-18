"""One monotonic forward-work deadline; rollback has a separate allowance."""

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Protocol


class FinalizationBudget:
    def __init__(self, seconds: float = 60):
        if seconds <= 0:
            raise ValueError("positive budget required")
        self.seconds = seconds
        self.deadline = 0.0
        self.timeout: asyncio.Timeout | None = None

    async def __aenter__(self) -> "FinalizationBudget":
        self.deadline = asyncio.get_running_loop().time() + self.seconds
        self.timeout = asyncio.timeout_at(self.deadline)
        await self.timeout.__aenter__()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self.timeout is not None
        await self.timeout.__aexit__(kind, error, tb)

    def remaining(self) -> float:
        return max(0, self.deadline - asyncio.get_running_loop().time())


class RecoverableSession(Protocol):
    async def rollback(self) -> None: ...
    async def close(self) -> None: ...
    async def invalidate(self) -> None: ...


async def rollback_or_invalidate(
    session: RecoverableSession,
    seconds: float = 5,
    *,
    force_terminate: Callable[[], None] | None = None,
) -> None:
    """Bound rollback and invalidation together; never detach owned cleanup.

    The finalizer must supply the held asyncpg connection's synchronous
    terminate method for a hard transport fallback. Without it a cleanup
    failure propagates as unconfirmed recovery, never as successful rollback.
    """
    if seconds <= 0:
        raise ValueError("positive cleanup budget required")

    async def recover() -> None:
        try:
            async with asyncio.timeout(seconds):
                try:
                    # Reserve time within C for invalidation rather than adding
                    # an unbounded invalidate after a full-length rollback.
                    async with asyncio.timeout(seconds * 0.6):
                        await session.rollback()
                        await session.close()
                except Exception:
                    await session.invalidate()
        except BaseException:
            if force_terminate is not None:
                force_terminate()
            raise

    task = asyncio.create_task(recover())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError
