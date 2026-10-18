from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class DatabaseCapabilities:
    server_version: str
    extensions: dict[str, str]
    ready: bool


async def inspect_database(engine: AsyncEngine) -> DatabaseCapabilities:
    async with engine.connect() as conn:
        version = str(await conn.scalar(text("SHOW server_version")))
        extensions = dict(
            (await conn.execute(text("SELECT extname,extversion FROM pg_extension"))).tuples().all()
        )
        return DatabaseCapabilities(
            version, extensions, {"vector", "pg_trgm", "citext", "uuid-ossp"} <= extensions.keys()
        )
