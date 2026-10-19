"""Database authority time; deliberately independent of injected scheduling clocks."""

from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def database_now(s: AsyncSession) -> datetime:
    return cast(datetime, (await s.execute(select(func.clock_timestamp()))).scalar_one())
