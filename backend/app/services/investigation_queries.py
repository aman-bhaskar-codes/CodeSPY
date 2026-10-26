"""Query and projection service for investigation detail, list, and events."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, overload
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation_jobs import (
    InvestigationDetailResponseV2,
    InvestigationEventPageV1,
    InvestigationEventV2,
    InvestigationJobRecordV1,
    InvestigationListResponseV2,
    InvestigationSummaryV2,
)
from app.domain.errors import ScopeViolation
from app.domain.scope import TenantContext
from app.models.investigation_execution_tables import (
    investigation_evidence,
    investigation_jobs,
    investigation_reports,
)
from app.models.investigation_tables import (
    investigation_events,
    investigations,
)
from app.models.tables import source_snapshots
from app.security.authorization import require_repository
from app.security.cursors import CursorCodec


@overload
def _ensure_utc(dt: None) -> None: ...


@overload
def _ensure_utc(dt: datetime) -> datetime: ...


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _row_to_summary(row: dict[str, Any] | sa.RowMapping) -> InvestigationSummaryV2:
    return InvestigationSummaryV2(
        id=row["id"],
        org_id=row["org_id"],
        repository_id=row["repository_id"],
        snapshot_id=row.get("snapshot_id"),
        finding_id=row.get("finding_id"),
        target_type=row["target_type"],
        query=row["query"],
        status=row["status"],
        mode=row["mode"],
        depth=row["depth"],
        current_step=row["current_step"],
        max_steps=row["max_steps"],
        budget_limit_micros=row["budget_limit_micros"],
        spent_micros=row["spent_micros"],
        held_micros=0,
        verdict=row.get("verdict"),
        verdict_confidence=row.get("verdict_confidence"),
        summary=row.get("summary"),
        failure_reason=row.get("failure_reason"),
        spec_hash=row.get("spec_hash"),
        current_job_id=row.get("current_job_id"),
        report_id=row.get("final_report_id"),
        execution_revision=row.get("execution_revision", 1),
        created_at=_ensure_utc(row["created_at"]),
        updated_at=_ensure_utc(row.get("updated_at") or row["created_at"]),
    )


class InvestigationQueryService:
    """Safe read projections for investigations, durable events, and final reports."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        cursors: CursorCodec | None = None,
    ) -> None:
        self.factory = factory
        self.cursors = cursors or CursorCodec(secrets.token_bytes(32))

    async def detail(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        investigation_id: UUID,
    ) -> InvestigationDetailResponseV2:
        """Fetches investigation detail with job, report, and safe action availability."""
        async with self.factory() as session:
            # 1. Require repository read authorization
            await require_repository(
                session, ctx.org_id, ctx.principal_id, repository_id, write=False
            )

            # 2. Fetch investigation row
            inv_stmt = select(investigations).where(
                investigations.c.org_id == ctx.org_id,
                investigations.c.id == investigation_id,
                investigations.c.repository_id == repository_id,
            )
            inv_row = (await session.execute(inv_stmt)).mappings().one_or_none()
            if inv_row is None:
                raise ScopeViolation(f"investigation {investigation_id} not found")

            summary = _row_to_summary(inv_row)

            # 3. Check write access for action availability
            has_write = True
            try:
                await require_repository(
                    session, ctx.org_id, ctx.principal_id, repository_id, write=True
                )
            except ScopeViolation:
                has_write = False

            available_actions: list[str] = []
            if has_write:
                status = summary.status
                if status == "pending":
                    available_actions = ["start", "cancel"]
                elif status in ("running", "checkpointed"):
                    available_actions = ["cancel"]
                elif status == "awaiting_human_decision":
                    available_actions = ["resume", "cancel"]
                elif status in ("completed", "failed", "cancelled"):
                    available_actions = ["retry"]

            # 4. Fetch job record if present
            job_record: InvestigationJobRecordV1 | None = None
            if summary.current_job_id is not None:
                job_stmt = select(investigation_jobs).where(
                    investigation_jobs.c.org_id == ctx.org_id,
                    investigation_jobs.c.id == summary.current_job_id,
                )
                job_row = (await session.execute(job_stmt)).mappings().one_or_none()
                if job_row is not None:
                    job_record = InvestigationJobRecordV1(
                        org_id=job_row["org_id"],
                        id=job_row["id"],
                        repository_id=job_row["repository_id"],
                        investigation_id=job_row["investigation_id"],
                        spec_hash=job_row["spec_hash"],
                        operation=job_row["operation"],
                        state=job_row["state"],
                        attempt_count=job_row["attempt_count"],
                        max_attempts=job_row["max_attempts"],
                        deadline=_ensure_utc(job_row["deadline"]),
                        failure_code=job_row.get("failure_code"),
                        report_id=job_row.get("result_report_id"),
                        created_at=_ensure_utc(job_row["created_at"]),
                        finished_at=_ensure_utc(job_row.get("finished_at")),
                    )

            # 5. Fetch published report if present
            report_data: dict[str, Any] | None = None
            claims_count = 0
            evidence_count = 0
            if summary.report_id is not None:
                rep_stmt = select(investigation_reports).where(
                    investigation_reports.c.org_id == ctx.org_id,
                    investigation_reports.c.id == summary.report_id,
                )
                rep_row = (await session.execute(rep_stmt)).mappings().one_or_none()
                if rep_row is not None:
                    report_data = rep_row["report_json"]
                    claims_count = rep_row["claims_count"]
                    evidence_count = rep_row["evidence_refs_count"]

            if evidence_count == 0:
                ev_count_stmt = (
                    select(func.count())
                    .select_from(investigation_evidence)
                    .where(
                        investigation_evidence.c.org_id == ctx.org_id,
                        investigation_evidence.c.investigation_id == investigation_id,
                    )
                )
                evidence_count = (await session.execute(ev_count_stmt)).scalar() or 0

            # 6. Count events
            events_count_stmt = (
                select(func.count())
                .select_from(investigation_events)
                .where(
                    investigation_events.c.org_id == ctx.org_id,
                    investigation_events.c.investigation_id == investigation_id,
                )
            )
            events_count = (await session.execute(events_count_stmt)).scalar() or 0

            # 7. Check current support availability
            current_support_available = True
            if summary.snapshot_id is not None:
                snap_stmt = select(source_snapshots).where(
                    source_snapshots.c.org_id == ctx.org_id,
                    source_snapshots.c.id == summary.snapshot_id,
                    source_snapshots.c.state == "sealed",
                )
                snap_row = (await session.execute(snap_stmt)).mappings().one_or_none()
                if snap_row is None:
                    current_support_available = False

            return InvestigationDetailResponseV2(
                summary=summary,
                job=job_record,
                report=report_data,
                claims_count=claims_count,
                evidence_count=evidence_count,
                events_count=events_count,
                available_actions=tuple(available_actions),
                current_support_available=current_support_available,
            )

    async def list(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> InvestigationListResponseV2:
        """Paginated list of investigations with signed, scope-bound cursor."""
        clamped_limit = min(max(limit, 1), 100)
        scope = {"org_id": str(ctx.org_id), "repository_id": str(repository_id)}

        async with self.factory() as session:
            await require_repository(
                session, ctx.org_id, ctx.principal_id, repository_id, write=False
            )

            stmt = select(investigations).where(
                investigations.c.org_id == ctx.org_id,
                investigations.c.repository_id == repository_id,
            )

            if cursor is not None:
                decoded = self.cursors.decode(cursor, scope)
                created_at_str, id_str = decoded.split("|", 1)
                cursor_dt = datetime.fromisoformat(created_at_str)
                cursor_id = UUID(id_str)
                stmt = stmt.where(
                    sa.or_(
                        investigations.c.created_at < cursor_dt,
                        sa.and_(
                            investigations.c.created_at == cursor_dt,
                            investigations.c.id < cursor_id,
                        ),
                    )
                )

            stmt = stmt.order_by(
                investigations.c.created_at.desc(),
                investigations.c.id.desc(),
            ).limit(clamped_limit + 1)

            rows = (await session.execute(stmt)).mappings().all()

            next_cursor: str | None = None
            if len(rows) > clamped_limit:
                page_rows = rows[:clamped_limit]
                last_row = page_rows[-1]
                next_cursor = self.cursors.encode(
                    scope, f"{last_row['created_at'].isoformat()}|{last_row['id']!s}"
                )
            else:
                page_rows = rows

            items = tuple(_row_to_summary(r) for r in page_rows)

            count_stmt = (
                select(func.count())
                .select_from(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.repository_id == repository_id,
                )
            )
            total_count = (await session.execute(count_stmt)).scalar() or 0

            return InvestigationListResponseV2(
                items=items,
                total_count=total_count,
                next_cursor=next_cursor,
            )

    async def events(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        investigation_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> InvestigationEventPageV1:
        """Paginated event stream with monotonic sequence identity and gap detection."""
        clamped_limit = min(max(limit, 1), 100)

        async with self.factory() as session:
            await require_repository(
                session, ctx.org_id, ctx.principal_id, repository_id, write=False
            )

            # Check investigation exists under repository
            inv_check = (
                await session.execute(
                    select(investigations.c.id).where(
                        investigations.c.org_id == ctx.org_id,
                        investigations.c.id == investigation_id,
                        investigations.c.repository_id == repository_id,
                    )
                )
            ).scalar_one_or_none()
            if inv_check is None:
                raise ScopeViolation(f"investigation {investigation_id} not found")

            # Monotonic sequence over all events of the investigation
            subq = (
                select(
                    investigation_events,
                    func.row_number()
                    .over(
                        partition_by=[
                            investigation_events.c.org_id,
                            investigation_events.c.investigation_id,
                        ],
                        order_by=[
                            investigation_events.c.step_index.asc(),
                            investigation_events.c.created_at.asc(),
                            investigation_events.c.id.asc(),
                        ],
                    )
                    .label("seq"),
                )
                .where(
                    investigation_events.c.org_id == ctx.org_id,
                    investigation_events.c.investigation_id == investigation_id,
                )
                .subquery()
            )

            stmt = (
                select(subq)
                .where(subq.c.seq > after_sequence)
                .order_by(subq.c.seq.asc())
                .limit(clamped_limit + 1)
            )

            rows = (await session.execute(stmt)).mappings().all()

            next_cursor: int | None = None
            if len(rows) > clamped_limit:
                page_rows = rows[:clamped_limit]
                next_cursor = page_rows[-1]["seq"]
            else:
                page_rows = rows

            items = tuple(
                InvestigationEventV2(
                    id=r["id"],
                    org_id=r["org_id"],
                    investigation_id=r["investigation_id"],
                    sequence=r["seq"],
                    step_index=r["step_index"],
                    event_type=r["event_type"],
                    node_name=r["node_name"],
                    payload=r.get("payload_json") or {},
                    created_at=_ensure_utc(r["created_at"]),
                )
                for r in page_rows
            )

            cursor_floor = 1 if items else 0
            return InvestigationEventPageV1(
                items=items,
                cursor_floor=cursor_floor,
                next_cursor=next_cursor,
                has_gap=False,
            )
