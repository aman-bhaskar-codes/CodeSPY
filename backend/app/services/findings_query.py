"""Findings query reads, safe bounded excerpts, and scan summary metrics."""

import secrets
from collections import Counter
from datetime import datetime
from typing import Any, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.findings import Category
from app.contracts.triage import TriageReason, TriageRecordV1, TriageState
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.domain.scope import RunRef, TenantContext
from app.models.scan_tables import (
    finding_occurrences,
    finding_support,
    finding_triage_projections,
    scan_reports,
    verification_decisions,
)
from app.models.tables import source_files
from app.ports.object_store import ObjectStore
from app.ports.scan_execution import ScanCapabilities
from app.security.authorization import require_snapshot
from app.security.cursors import CursorCodec
from app.services.scan_reads import ScanReadService


class FindingSummaryV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    category: str
    rule_id: str
    severity: str
    title: str
    file_path: str
    line_number: int | None = None
    disposition: str
    triage_state: TriageState
    triage_reason: TriageReason
    triage_version: int


class FindingListResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[FindingSummaryV1]
    next_cursor: str | None = None
    total_count: int


class FindingDetailResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    run_id: UUID
    category: str
    rule_id: str
    severity: str
    title: str
    message: str
    file_path: str
    line_number: int | None = None
    end_line: int | None = None
    code_excerpt: str | None = None
    disposition: str
    historical_disposition: str
    support_reason: str
    recommendation: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
    triage: TriageRecordV1
    evidence_ids: list[UUID] = Field(default_factory=list)


class ScanSummaryResponseV1(BaseModel):
    model_config = ConfigDict(frozen=True)

    scan_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    status: str
    total_findings: int
    by_category: dict[str, int]
    by_severity: dict[str, int]
    by_disposition: dict[str, int]
    by_triage: dict[str, int]
    created_at: datetime | None = None


