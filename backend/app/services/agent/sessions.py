"""Agent session lifecycle: create (idempotent), read, state and event pages."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.agent_events import AgentEventV1
from app.contracts.agent_session import (
    DEFAULT_COST_MICROS_LIMIT,
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOOL_CALLS,
    AgentSessionCreateRequestV1,
    AgentSessionV1,
    AgentStateV1,
)
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.models.agent_tables import agent_sessions
from app.services.agent.event_log import READ_BATCH, AgentEventLog
from app.services.agent.state import reduce_events

MAX_PAGE = 500


def _view(row: Any) -> AgentSessionV1:
    created: datetime = row.created_at
    return AgentSessionV1(
        id=row.id,
        repository_id=row.repository_id,
        snapshot_id=row.base_snapshot_id,
        mode=row.mode,
        status=row.status,
        model_profile=row.model_profile,
        last_seq=int(row.last_seq),
        created_at=created.isoformat(),
    )


class AgentSessionService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], log: AgentEventLog) -> None:
        self.factory, self.log = factory, log

    async def create(
        self, ctx: TenantContext, request: AgentSessionCreateRequestV1, idempotency_key: str
    ) -> AgentSessionV1:
        digest = semantic_digest(request.model_dump(mode="json"))
        async with transaction(self.factory) as s:
            existing = (
                await s.execute(
                    select(agent_sessions).where(
                        agent_sessions.c.org_id == ctx.org_id,
                        agent_sessions.c.principal_id == ctx.principal_id,
                        agent_sessions.c.idempotency_key == idempotency_key,
                    )
                )
            ).one_or_none()
            if existing is not None:
                if existing.request_digest != digest:
                    raise DuplicateConflict("idempotency key reused with a different request")
                return _view(existing)
            session_id = uuid4()
            limits = {
                "max_steps": DEFAULT_MAX_STEPS,
                "max_tool_calls": DEFAULT_MAX_TOOL_CALLS,
                "cost_micros_limit": DEFAULT_COST_MICROS_LIMIT,
            }
            await s.execute(
                insert(agent_sessions).values(
                    org_id=ctx.org_id,
                    id=session_id,
                    repository_id=request.repository_id,
                    base_snapshot_id=request.snapshot_id,
                    principal_id=ctx.principal_id,
                    mode=request.mode,
                    model_profile=request.model_profile,
                    limits_json=limits,
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                )
            )
            await self.log.append(
                s,
                ctx.org_id,
                session_id,
                "session.created",
                {
                    "mode": request.mode,
                    "base_snapshot_id": str(request.snapshot_id),
                    "model_profile": request.model_profile,
                    "limits": limits,
                },
                event_key="session.created",
            )
            return await self._get(s, ctx, session_id)

    async def get(self, ctx: TenantContext, session_id: UUID) -> AgentSessionV1:
        async with transaction(self.factory) as s:
            return await self._get(s, ctx, session_id)

    async def _get(self, s: AsyncSession, ctx: TenantContext, session_id: UUID) -> AgentSessionV1:
        row = (
            await s.execute(
                select(agent_sessions).where(
                    agent_sessions.c.org_id == ctx.org_id,
                    agent_sessions.c.id == session_id,
                    agent_sessions.c.principal_id == ctx.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ScopeViolation("agent session unavailable")
        return _view(row)

    async def state(self, ctx: TenantContext, session_id: UUID) -> AgentStateV1:
        events: list[AgentEventV1] = []
        async with transaction(self.factory) as s:
            await self._get(s, ctx, session_id)
            after = 0
            while True:
                page = await self.log.read_after(s, ctx.org_id, session_id, after, READ_BATCH)
                events.extend(page)
                if len(page) < READ_BATCH:
                    break
                after = page[-1].seq
        return reduce_events(session_id, events)

    async def events(
        self, ctx: TenantContext, session_id: UUID, after: int, limit: int
    ) -> list[AgentEventV1]:
        async with transaction(self.factory) as s:
            await self._get(s, ctx, session_id)
            return await self.log.read_after(s, ctx.org_id, session_id, after, min(limit, MAX_PAGE))
