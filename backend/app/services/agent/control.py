"""One loop per session, and the controls a person has over it: interrupt, cancel and status.

The lease keeps two workers from driving the same session. It is a row lock with an expiry, so a
worker that dies frees the session when its lease runs out. The control flag is read by the loop
between steps: an interrupt stops after the current step, a cancel ends the session for good.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.transaction import transaction
from app.domain.errors import ScopeViolation
from app.models.agent_tables import agent_sessions
from app.services.agent.event_log import AgentEventLog

FINISHED = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class SessionRow:
    org_id: UUID
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    principal_id: UUID
    mode: str
    status: str
    model_profile: str
    limits: dict[str, Any]
    control: str | None
    last_seq: int


class SessionControl:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        log: AgentEventLog,
        owner: str,
        *,
        lease_seconds: int = 60,
    ) -> None:
        self.factory, self.log, self.owner, self.lease_seconds = factory, log, owner, lease_seconds

    async def load(self, org: UUID, session: UUID) -> SessionRow:
        async with transaction(self.factory) as s:
            row = (
                await s.execute(
                    select(agent_sessions).where(
                        agent_sessions.c.org_id == org, agent_sessions.c.id == session
                    )
                )
            ).one_or_none()
        if row is None:
            raise ScopeViolation("agent session unavailable")
        return SessionRow(
            row.org_id,
            row.id,
            row.repository_id,
            row.base_snapshot_id,
            row.principal_id,
            row.mode,
            row.status,
            row.model_profile,
            dict(row.limits_json),
            row.control,
            int(row.last_seq),
        )

    async def acquire(self, org: UUID, session: UUID) -> bool:
        """Take the session's lease if it is free, expired or already ours."""
        async with transaction(self.factory) as s:
            result = await s.execute(
                update(agent_sessions)
                .where(
                    agent_sessions.c.org_id == org,
                    agent_sessions.c.id == session,
                    (agent_sessions.c.lease_until.is_(None))
                    | (agent_sessions.c.lease_until < func.clock_timestamp())
                    | (agent_sessions.c.lease_owner == self.owner),
                )
                .values(
                    lease_owner=self.owner,
                    lease_until=func.clock_timestamp()
                    + text(f"interval '{int(self.lease_seconds)} seconds'"),
                )
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def renew(self, org: UUID, session: UUID) -> bool:
        """Extend our lease; ``False`` means it was lost and the loop must stop."""
        async with transaction(self.factory) as s:
            result = await s.execute(
                update(agent_sessions)
                .where(
                    agent_sessions.c.org_id == org,
                    agent_sessions.c.id == session,
                    agent_sessions.c.lease_owner == self.owner,
                )
                .values(
                    lease_until=func.clock_timestamp()
                    + text(f"interval '{int(self.lease_seconds)} seconds'")
                )
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def release(self, org: UUID, session: UUID) -> None:
        async with transaction(self.factory) as s:
            await s.execute(
                update(agent_sessions)
                .where(
                    agent_sessions.c.org_id == org,
                    agent_sessions.c.id == session,
                    agent_sessions.c.lease_owner == self.owner,
                )
                .values(lease_owner=None, lease_until=None)
            )

    async def request(self, org: UUID, session: UUID, kind: str) -> None:
        """Ask the loop to stop. A cancel is final and is never replaced by an interrupt."""
        async with transaction(self.factory) as s:
            statement = update(agent_sessions).where(
                agent_sessions.c.org_id == org, agent_sessions.c.id == session
            )
            if kind == "interrupt":
                statement = statement.where(agent_sessions.c.control.is_(None))
            await s.execute(statement.values(control=kind))

    async def clear_interrupt(self, org: UUID, session: UUID) -> None:
        async with transaction(self.factory) as s:
            await s.execute(
                update(agent_sessions)
                .where(
                    agent_sessions.c.org_id == org,
                    agent_sessions.c.id == session,
                    agent_sessions.c.control == "interrupt",
                )
                .values(control=None)
            )

    async def set_status(
        self,
        org: UUID,
        session: UUID,
        status: str,
        *,
        key: str | None = None,
        turn: int = 0,
        reason: str | None = None,
    ) -> None:
        """Change the status and announce it, together. Repeating with the same key is safe."""
        async with transaction(self.factory) as s:
            current = await s.scalar(
                select(agent_sessions.c.status).where(
                    agent_sessions.c.org_id == org, agent_sessions.c.id == session
                )
            )
            if current is None:
                raise ScopeViolation("agent session unavailable")
            if current == status or current in FINISHED:
                return
            await s.execute(
                update(agent_sessions)
                .where(agent_sessions.c.org_id == org, agent_sessions.c.id == session)
                .values(status=status)
            )
            payload: dict[str, Any] = {"status": status}
            if reason:
                payload["reason"] = reason
            await self.log.append(
                s, org, session, "session.status", payload, turn=turn, event_key=key
            )
