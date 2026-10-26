"""Atomic report publication and commit recovery (Task I).

Implements section 12 atomic publication in a single bounded transaction with no external
work inside locks. Compares existing receipts for exact replay, prevents orphan reports or
duplicate publication, updates jobs, attempts and investigations atomically, and recovers
ambiguous commits cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.contracts.investigation import InvestigationVerdict
from app.contracts.investigation_execution import (
    InvestigationPublicationReceiptV1,
    InvestigationReportDraftV1,
    InvestigationReportV1,
    InvestigationScopeV1,
)
from app.contracts.jobs import AttemptLeaseV2
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.models.investigation_execution_tables import (
    investigation_jobs,
    investigation_publication_receipts,
    investigation_reports,
)
from app.models.investigation_tables import investigations
from app.models.phase2_tables import attempts, jobs
from app.ports.investigation_execution import InvestigationSupportPort
from app.repositories.investigation_execution_repository import (
    SqlInvestigationReportRepository,
    SqlInvestigationSpecRepository,
)
from app.security.authorization import lock_scope, require_repository
from app.services.finalization_budget import FinalizationBudget
from app.services.investigation_checkpointer import StaleWorkerFencingError
from app.services.publication import PublicationExecutor
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class InvestigationFinalizer:
    """Publishes investigation reports atomically with attempt and job success."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        report_repo: SqlInvestigationReportRepository | None = None,
        spec_repo: SqlInvestigationSpecRepository | None = None,
        *,
        budget_seconds: float = 10,
        source_support: InvestigationSupportPort | None = None,
        memory_proposals: bool = False,
    ) -> None:
        if not 0 < budget_seconds <= 10:
            raise ValueError("investigation publication budget must be at most ten seconds")
        self.executor = PublicationExecutor(
            factory, budget_seconds=budget_seconds, statement_seconds=5, idle_seconds=5
        )
        self.memory_proposals = memory_proposals
        self.source_support = source_support
        self.factory = factory
        self.report_repo = report_repo or SqlInvestigationReportRepository()
        self.spec_repo = spec_repo or SqlInvestigationSpecRepository()

    async def publish(
        self,
        arg1: TenantContext | AttemptLeaseV2,
        arg2: AttemptLeaseV2 | InvestigationReportDraftV1 | Any,
        arg3: InvestigationReportDraftV1 | None = None,
    ) -> InvestigationPublicationReceiptV1:
        """Atomically publishes report draft and marks job and investigation completed."""
        if isinstance(arg1, AttemptLeaseV2):
            lease = arg1
            if isinstance(arg2, InvestigationReportDraftV1):
                draft = arg2
            elif hasattr(arg2, "draft") and arg2.draft is not None:
                draft = arg2.draft
            else:
                raise ValueError("draft required for publication")
            ctx: TenantContext | None = None
        else:
            ctx = arg1
            if not isinstance(arg2, AttemptLeaseV2):
                raise TypeError(f"expected AttemptLeaseV2, got {type(arg2)}")
            lease = arg2
            if not isinstance(arg3, InvestigationReportDraftV1):
                raise TypeError(f"expected InvestigationReportDraftV1, got {type(arg3)}")
            draft = arg3

        async def authorized(session: AsyncSession) -> None:
            hint = (
                (
                    await session.execute(
                        select(investigation_jobs).where(
                            investigation_jobs.c.org_id == lease.org_id,
                            investigation_jobs.c.id == lease.job_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if hint is None or hint["operation"] != "investigate_snapshot":
                raise ScopeViolation("investigation job unavailable")
            if ctx is not None and (
                ctx.org_id != lease.org_id or ctx.principal_id != hint["principal_id"]
            ):
                raise ScopeViolation("publication actor differs")
            if (
                hint["investigation_id"] != draft.investigation_id
                or hint["spec_hash"] != draft.spec_hash
            ):
                raise DuplicateConflict("publication scope differs")
            await lock_scope(session, lease.org_id, hint["principal_id"], hint["repository_id"])
            await require_repository(
                session,
                lease.org_id,
                hint["principal_id"],
                hint["repository_id"],
                write=True,
                lock=True,
            )

            if self.source_support is not None:
                spec = await self.spec_repo.get_spec(session, lease.org_id, draft.spec_hash)
                if spec is None:
                    raise ScopeViolation("investigation specification unavailable")
                await self.source_support.assert_current(
                    session,
                    InvestigationScopeV1(
                        org_id=lease.org_id,
                        repository_id=hint["repository_id"],
                        snapshot_id=spec.snapshot_id,
                        principal_id=hint["principal_id"],
                        investigation_id=draft.investigation_id,
                        spec_hash=draft.spec_hash,
                        finding_id=spec.finding_id,
                        finding_run_id=spec.finding_run_id,
                        chunk_set_id=spec.chunk_set_id,
                        execution_revision=spec.runtime_revision,
                    ),
                )

        def exact(receipt: InvestigationPublicationReceiptV1) -> InvestigationPublicationReceiptV1:
            if (
                receipt.job_id != lease.job_id
                or receipt.attempt_id != lease.attempt_id
                or receipt.attempt_fence != lease.fence
                or receipt.spec_hash != draft.spec_hash
                or receipt.investigation_id != draft.investigation_id
                or receipt.draft_digest != draft.draft_digest
            ):
                raise DuplicateConflict("publication receipt identity conflict")
            return receipt

        async def work(
            session: AsyncSession, budget: FinalizationBudget
        ) -> InvestigationPublicationReceiptV1:
            await authorized(session)
            existing = await self._check_existing_receipt(session, lease.org_id, lease.job_id)
            if existing is not None:
                return exact(existing)
            return await self._atomic_publish(session, lease, draft)

        async def resolve() -> InvestigationPublicationReceiptV1:
            async with asyncio.timeout(5), transaction(self.factory) as session:
                await authorized(session)
                recovered = await self._check_existing_receipt(session, lease.org_id, lease.job_id)
                if recovered is None:
                    from app.domain.errors import FinalizationOutcomeUnknown

                    raise FinalizationOutcomeUnknown("investigation publication requires recovery")
                return exact(recovered)

        return await self.executor.execute(work, resolve)

    async def _check_existing_receipt(
        self,
        session: AsyncSession,
        org_id: UUID,
        job_id: UUID,
    ) -> InvestigationPublicationReceiptV1 | None:
        stmt = select(investigation_publication_receipts).where(
            investigation_publication_receipts.c.org_id == org_id,
            investigation_publication_receipts.c.job_id == job_id,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        return InvestigationPublicationReceiptV1(
            receipt_id=row["id"],
            investigation_id=row["investigation_id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            attempt_fence=row["fencing_token"],
            spec_hash=row["spec_hash"],
            report_id=row["report_id"],
            report_digest=row["report_digest"],
            draft_digest=row["draft_digest"],
            created_at=_ensure_utc(row["published_at"]),
        )

    async def _atomic_publish(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        draft: InvestigationReportDraftV1,
    ) -> InvestigationPublicationReceiptV1:
        now = await database_now(session)
        now_utc = _ensure_utc(now)

        # 1. Lock job
        job_stmt = (
            select(jobs)
            .where(jobs.c.org_id == lease.org_id, jobs.c.id == lease.job_id)
            .with_for_update()
        )
        job_row = (await session.execute(job_stmt)).mappings().one_or_none()
        if job_row is None:
            raise ScopeViolation(f"job {lease.job_id} not found")

        if job_row["current_attempt_id"] != lease.attempt_id:
            raise StaleWorkerFencingError("publication attempt is no longer current")
        job_state = job_row["state"]
        if job_state in ("cancelling", "cancelled"):
            raise ScopeViolation(f"cannot publish report for {job_state} job {lease.job_id}")

        if job_state == "failed":
            raise ScopeViolation(f"cannot publish report for failed job {lease.job_id}")

        if job_row["fence"] != lease.fence:
            raise StaleWorkerFencingError(
                f"fencing token mismatch: db={job_row['fence']}, lease={lease.fence}"
            )

        deadline = _ensure_utc(job_row["deadline"])
        if deadline is not None and deadline <= now_utc:
            raise ScopeViolation(f"job deadline expired before publication for {lease.job_id}")

        # If job already succeeded, check replay
        if job_state == "succeeded":
            existing = await self._check_existing_receipt(session, lease.org_id, lease.job_id)
            if existing is not None:
                if existing.draft_digest == draft.draft_digest:
                    return existing
                raise DuplicateConflict(
                    f"report publication digest conflict for job {lease.job_id}"
                )

        # 2. Lock attempt
        attempt_stmt = (
            select(attempts)
            .where(
                attempts.c.org_id == lease.org_id,
                attempts.c.job_id == lease.job_id,
                attempts.c.id == lease.attempt_id,
            )
            .with_for_update()
        )
        attempt_row = (await session.execute(attempt_stmt)).mappings().one_or_none()
        if attempt_row is None:
            raise ScopeViolation(f"attempt {lease.attempt_id} not found")

        if (
            attempt_row["state"] != "active"
            or attempt_row["fence"] != lease.fence
            or attempt_row["owner"] != lease.owner
        ):
            raise StaleWorkerFencingError("publication attempt authority differs")
        now_utc = _ensure_utc(await database_now(session))
        lease_expiry = _ensure_utc(attempt_row["lease_expires_at"])
        if lease_expiry is not None and lease_expiry <= now_utc:
            raise ScopeViolation(f"attempt lease expired before publication for {lease.attempt_id}")

        if (lease_expiry - now_utc).total_seconds() < 65:
            raise ScopeViolation("insufficient remaining publication lease")
        if (deadline - now_utc).total_seconds() < 15:
            raise ScopeViolation("insufficient remaining publication deadline")
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            from app.repositories.job_repository import worker_context

            await worker_context(session, lease)

        # 3. Lock investigation
        inv_stmt = (
            select(investigations)
            .where(
                investigations.c.org_id == lease.org_id,
                investigations.c.id == draft.investigation_id,
            )
            .with_for_update()
        )
        inv_row = (await session.execute(inv_stmt)).mappings().one_or_none()
        if inv_row is None:
            raise ScopeViolation(f"investigation {draft.investigation_id} not found")

        inv_status = inv_row["status"]
        if inv_status in ("cancelled", "failed"):
            raise ScopeViolation(
                f"cannot publish report for {inv_status} investigation {draft.investigation_id}"
            )

        # 4. Resolve spec for runtime revision
        spec = await self.spec_repo.get_spec(session, lease.org_id, draft.spec_hash)
        if spec is None or inv_row["spec_hash"] != draft.spec_hash:
            raise DuplicateConflict("publication specification conflict")
        runtime_revision = spec.runtime_revision

        # 5. Build full InvestigationReportV1
        report_id = uuid4()
        report_proto = InvestigationReportV1(
            report_id=report_id,
            investigation_id=draft.investigation_id,
            spec_hash=draft.spec_hash,
            runtime_revision=runtime_revision,
            verdict=(
                draft.verdict
                if draft.verdict in {v.value for v in InvestigationVerdict}
                else InvestigationVerdict.INDETERMINATE.value
            ),
            verdict_confidence=draft.verdict_confidence,
            summary=draft.summary,
            claims=draft.claims,
            verifications=draft.verifications,
            evidence_refs=draft.evidence_refs,
            coverage_notes=draft.coverage_notes,
            unknowns=draft.unknowns,
            limitations=draft.limitations,
            settled_usage_micros=draft.settled_usage_micros,
            held_usage_micros=draft.held_usage_micros,
            stop_reason=draft.stop_reason,
            current_support_available=draft.current_support_available,
            report_digest="0" * 64,
            created_at=now_utc,
        )
        report_digest = report_proto.compute_report_digest()
        report = report_proto.model_copy(update={"report_digest": report_digest})

        # 6. Insert investigation_reports
        report_values = {
            "org_id": lease.org_id,
            "id": report_id,
            "investigation_id": draft.investigation_id,
            "spec_hash": draft.spec_hash,
            "execution_revision": runtime_revision,
            "job_id": lease.job_id,
            "verdict": report.verdict,
            "verdict_confidence": report.verdict_confidence,
            "summary": report.summary,
            "report_json": report.model_dump(mode="json"),
            "report_digest": report_digest,
            "claims_count": len(report.claims),
            "evidence_refs_count": len(report.evidence_refs),
            "created_at": now_utc,
        }
        await session.execute(insert(investigation_reports).values(**report_values))

        # 7. Insert investigation_publication_receipts
        receipt_id = uuid4()
        publication_time = now_utc
        receipt_values = {
            "org_id": lease.org_id,
            "id": receipt_id,
            "investigation_id": draft.investigation_id,
            "spec_hash": draft.spec_hash,
            "job_id": lease.job_id,
            "attempt_id": lease.attempt_id,
            "fencing_token": lease.fence,
            "report_id": report_id,
            "report_digest": report_digest,
            "draft_digest": draft.draft_digest,
            "published_at": now_utc,
        }
        await session.execute(insert(investigation_publication_receipts).values(**receipt_values))

        now_utc = _ensure_utc(await database_now(session))
        if deadline <= now_utc or lease_expiry <= now_utc:
            raise ScopeViolation("publication authority expired before terminal write")

        # 8. Transition jobs and attempts to succeeded
        await session.execute(
            update(investigation_jobs)
            .where(
                investigation_jobs.c.org_id == lease.org_id, investigation_jobs.c.id == lease.job_id
            )
            .values(
                state="succeeded",
                result_report_id=report_id,
                finished_at=now_utc,
                publication_checked_at=now_utc,
            )
        )
        await session.execute(
            update(attempts)
            .where(
                attempts.c.org_id == lease.org_id,
                attempts.c.job_id == lease.job_id,
                attempts.c.id == lease.attempt_id,
            )
            .values(state="succeeded", finished_at=now_utc)
        )

        # 9. Transition investigations to completed
        await session.execute(
            update(investigations)
            .where(
                investigations.c.org_id == lease.org_id,
                investigations.c.id == draft.investigation_id,
            )
            .values(
                final_report_id=report_id,
                verdict=report.verdict,
                verdict_confidence=report.verdict_confidence,
                summary=report.summary,
                status="completed",
                updated_at=now_utc,
            )
        )

        if self.memory_proposals:
            from app.models.phase2_tables import PHASE2_TABLES

            await session.execute(
                insert(PHASE2_TABLES["outbox"]).values(
                    org_id=lease.org_id,
                    id=uuid4(),
                    job_id=lease.job_id,
                    kind="memory_proposals",
                    generation=1,
                    payload={
                        "schema_version": 1,
                        "org_id": str(lease.org_id),
                        "repository_id": str(inv_row["repository_id"]),
                        "snapshot_id": str(spec.snapshot_id),
                        "investigation_id": str(draft.investigation_id),
                        "report_id": str(report_id),
                        "report_digest": report_digest,
                        "derivation_revision": 1,
                    },
                )
            )

        return InvestigationPublicationReceiptV1(
            receipt_id=receipt_id,
            investigation_id=draft.investigation_id,
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            attempt_fence=lease.fence,
            spec_hash=draft.spec_hash,
            report_id=report_id,
            report_digest=report_digest,
            draft_digest=draft.draft_digest,
            created_at=publication_time,
        )
