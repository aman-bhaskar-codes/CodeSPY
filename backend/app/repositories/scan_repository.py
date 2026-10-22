"""Scoped immutable scan configuration and attempt metadata; no commits."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.findings import ScanPublicationDraftV1, ScanPublicationSpecV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.scan_jobs import ScanConfigV1, ScanSpecRecordV1, stored_config
from app.contracts.scan_publication import project_scan, stored_draft
from app.domain.canonical_json import canonical_bytes, load_json, semantic_digest
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import RunRef, TenantContext
from app.models.scan_tables import scan_attempt_specs, scan_jobs, scan_reports, scan_specs
from app.models.tables import analysis_runs
from app.repositories.job_repository import attempts
from app.security.authorization import require_repository


def spec_record(row: Any) -> ScanSpecRecordV1:
    return ScanSpecRecordV1(
        **{
            k: row[k]
            for k in (
                "org_id",
                "id",
                "repository_id",
                "snapshot_id",
                "principal_id",
                "config_hash",
                "created_at",
            )
        },
        config=stored_config(row["config_json"]),
    )


class ScanRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_spec(
        self, ctx: TenantContext, repository_id: UUID, spec_id: UUID
    ) -> ScanSpecRecordV1 | None:
        await require_repository(self.session, ctx.org_id, ctx.principal_id, repository_id)
        row = (
            (
                await self.session.execute(
                    select(scan_specs).where(
                        scan_specs.c.org_id == ctx.org_id,
                        scan_specs.c.repository_id == repository_id,
                        scan_specs.c.id == spec_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return spec_record(row) if row is not None else None

    async def create_attempt_spec(
        self,
        lease: AttemptLeaseV2,
        spec_id: UUID,
        repository_id: UUID,
        config: ScanConfigV1,
        captured_at: datetime,
    ) -> ScanPublicationSpecV1:
        if lease.run_ref is None:
            raise ValueError("scan attempt requires run scope")
        publication = config.publication_spec(lease.run_ref, repository_id, captured_at)
        raw = canonical_bytes(publication.model_dump())
        if len(raw) > 98304:
            raise ValueError("scan attempt specification exceeds bound")
        await self.session.execute(
            insert(scan_attempt_specs).values(
                org_id=lease.org_id,
                snapshot_id=lease.run_ref.snapshot_id,
                run_id=lease.run_ref.run_id,
                repository_id=repository_id,
                spec_id=spec_id,
                config_hash=config.semantic_hash(),
                job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                fence=lease.fence,
                publication_spec_json=load_json(raw.decode()),
                canonical_text=raw.decode(),
                spec_digest=semantic_digest(publication.model_dump()),
                captured_at=captured_at,
            )
        )
        return publication

    async def get_attempt_spec(self, lease: AttemptLeaseV2) -> ScanPublicationSpecV1:
        if lease.run_ref is None:
            raise ValueError("scan attempt requires run scope")
        row = (
            (
                await self.session.execute(
                    select(scan_attempt_specs).where(
                        scan_attempt_specs.c.org_id == lease.org_id,
                        scan_attempt_specs.c.job_id == lease.job_id,
                        scan_attempt_specs.c.attempt_id == lease.attempt_id,
                        scan_attempt_specs.c.fence == lease.fence,
                        scan_attempt_specs.c.snapshot_id == lease.run_ref.snapshot_id,
                        scan_attempt_specs.c.run_id == lease.run_ref.run_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ScopeViolation("scan attempt specification unavailable")
        value = ScanPublicationSpecV1.model_validate_json(
            canonical_bytes(row["publication_spec_json"])
        )
        if semantic_digest(value.model_dump()) != row["spec_digest"]:
            raise DuplicateConflict("scan attempt specification differs")
        return value

    async def read(self, ctx: TenantContext, ref: RunRef) -> ScanPublicationDraftV1 | None:
        if ref.org_id != ctx.org_id:
            raise ScopeViolation("scan unavailable")
        row = (
            (
                await self.session.execute(
                    select(scan_reports).where(
                        scan_reports.c.org_id == ref.org_id,
                        scan_reports.c.snapshot_id == ref.snapshot_id,
                        scan_reports.c.run_id == ref.run_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        await require_repository(self.session, ctx.org_id, ctx.principal_id, row["repository_id"])
        joined = await self.session.scalar(
            select(scan_jobs.c.id)
            .join(
                scan_attempt_specs,
                (scan_attempt_specs.c.org_id == scan_jobs.c.org_id)
                & (scan_attempt_specs.c.job_id == scan_jobs.c.id)
                & (scan_attempt_specs.c.attempt_id == scan_jobs.c.current_attempt_id)
                & (scan_attempt_specs.c.fence == scan_jobs.c.fence)
                & (scan_attempt_specs.c.spec_id == scan_jobs.c.scan_spec_id)
                & (scan_attempt_specs.c.config_hash == scan_jobs.c.config_hash),
            )
            .join(
                analysis_runs,
                (analysis_runs.c.org_id == scan_attempt_specs.c.org_id)
                & (analysis_runs.c.snapshot_id == scan_attempt_specs.c.snapshot_id)
                & (analysis_runs.c.id == scan_attempt_specs.c.run_id),
            )
            .join(
                attempts,
                (attempts.c.org_id == scan_jobs.c.org_id)
                & (attempts.c.job_id == scan_jobs.c.id)
                & (attempts.c.id == scan_jobs.c.current_attempt_id),
            )
            .where(
                scan_jobs.c.org_id == ref.org_id,
                scan_jobs.c.snapshot_id == ref.snapshot_id,
                scan_jobs.c.result_run_id == ref.run_id,
                scan_jobs.c.operation == "scan_snapshot",
                scan_jobs.c.repository_id == row["repository_id"],
                scan_jobs.c.scan_spec_id == row["spec_id"],
                scan_jobs.c.config_hash == row["config_hash"],
                scan_jobs.c.state.in_(["succeeded", "partial"]),
                scan_attempt_specs.c.run_id == ref.run_id,
                analysis_runs.c.result_hash == row["batch_hash"],
                analysis_runs.c.status.in_(["completed", "partial"]),
                attempts.c.state == "succeeded",
            )
        )
        if joined is None:
            raise DuplicateConflict("scan terminal association differs")
        draft = stored_draft(row["draft_json"])
        projection = project_scan(draft)
        if (
            draft.spec.scope != ref
            or projection.draft_digest != row["draft_digest"]
            or projection.batch_hash != row["batch_hash"]
        ):
            raise DuplicateConflict("scan stored payload differs")
        return draft
