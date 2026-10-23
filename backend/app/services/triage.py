"""Append-only triage event stream and optimistic projections service."""

import secrets
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, desc, insert, or_, select, update
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
from app.domain.errors import DuplicateConflict
from app.domain.scope import TenantContext
from app.models.scan_tables import (
    finding_occurrences,
    finding_triage_events,
    finding_triage_projections,
)
from app.repositories.command_repository import record_command, replay_command
from app.security.authorization import require_snapshot
from app.security.cursors import CursorCodec


class TriageService:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], cursors: CursorCodec | None = None
    ):
        self.factory = factory
        self.cursors = cursors or CursorCodec(secrets.token_bytes(32))

    async def get_record(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
    ) -> TriageRecordV1:
        async with transaction(self.factory) as session:
            await self._verify_finding_exists(session, ctx, repository_id, snapshot_id, finding_id)
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
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TriageHistoryV1:
        if not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        async with transaction(self.factory) as session:
            finding = await self._verify_finding_exists(
                session, ctx, repository_id, snapshot_id, finding_id
            )
            scope = {
                "kind": "triage_history",
                "org": str(ctx.org_id),
                "principal": str(ctx.principal_id),
                "repository": str(repository_id),
                "snapshot": str(snapshot_id),
                "run": str(finding["run_id"]),
                "finding": str(finding_id),
            }
            query = select(finding_triage_events).where(
                finding_triage_events.c.org_id == ctx.org_id,
                finding_triage_events.c.snapshot_id == snapshot_id,
                finding_triage_events.c.run_id == finding["run_id"],
                finding_triage_events.c.finding_id == finding_id,
            )
            if cursor is not None:
                try:
                    timestamp, event_id = self.cursors.decode(cursor, scope).split("|", 1)
                    position_time, position_id = datetime.fromisoformat(timestamp), UUID(event_id)
                except (ValueError, TypeError):
                    raise ValueError("invalid history cursor") from None
                query = query.where(
                    or_(
                        finding_triage_events.c.created_at < position_time,
                        and_(
                            finding_triage_events.c.created_at == position_time,
                            finding_triage_events.c.event_id < position_id,
                        ),
                    )
                )
            rows = (
                (
                    await session.execute(
                        query.order_by(
                            desc(finding_triage_events.c.created_at),
                            desc(finding_triage_events.c.event_id),
                        ).limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            selected = rows[:limit]
            events = [
                TriageHistoryItemV1(
                    event_id=r["event_id"],
                    finding_id=finding_id,
                    principal_id=r["principal_id"],
                    state=r["state"],
                    reason=r["reason"],
                    created_at=r["created_at"],
                )
                for r in selected
            ]
            next_cursor = None
            if len(rows) > limit:
                last = selected[-1]
                next_cursor = self.cursors.encode(
                    scope, f"{last['created_at'].isoformat()}|{last['event_id']}"
                )
            return TriageHistoryV1(finding_id=finding_id, events=events, next_cursor=next_cursor)

    async def transition(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
        transition: TriageTransitionV1,
        *,
        request_key: str | None = None,
    ) -> TriageRecordV1:
        transition = TriageTransitionV1.model_validate(transition.model_dump())
        if request_key is not None and not 1 <= len(request_key.encode()) <= 128:
            raise ValueError("bounded command key required")
        command = {
            "repository_id": str(repository_id),
            "snapshot_id": str(snapshot_id),
            "finding_id": str(finding_id),
            "transition": transition.model_dump(mode="json"),
        }
        async with transaction(self.factory) as session:
            finding = await self._verify_finding_exists(
                session, ctx, repository_id, snapshot_id, finding_id, write=True
            )
            run_id = finding["run_id"]
            if request_key is not None:
                replay = await replay_command(session, ctx, request_key, "finding_triage", command)
                if replay is not None:
                    return TriageRecordV1.model_validate(replay)

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

            # A no-op command still retains its original response for later replay.
            if transition.state == current_state and transition.reason == current_reason:
                record = TriageRecordV1(
                    finding_id=finding_id,
                    current_state=current_state,
                    current_reason=current_reason,
                    last_event_id=projection["last_event_id"] if projection else None,
                    updated_by=projection["updated_by"] if projection else None,
                    updated_at=projection["updated_at"] if projection else None,
                    version=current_version,
                )
                return await self._remember(session, ctx, request_key, command, record)

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

            record = TriageRecordV1(
                finding_id=finding_id,
                current_state=transition.state,
                current_reason=transition.reason,
                last_event_id=event_id,
                updated_by=ctx.principal_id,
                updated_at=now,
                version=new_version,
            )
            return await self._remember(session, ctx, request_key, command, record)

    async def _remember(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        request_key: str | None,
        command: dict[str, Any],
        record: TriageRecordV1,
    ) -> TriageRecordV1:
        if request_key is not None:
            await record_command(
                session, ctx, request_key, "finding_triage", command, record.model_dump(mode="json")
            )
        return record

    async def _verify_finding_exists(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
        *,
        write: bool = False,
    ) -> dict[str, Any]:
        await require_snapshot(session, ctx, repository_id, snapshot_id, write=write, lock=write)
        query = select(finding_occurrences).where(
            finding_occurrences.c.org_id == ctx.org_id,
            finding_occurrences.c.snapshot_id == snapshot_id,
            finding_occurrences.c.id == finding_id,
        )
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            raise NoResultFound("finding_not_found")
        return dict(row)
