"""Approvals: a question to the person, parked in the database until they answer.

The loop asks by writing an approval row and an ``approval.requested`` event, then stops and gives
up its lease; nothing waits in memory. The person's answer is recorded here in one transaction with
its ``approval.resolved`` event, and the loop is started again. An approval that nobody answers by
its expiry is closed as expired and the tool call is treated as denied.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.transaction import transaction
from app.domain.errors import InvalidTransition, ScopeViolation
from app.models.agent_tables import agent_approvals, agent_sessions
from app.services.agent.event_log import AgentEventLog, format_ts

APPROVAL_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class ApprovalRow:
    approval_id: str
    subject_kind: str
    subject_ref: str
    status: str
    rule: str | None
    expired: bool


class ApprovalService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], log: AgentEventLog) -> None:
        self.factory, self.log = factory, log

    async def request(
        self,
        org: UUID,
        session: UUID,
        approval_id: str,
        kind: str,
        ref: str,
        *,
        reason: str,
        rule_matched: str | None = None,
        turn: int = 0,
        ttl: timedelta = APPROVAL_TTL,
    ) -> ApprovalRow:
        """Ask once. Asking again for the same approval returns the first question."""
        async with transaction(self.factory) as s:
            existing = await self._row(s, org, session, approval_id)
            if existing is not None:
                return existing
            expires = datetime.now(UTC) + ttl
            event = await self.log.append(
                s,
                org,
                session,
                "approval.requested",
                {
                    "approval_id": approval_id,
                    "subject": {"kind": kind, "ref": ref},
                    "reason": reason[:512],
                    **({"rule_matched": rule_matched} if rule_matched else {}),
                    "expires_at": format_ts(expires),
                },
                turn=turn,
                event_key=f"approval.requested.{approval_id}",
            )
            await s.execute(
                insert(agent_approvals).values(
                    org_id=org,
                    session_id=session,
                    approval_id=approval_id,
                    subject_kind=kind,
                    subject_ref=ref,
                    reason=reason[:512],
                    requested_seq=event.seq,
                    expires_at=expires,
                )
            )
            row = await self._row(s, org, session, approval_id)
            assert row is not None
            return row

    async def get(self, org: UUID, session: UUID, approval_id: str) -> ApprovalRow | None:
        async with transaction(self.factory) as s:
            return await self._row(s, org, session, approval_id)

    async def resolve(
        self,
        org: UUID,
        session: UUID,
        approval_id: str,
        *,
        decision: str,
        by: UUID,
        scope: str = "once",
        rule: str | None = None,
        turn: int = 0,
    ) -> ApprovalRow:
        """Record the person's answer. Answering twice, or after expiry, is refused."""
        async with transaction(self.factory) as s:
            row = await self._row(s, org, session, approval_id, lock=True)
            if row is None:
                raise ScopeViolation("approval unavailable")
            if row.status != "pending":
                raise InvalidTransition("this approval was already answered")
            if row.expired:
                await self._close(s, org, session, approval_id, "expired", None, "system", turn)
                raise InvalidTransition("this approval expired")
            stored_rule = rule if decision == "approved" and scope == "session_rule" else None
            await s.execute(
                update(agent_approvals)
                .where(
                    agent_approvals.c.org_id == org,
                    agent_approvals.c.session_id == session,
                    agent_approvals.c.approval_id == approval_id,
                )
                .values(
                    status=decision,
                    decided_by=by,
                    decided_at=func.clock_timestamp(),
                    rule_json={"pattern": stored_rule} if stored_rule else None,
                )
            )
            await self.log.append(
                s,
                org,
                session,
                "approval.resolved",
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "scope": "session_rule" if stored_rule else "once",
                    **({"rule": stored_rule} if stored_rule else {}),
                    "by": str(by),
                },
                turn=turn,
                event_key=f"approval.resolved.{approval_id}",
            )
            closed = await self._row(s, org, session, approval_id)
            assert closed is not None
            return closed

    async def expire_if_due(
        self, org: UUID, session: UUID, approval_id: str, turn: int = 0
    ) -> ApprovalRow:
        """Close an unanswered approval whose time is up, so its tool call can be denied."""
        async with transaction(self.factory) as s:
            row = await self._row(s, org, session, approval_id, lock=True)
            if row is None:
                raise ScopeViolation("approval unavailable")
            if row.status == "pending" and row.expired:
                await self._close(s, org, session, approval_id, "expired", None, "system", turn)
                row = await self._row(s, org, session, approval_id)
                assert row is not None
            return row

    async def session_rules(self, org: UUID, session: UUID) -> tuple[str, ...]:
        """Patterns the person approved for the rest of this session."""
        async with transaction(self.factory) as s:
            rows = (
                (
                    await s.execute(
                        select(agent_approvals.c.rule_json).where(
                            agent_approvals.c.org_id == org,
                            agent_approvals.c.session_id == session,
                            agent_approvals.c.status == "approved",
                            agent_approvals.c.rule_json.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return tuple(dict.fromkeys(r["pattern"] for r in rows if r and r.get("pattern")))

    async def pending(self, org: UUID, session: UUID) -> list[ApprovalRow]:
        async with transaction(self.factory) as s:
            ids = (
                (
                    await s.execute(
                        select(agent_approvals.c.approval_id).where(
                            agent_approvals.c.org_id == org,
                            agent_approvals.c.session_id == session,
                            agent_approvals.c.status == "pending",
                        )
                    )
                )
                .scalars()
                .all()
            )
            rows = [await self._row(s, org, session, i) for i in ids]
        return [r for r in rows if r is not None]

    async def _close(
        self,
        s: AsyncSession,
        org: UUID,
        session: UUID,
        approval_id: str,
        decision: str,
        rule: str | None,
        by: str,
        turn: int,
    ) -> None:
        await s.execute(
            update(agent_approvals)
            .where(
                agent_approvals.c.org_id == org,
                agent_approvals.c.session_id == session,
                agent_approvals.c.approval_id == approval_id,
            )
            .values(status=decision, decided_at=func.clock_timestamp())
        )
        payload: dict[str, Any] = {
            "approval_id": approval_id,
            "decision": decision,
            "scope": "once",
            "by": by,
        }
        await self.log.append(
            s,
            org,
            session,
            "approval.resolved",
            payload,
            turn=turn,
            event_key=f"approval.resolved.{approval_id}",
        )

    async def _row(
        self, s: AsyncSession, org: UUID, session: UUID, approval_id: str, *, lock: bool = False
    ) -> ApprovalRow | None:
        query = select(
            agent_approvals.c.approval_id,
            agent_approvals.c.subject_kind,
            agent_approvals.c.subject_ref,
            agent_approvals.c.status,
            agent_approvals.c.rule_json,
            (agent_approvals.c.expires_at < func.clock_timestamp()).label("expired"),
        ).where(
            agent_approvals.c.org_id == org,
            agent_approvals.c.session_id == session,
            agent_approvals.c.approval_id == approval_id,
        )
        if lock:
            query = query.with_for_update()
        row = (await s.execute(query)).first()
        if row is None:
            return None
        rule = (row.rule_json or {}).get("pattern") if row.rule_json else None
        return ApprovalRow(
            row.approval_id, row.subject_kind, row.subject_ref, row.status, rule, bool(row.expired)
        )


__all__ = ["APPROVAL_TTL", "ApprovalRow", "ApprovalService", "agent_sessions"]
