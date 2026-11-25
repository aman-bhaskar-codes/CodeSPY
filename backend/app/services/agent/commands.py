"""What a person can tell a session: message, approve, interrupt, cancel, change mode.

Each command is checked against who owns the session and what state it is in, recorded as an event
in the same transaction as its effect, and then the loop is scheduled to continue. The loop itself
never waits for a person: these commands are how a parked session wakes.
"""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.agent_events import AgentEventV1
from app.db.transaction import transaction
from app.domain.errors import InvalidTransition, ScopeViolation
from app.domain.scope import TenantContext
from app.models.agent_tables import agent_events, agent_sessions
from app.services.agent.approvals import ApprovalRow, ApprovalService
from app.services.agent.control import FINISHED, SessionControl, SessionRow
from app.services.agent.event_log import AgentEventLog
from app.services.agent.runner import SessionRunner

Scheduler = Callable[[TenantContext, UUID], Awaitable[None]]
ACCEPTS_MESSAGES = frozenset({"idle", "interrupted"})


class AgentCommands:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        log: AgentEventLog,
        control: SessionControl,
        approvals: ApprovalService,
        runner: SessionRunner,
        schedule: Scheduler,
    ) -> None:
        self.factory, self.log, self.control = factory, log, control
        self.approvals, self.runner, self.schedule = approvals, runner, schedule

    async def _own(self, ctx: TenantContext, session_id: UUID) -> SessionRow:
        row = await self.control.load(ctx.org_id, session_id)
        if row.principal_id != ctx.principal_id:
            raise ScopeViolation("agent session unavailable")
        return row

    async def send_message(
        self,
        ctx: TenantContext,
        session_id: UUID,
        text: str,
        context: list[dict[str, Any]] | None = None,
        *,
        request_key: str,
    ) -> AgentEventV1:
        """Start a turn. Sending the same request key again returns the same message."""
        row = await self._own(ctx, session_id)
        message_id = "u" + hashlib.sha256(f"{session_id}:{request_key}".encode()).hexdigest()[:16]
        async with transaction(self.factory) as s:
            known = await s.scalar(
                select(agent_events.c.seq).where(
                    agent_events.c.org_id == ctx.org_id,
                    agent_events.c.session_id == session_id,
                    agent_events.c.event_key == f"message.user.{message_id}",
                )
            )
        if known is None:
            if row.status in FINISHED:
                raise InvalidTransition("this session has ended")
            if row.status not in ACCEPTS_MESSAGES:
                raise InvalidTransition("the agent is working or waiting for an answer")
        turn = len(await self._user_messages(ctx, session_id)) + (0 if known else 1)
        async with transaction(self.factory) as s:
            event = await self.log.append(
                s,
                ctx.org_id,
                session_id,
                "message.user",
                {"message_id": message_id, "text": text, "context": context or []},
                turn=turn,
                event_key=f"message.user.{message_id}",
            )
        if known is None:
            await self.schedule(ctx, session_id)
        return event

    async def _user_messages(self, ctx: TenantContext, session_id: UUID) -> list[AgentEventV1]:
        out: list[AgentEventV1] = []
        after = 0
        async with transaction(self.factory) as s:
            while True:
                page = await self.log.read_after(s, ctx.org_id, session_id, after, 200)
                out.extend(e for e in page if e.type == "message.user")
                if len(page) < 200:
                    return out
                after = page[-1].seq

    async def resolve_approval(
        self,
        ctx: TenantContext,
        session_id: UUID,
        approval_id: str,
        decision: str,
        *,
        scope: str = "once",
        rule: str | None = None,
    ) -> ApprovalRow:
        await self._own(ctx, session_id)
        row = await self.approvals.resolve(
            ctx.org_id,
            session_id,
            approval_id,
            decision=decision,
            by=ctx.principal_id,
            scope=scope,
            rule=rule,
        )
        await self.schedule(ctx, session_id)
        return row

    async def interrupt(self, ctx: TenantContext, session_id: UUID) -> None:
        """Stop after the current step. A session waiting on a person simply pauses."""
        row = await self._own(ctx, session_id)
        if row.status in FINISHED:
            raise InvalidTransition("this session has ended")
        await self.control.request(ctx.org_id, session_id, "interrupt")
        if await self.control.acquire(ctx.org_id, session_id):  # nobody is running it
            try:
                await self.control.clear_interrupt(ctx.org_id, session_id)
                await self.control.set_status(
                    ctx.org_id, session_id, "interrupted", reason="interrupted by the user"
                )
            finally:
                await self.control.release(ctx.org_id, session_id)

    async def cancel(self, ctx: TenantContext, session_id: UUID) -> None:
        await self._own(ctx, session_id)
        await self.runner.cancel(ctx, session_id)

    async def set_mode(self, ctx: TenantContext, session_id: UUID, mode: str) -> None:
        row = await self._own(ctx, session_id)
        if row.status not in ACCEPTS_MESSAGES:
            raise InvalidTransition("the mode changes only between turns")
        if mode == row.mode:
            return
        async with transaction(self.factory) as s:
            await s.execute(
                update(agent_sessions)
                .where(agent_sessions.c.org_id == ctx.org_id, agent_sessions.c.id == session_id)
                .values(mode=mode)
            )
            await self.log.append(
                s, ctx.org_id, session_id, "session.mode.changed", {"mode": mode, "by": "user"}
            )
