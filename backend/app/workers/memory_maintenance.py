"""Bounded retention through the existing worker maintenance loop."""

from app.db.transaction import transaction
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class MemoryMaintenanceWorker:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def tick(self) -> int:
        async with transaction(self.factory) as session:
            if session.bind is None or session.bind.dialect.name != "postgresql":
                return 0
            installed = await session.scalar(
                text("SELECT to_regprocedure('public.codespy_purge_memories(integer)')")
            )
            if installed is None:
                return 0
            count = await session.scalar(text("SELECT public.codespy_purge_memories(100)"))
            return int(count or 0)
