"""Findings query reads, safe bounded excerpts, and scan summary metrics."""

import io
import json
import zipfile
from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.triage import TriageReason, TriageRecordV1, TriageState
from app.db.transaction import transaction
from app.domain.scope import TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.models.scan_tables import (
    finding_occurrences,
    finding_support,
    finding_triage_projections,
    scan_reports,
    verification_decisions,
)
from app.models.tables import source_files
from app.ports.object_store import ObjectStore
from app.services.analysis_sources import locate_source


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
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def get_summary(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        run_id: UUID,
    ) -> ScanSummaryResponseV1:
        async with transaction(self.factory) as session:
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
                    & (finding_triage_projections.c.snapshot_id == finding_occurrences.c.snapshot_id)
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

            by_cat = Counter()
            by_sev = Counter()
            by_disp = Counter()
            by_triage = Counter()

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
        limit = min(max(1, limit), 100)
        async with transaction(self.factory) as session:
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
                    & (finding_triage_projections.c.snapshot_id == finding_occurrences.c.snapshot_id)
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
            if disposition:
                base_query = base_query.where(verification_decisions.c.disposition == disposition)
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

            if cursor:
                try:
                    cursor_id = UUID(cursor)
                    base_query = base_query.where(finding_occurrences.c.id > cursor_id)
                except ValueError:
                    pass

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
                if severity and sev != severity:
                    continue

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

            next_cursor = str(result_rows[-1]["id"]) if (has_more and result_rows) else None
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
                    & (finding_triage_projections.c.snapshot_id == finding_occurrences.c.snapshot_id)
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

            # Safe bounded code excerpt read
            code_excerpt = None
            if store is not None and start_line is not None:
                try:
                    code_excerpt = await self._read_safe_excerpt(
                        session,
                        ctx.org_id,
                        repository_id,
                        snapshot_id,
                        r["file_path"],
                        start_line,
                        end_line or start_line,
                        store,
                        redact_secrets=(r["category"] == "secret_format"),
                    )
                except Exception:
                    code_excerpt = None

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
                code_excerpt=code_excerpt,
                disposition=r["disposition"] or "unresolved",
                historical_disposition=r["disposition"] or "unresolved",
                support_reason="current_support",
                recommendation=payload.get("recommendation"),
                limitations=payload.get("limitations", []),
                triage=triage_record,
                evidence_ids=list(evidence_rows),
            )

    async def _read_safe_excerpt(
        self,
        session: AsyncSession,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        file_path: str,
        start_line: int,
        end_line: int,
        store: ObjectStore,
        *,
        redact_secrets: bool = False,
    ) -> str | None:
        located = await locate_source(session, org_id, repository_id, snapshot_id)
        raw_archive = b""
        async for chunk in store.get_stream(located.archive.storage_key):
            raw_archive += chunk
            if len(raw_archive) > 104857600:
                return None

        with zipfile.ZipFile(io.BytesIO(raw_archive)) as zf:
            try:
                content = zf.read(file_path).decode("utf-8", errors="replace")
            except KeyError:
                return None

        lines = content.splitlines()
        first = max(1, start_line - 3)
        last = min(len(lines), end_line + 3)
        if last - first > 80:
            last = first + 80

        selected = []
        for i in range(first, last + 1):
            line_idx = i - 1
            if 0 <= line_idx < len(lines):
                line_text = lines[line_idx]
                if redact_secrets and i >= start_line and i <= end_line:
                    # Redact potential secret literals while preserving line structure
                    line_text = "[REDACTED_SECRET]"
                selected.append(f"{i:4d} | {line_text}")

        excerpt = "\n".join(selected)
        return excerpt[:32768]
