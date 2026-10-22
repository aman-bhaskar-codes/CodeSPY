"""Append-only triage event stream and optimistic projections service."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import desc, insert, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.triage import (
    TriageHistoryItemV1,
    TriageHistoryV1,
    TriageRecordV1,
    TriageTransitionV1,
)
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, PermissionDenied
from app.domain.scope import TenantContext
from app.models.scan_tables import (
    finding_occurrences,
    finding_triage_events,
    finding_triage_projections,
)


class TriageService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def get_record(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
    ) -> TriageRecordV1:
        async with transaction(self.factory) as session:
            await self._verify_finding_exists(session, ctx, snapshot_id, finding_id)
            row = (
                (
                    await session.execute(
                        select(finding_triage_projections).where(
                            finding_triage_projections.c.org_id == ctx.org_id,
                            finding_triage_projections.c.snapshot_id == snapshot_id,
                            finding_triage_projections.c.finding_id == finding_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return TriageRecordV1(
                    finding_id=finding_id,
                    current_state="unreviewed",
                    current_reason="none",
                    version=0,
                )
            return TriageRecordV1(
                finding_id=finding_id,
                current_state=row["current_state"],
                current_reason=row["current_reason"],
                last_event_id=row["last_event_id"],
                updated_by=row["updated_by"],
                updated_at=row["updated_at"],
                version=row["version"],
            )

    async def get_history(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
    ) -> TriageHistoryV1:
        async with transaction(self.factory) as session:
            await self._verify_finding_exists(session, ctx, snapshot_id, finding_id)
            rows = (
                (
                    await session.execute(
                        select(finding_triage_events)
                        .where(
                            finding_triage_events.c.org_id == ctx.org_id,
                            finding_triage_events.c.snapshot_id == snapshot_id,
                            finding_triage_events.c.finding_id == finding_id,
                        )
                        .order_by(
                            desc(finding_triage_events.c.created_at),
                            desc(finding_triage_events.c.event_id),
                        )
                    )
                )
                .mappings()
                .all()
            )
            events = [
                TriageHistoryItemV1(
                    event_id=r["event_id"],
                    finding_id=finding_id,
                    principal_id=r["principal_id"],
                    state=r["state"],
                    reason=r["reason"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
            return TriageHistoryV1(finding_id=finding_id, events=events)

    async def transition(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
        transition: TriageTransitionV1,
    ) -> TriageRecordV1:
        if ctx.role not in {"owner", "admin", "member"}:
            raise PermissionDenied("triage_requires_member_role")

        async with transaction(self.factory) as session:
            finding = await self._verify_finding_exists(session, ctx, snapshot_id, finding_id)
            run_id = finding["run_id"]

            projection = (
                (
                    await session.execute(
                        select(finding_triage_projections)
                        .where(
                            finding_triage_projections.c.org_id == ctx.org_id,
                            finding_triage_projections.c.snapshot_id == snapshot_id,
                            finding_triage_projections.c.run_id == run_id,
                            finding_triage_projections.c.finding_id == finding_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )

            current_version = projection["version"] if projection is not None else 0
            current_state = projection["current_state"] if projection is not None else "unreviewed"
            current_reason = projection["current_reason"] if projection is not None else "none"

            if transition.expected_version != current_version:
                raise DuplicateConflict(
                    f"triage version conflict: expected {transition.expected_version}, "
                    f"current {current_version}"
                )

            # Idempotent replay: if state and reason match exactly, return existing record
            if transition.state == current_state and transition.reason == current_reason:
                return TriageRecordV1(
                    finding_id=finding_id,
                    current_state=current_state,
                    current_reason=current_reason,
                    last_event_id=projection["last_event_id"] if projection else None,
                    updated_by=projection["updated_by"] if projection else None,
                    updated_at=projection["updated_at"] if projection else None,
                    version=current_version,
                )

            event_id = uuid4()
            now = await database_now(session)

            # Insert immutable audit event
            await session.execute(
                insert(finding_triage_events).values(
                    org_id=ctx.org_id,
                    snapshot_id=snapshot_id,
                    run_id=run_id,
                    finding_id=finding_id,
                    event_id=event_id,
                    principal_id=ctx.principal_id,
                    state=transition.state,
                    reason=transition.reason,
                    created_at=now,
                )
            )

            # Optimistic projection upsert / conditional update
            if current_version == 0:
                await session.execute(
                    insert(finding_triage_projections).values(
                        org_id=ctx.org_id,
                        snapshot_id=snapshot_id,
                        run_id=run_id,
                        finding_id=finding_id,
                        current_state=transition.state,
                        current_reason=transition.reason,
                        last_event_id=event_id,
                        updated_by=ctx.principal_id,
                        updated_at=now,
                        version=1,
                    )
                )
                new_version = 1
            else:
                res = await session.execute(
                    update(finding_triage_projections)
                    .where(
                        finding_triage_projections.c.org_id == ctx.org_id,
                        finding_triage_projections.c.snapshot_id == snapshot_id,
                        finding_triage_projections.c.run_id == run_id,
                        finding_triage_projections.c.finding_id == finding_id,
                        finding_triage_projections.c.version == current_version,
                    )
                    .values(
                        current_state=transition.state,
                        current_reason=transition.reason,
                        last_event_id=event_id,
                        updated_by=ctx.principal_id,
                        updated_at=now,
                        version=finding_triage_projections.c.version + 1,
                    )
                )
                rowcount = getattr(res, "rowcount", None)
                if rowcount != 1:
                    raise DuplicateConflict("concurrent triage modification")
                new_version = current_version + 1

            return TriageRecordV1(
                finding_id=finding_id,
                current_state=transition.state,
                current_reason=transition.reason,
                last_event_id=event_id,
                updated_by=ctx.principal_id,
                updated_at=now,
                version=new_version,
            )

    async def _verify_finding_exists(
        self, session: AsyncSession, ctx: TenantContext, snapshot_id: UUID, finding_id: UUID
    ) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    select(finding_occurrences).where(
                        finding_occurrences.c.org_id == ctx.org_id,
                        finding_occurrences.c.snapshot_id == snapshot_id,
                        finding_occurrences.c.id == finding_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NoResultFound("finding_not_found")
        return dict(row)
