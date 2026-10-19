"""Shared publication executor with transaction budgeting,
forced termination, and post-commit recovery.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import FinalizationOutcomeUnknown
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate

T = TypeVar("T")


class PublicationExecutor:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        budget_seconds: float = 60,
        safety_seconds: float = 5,
        cleanup_seconds: float = 5,
        commit_hook: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ):
        if not 0 < budget_seconds <= 60 or not 0 < cleanup_seconds <= safety_seconds <= 5:
            raise ValueError("invalid publication profile")
        self.factory = factory
        self.budget_seconds = budget_seconds
        self.safety_seconds = safety_seconds
        self.cleanup_seconds = cleanup_seconds
        self.commit_hook = commit_hook

    async def execute(
        self,
        work: Callable[[AsyncSession, FinalizationBudget], Awaitable[T]],
        resolve: Callable[[], Awaitable[T]],
    ) -> T:
        session: AsyncSession | None = None
        terminate: Callable[[], None] | None = None
        commit_started = False
        try:
            async with FinalizationBudget(self.budget_seconds) as budget:
                session = self.factory()
                await session.begin()
                conn = await session.connection()
                raw = await conn.get_raw_connection()
                driver = raw.driver_connection
                assert driver is not None
                terminate_driver: Callable[[], None] = driver.terminate

                def discard() -> None:
                    if raw.is_valid and raw.driver_connection is driver:
                        terminate_driver()
                        raw.invalidate(soft=True)
                        raw.detach()

                terminate = discard
                await session.execute(text("SET LOCAL statement_timeout='20s'"))
                await session.execute(text("SET LOCAL lock_timeout='2s'"))
                await session.execute(text("SET LOCAL idle_in_transaction_session_timeout='90s'"))
                result = await work(session, budget)
                commit_started = True
                if self.commit_hook is not None:
                    await self.commit_hook(session)
                else:
                    await session.commit()
        except BaseException as error:
            if session is not None:
                try:
                    await rollback_or_invalidate(
                        session, self.cleanup_seconds, force_terminate=terminate
                    )
                except BaseException:
                    if isinstance(error, asyncio.CancelledError):
                        raise error from None
                    if not commit_started:
                        raise FinalizationOutcomeUnknown(
                            "connection recovery unconfirmed"
                        ) from None
            if isinstance(error, asyncio.CancelledError):
                raise
            if commit_started:
                return await resolve()
            raise
        else:
            assert session is not None
            await rollback_or_invalidate(session, self.cleanup_seconds, force_terminate=terminate)
            return result
