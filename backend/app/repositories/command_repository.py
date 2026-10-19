"""Command responses share the transaction and organization lock of their effect."""

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict
from app.domain.scope import TenantContext
from app.models.phase2_tables import PHASE2_TABLES

commands = PHASE2_TABLES["api_commands"]


async def replay_command(
    session: AsyncSession, ctx: TenantContext, key: str, operation: str, request: dict[str, Any]
) -> dict[str, Any] | None:
    if not 1 <= len(key.encode()) <= 128:
        raise ValueError("bounded command key required")
    row = (
        (
            await session.execute(
                select(commands).where(
                    commands.c.org_id == ctx.org_id,
                    commands.c.principal_id == ctx.principal_id,
                    commands.c.request_key == key,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["operation"] != operation or row["request_hash"] != semantic_digest(request):
        raise DuplicateConflict("command key has different input")
    return dict(row["response"])


async def record_command(
    session: AsyncSession,
    ctx: TenantContext,
    key: str,
    operation: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> None:
    await session.execute(
        insert(commands).values(
            org_id=ctx.org_id,
            principal_id=ctx.principal_id,
            request_key=key,
            operation=operation,
            request_hash=semantic_digest(request),
            response=response,
        )
    )
