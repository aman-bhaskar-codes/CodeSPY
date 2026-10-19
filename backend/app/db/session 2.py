from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.database_config import DatabaseSettings


def build_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.async_url,
        echo=False,
        hide_parameters=True,
        isolation_level="READ COMMITTED",
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.DB_CONNECT_TIMEOUT_SECONDS,
            "server_settings": {
                "timezone": "UTC",
                "application_name": "codespy",
                "statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS),
                "lock_timeout": str(settings.DB_LOCK_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": str(settings.DB_IDLE_TRANSACTION_TIMEOUT_MS),
            },
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
