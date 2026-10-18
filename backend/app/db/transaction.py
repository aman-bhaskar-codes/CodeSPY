from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import (
    DatabaseUnavailable,
    DuplicateConflict,
    InvalidTransition,
    ScopeViolation,
)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    try:
        async with factory() as session, session.begin():
            yield session
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None)
        error = {
            "23505": DuplicateConflict,
            "23503": ScopeViolation,
            "42501": ScopeViolation,
            "23514": InvalidTransition,
        }.get(state or "", DatabaseUnavailable)
        raise error("database operation rejected", sqlstate=state) from None