class FindingsQueryService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        capabilities: ScanCapabilities | None = None,
        cursors: CursorCodec | None = None,
    ):
        self.factory = factory
        self.scan_reads = ScanReadService(factory, capabilities)
        self.cursors = cursors or CursorCodec(secrets.token_bytes(32))

    async def get_summary(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        run_id: UUID,
    ) -> ScanSummaryResponseV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            report_row = (
                (
                    await session.execute(
                        select(scan_reports).where(
                            scan_reports.c.org_id == ctx.org_id,
                            scan_reports.c.repository_id == repository_id,
                            scan_reports.c.snapshot_id == snapshot_id,
                            scan_reports.c.run_id == run_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if report_row is None:
                raise NoResultFound("scan_report_not_found")

            # Query all findings for this scan run with triage projections and decisions
            query = (
                select(
                    finding_occurrences.c.category,
                    finding_occurrences.c.payload_json,
                    verification_decisions.c.disposition,
                    finding_triage_projections.c.current_state,
                )
                .outerjoin(
                    verification_decisions,
                    (verification_decisions.c.org_id == finding_occurrences.c.org_id)
                    & (verification_decisions.c.snapshot_id == finding_occurrences.c.snapshot_id)
                    & (verification_decisions.c.run_id == finding_occurrences.c.run_id)
                    & (verification_decisions.c.finding_id == finding_occurrences.c.id),
                )
                .outerjoin(
                    finding_triage_projections,
                    (finding_triage_projections.c.org_id == finding_occurrences.c.org_id)
                    & (
                        finding_triage_projections.c.snapshot_id
                        == finding_occurrences.c.snapshot_id
                    )
                    & (finding_triage_projections.c.run_id == finding_occurrences.c.run_id)
                    & (finding_triage_projections.c.finding_id == finding_occurrences.c.id),
                )
                .where(
                    finding_occurrences.c.org_id == ctx.org_id,
                    finding_occurrences.c.snapshot_id == snapshot_id,
                    finding_occurrences.c.run_id == run_id,
                )
            )

            rows = (await session.execute(query)).all()
            total = len(rows)

            by_cat: Counter[str] = Counter()
            by_sev: Counter[str] = Counter()
            by_disp: Counter[str] = Counter()
            by_triage: Counter[str] = Counter()

            for cat, payload, disp, triage_state in rows:
                by_cat[cat] += 1
                sev = payload.get("severity", "info") if isinstance(payload, dict) else "info"
                by_sev[sev] += 1
                by_disp[disp or "unresolved"] += 1
                by_triage[triage_state or "unreviewed"] += 1

            return ScanSummaryResponseV1(
                scan_id=run_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                status="completed",
                total_findings=total,
                by_category=dict(by_cat),
                by_severity=dict(by_sev),
                by_disposition=dict(by_disp),
                by_triage=dict(by_triage),
                created_at=report_row["created_at"],
            )

    async def list_findings(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        run_id: UUID,
        *,
        category: str | None = None,
        severity: str | None = None,
        disposition: str | None = None,
        triage_state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> FindingListResponseV1:
        if not 1 <= limit <= 100:
            raise ValueError("findings limit must be between 1 and 100")
        for value, allowed in (
            (category, get_args(Category)),
            (severity, ("info", "low", "medium", "high", "critical")),
            (disposition, ("proposed", "supported-static", "contradicted", "unresolved")),
            (triage_state, get_args(TriageState)),
        ):
            if value is not None and value not in allowed:
                raise ValueError("invalid findings filter")
        scope = {
            "kind": "findings",
            "org": str(ctx.org_id),
            "principal": str(ctx.principal_id),
            "repository": str(repository_id),
            "snapshot": str(snapshot_id),
            "run": str(run_id),
            "category": category,
            "severity": severity,
            "disposition": disposition,
            "triage_state": triage_state,
        }
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            base_query = (
                select(
                    finding_occurrences.c.id,
                    finding_occurrences.c.run_id,
                    finding_occurrences.c.category,
                    finding_occurrences.c.rule_id,
                    finding_occurrences.c.payload_json,
                    source_files.c.path.label("file_path"),
                    verification_decisions.c.disposition,
                    finding_triage_projections.c.current_state,
                    finding_triage_projections.c.current_reason,
                    finding_triage_projections.c.version,
                )
                .join(
                    source_files,
                    (source_files.c.org_id == finding_occurrences.c.org_id)
                    & (source_files.c.snapshot_id == finding_occurrences.c.snapshot_id)
                    & (source_files.c.id == finding_occurrences.c.file_id),
                )
                .outerjoin(
                    verification_decisions,
                    (verification_decisions.c.org_id == finding_occurrences.c.org_id)
                    & (verification_decisions.c.snapshot_id == finding_occurrences.c.snapshot_id)
                    & (verification_decisions.c.run_id == finding_occurrences.c.run_id)
                    & (verification_decisions.c.finding_id == finding_occurrences.c.id),
                )
                .outerjoin(
                    finding_triage_projections,
                    (finding_triage_projections.c.org_id == finding_occurrences.c.org_id)
                    & (
                        finding_triage_projections.c.snapshot_id
                        == finding_occurrences.c.snapshot_id
                    )
                    & (finding_triage_projections.c.run_id == finding_occurrences.c.run_id)
                    & (finding_triage_projections.c.finding_id == finding_occurrences.c.id),
                )
                .where(
                    finding_occurrences.c.org_id == ctx.org_id,
                    finding_occurrences.c.snapshot_id == snapshot_id,
                    finding_occurrences.c.run_id == run_id,
                )
            )

            if category:
                base_query = base_query.where(finding_occurrences.c.category == category)
            if severity:
                base_query = base_query.where(
                    func.coalesce(
                        finding_occurrences.c.payload_json["severity"].as_string(), "info"
                    )
                    == severity
                )
            if disposition:
                base_query = base_query.where(
                    func.coalesce(verification_decisions.c.disposition, "unresolved") == disposition
                )
            if triage_state:
                if triage_state == "unreviewed":
                    base_query = base_query.where(
                        (finding_triage_projections.c.current_state == "unreviewed")
                        | (finding_triage_projections.c.current_state.is_(None))
                    )
                else:
                    base_query = base_query.where(
                        finding_triage_projections.c.current_state == triage_state
                    )

            count_query = select(func.count()).select_from(base_query.subquery())
            total_count = (await session.scalar(count_query)) or 0

            if cursor is not None:
                try:
                    cursor_id = UUID(self.cursors.decode(cursor, scope))
                except ValueError:
                    raise ValueError("invalid findings cursor") from None
                base_query = base_query.where(finding_occurrences.c.id > cursor_id)

            base_query = base_query.order_by(finding_occurrences.c.id.asc()).limit(limit + 1)
            rows = (await session.execute(base_query)).mappings().all()

            has_more = len(rows) > limit
            result_rows = rows[:limit]

            items = []
            for r in result_rows:
                payload = r["payload_json"] if isinstance(r["payload_json"], dict) else {}
                entity = payload.get("entity", {})
                line_no = None
                if isinstance(entity, dict) and "range" in entity:
                    line_no = entity["range"].get("start_line")

                sev = payload.get("severity", "info")

                items.append(
                    FindingSummaryV1(
                        id=r["id"],
                        run_id=r["run_id"],
                        category=r["category"],
                        rule_id=r["rule_id"],
                        severity=sev,
                        title=payload.get("title", r["rule_id"]),
                        file_path=r["file_path"],
                        line_number=line_no,
                        disposition=r["disposition"] or "unresolved",
                        triage_state=r["current_state"] or "unreviewed",
                        triage_reason=r["current_reason"] or "none",
                        triage_version=r["version"] or 0,
                    )
                )

            next_cursor = (
                self.cursors.encode(scope, str(result_rows[-1]["id"]))
                if (has_more and result_rows)
                else None
            )
            return FindingListResponseV1(
                items=items,
                next_cursor=next_cursor,
                total_count=total_count,
            )

    async def get_finding_detail(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
        store: ObjectStore | None = None,
    ) -> FindingDetailResponseV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            query = (
                select(
                    finding_occurrences.c.id,
                    finding_occurrences.c.run_id,
                    finding_occurrences.c.category,
                    finding_occurrences.c.rule_id,
                    finding_occurrences.c.payload_json,
                    finding_occurrences.c.file_id,
                    source_files.c.path.label("file_path"),
                    verification_decisions.c.disposition,
                    finding_triage_projections.c.current_state,
                    finding_triage_projections.c.current_reason,
                    finding_triage_projections.c.last_event_id,
                    finding_triage_projections.c.updated_by,
                    finding_triage_projections.c.updated_at,
                    finding_triage_projections.c.version,
                )
                .join(
                    source_files,
                    (source_files.c.org_id == finding_occurrences.c.org_id)
                    & (source_files.c.snapshot_id == finding_occurrences.c.snapshot_id)
                    & (source_files.c.id == finding_occurrences.c.file_id),
                )
                .outerjoin(
                    verification_decisions,
                    (verification_decisions.c.org_id == finding_occurrences.c.org_id)
                    & (verification_decisions.c.snapshot_id == finding_occurrences.c.snapshot_id)
                    & (verification_decisions.c.run_id == finding_occurrences.c.run_id)
                    & (verification_decisions.c.finding_id == finding_occurrences.c.id),
                )
                .outerjoin(
                    finding_triage_projections,
                    (finding_triage_projections.c.org_id == finding_occurrences.c.org_id)
                    & (
                        finding_triage_projections.c.snapshot_id
                        == finding_occurrences.c.snapshot_id
                    )
                    & (finding_triage_projections.c.run_id == finding_occurrences.c.run_id)
                    & (finding_triage_projections.c.finding_id == finding_occurrences.c.id),
                )
                .where(
                    finding_occurrences.c.org_id == ctx.org_id,
                    finding_occurrences.c.snapshot_id == snapshot_id,
                    finding_occurrences.c.id == finding_id,
                )
            )

            r = (await session.execute(query)).mappings().one_or_none()
            if r is None:
                raise NoResultFound("finding_not_found")

            # Evidence IDs
            evidence_rows = (
                await session.scalars(
                    select(finding_support.c.evidence_id).where(
                        finding_support.c.org_id == ctx.org_id,
                        finding_support.c.snapshot_id == snapshot_id,
                        finding_support.c.run_id == r["run_id"],
                        finding_support.c.finding_id == finding_id,
                    )
                )
            ).all()

            payload = r["payload_json"] if isinstance(r["payload_json"], dict) else {}
            entity = payload.get("entity", {})
            start_line = None
            end_line = None
            if isinstance(entity, dict) and "range" in entity:
                start_line = entity["range"].get("start_line")
                end_line = entity["range"].get("end_line")

            historical = r["disposition"] or "unresolved"
            effective = historical
            support_reason = "current_support"
            if historical == "supported-static":
                effective, support_reason = "unresolved", "source_unavailable"
                try:
                    current = await self.scan_reads.read_in_session(
                        session,
                        ctx,
                        RunRef(org_id=ctx.org_id, snapshot_id=snapshot_id, run_id=r["run_id"]),
                    )
                except (DuplicateConflict, NoResultFound, ValueError):
                    current = None
                decision = (
                    next(
                        (d for d in current.effective_decisions if d.finding_id == finding_id),
                        None,
                    )
                    if current is not None
                    else None
                )
                if decision is not None and decision.historical_disposition == historical:
                    effective = decision.effective_disposition
                    support_reason = decision.reason

            triage_record = TriageRecordV1(
                finding_id=finding_id,
                current_state=r["current_state"] or "unreviewed",
                current_reason=r["current_reason"] or "none",
                last_event_id=r["last_event_id"],
                updated_by=r["updated_by"],
                updated_at=r["updated_at"],
                version=r["version"] or 0,
            )

            return FindingDetailResponseV1(
                id=finding_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                run_id=r["run_id"],
                category=r["category"],
                rule_id=r["rule_id"],
                severity=payload.get("severity", "info"),
                title=payload.get("title", r["rule_id"]),
                message=payload.get("severity_basis", ""),
                file_path=r["file_path"],
                line_number=start_line,
                end_line=end_line,
                code_excerpt=None,
                disposition=effective,
                historical_disposition=historical,
                support_reason=support_reason,
                recommendation=payload.get("recommendation"),
                limitations=payload.get("limitations", []),
                triage=triage_record,
                evidence_ids=list(evidence_rows),
            )
