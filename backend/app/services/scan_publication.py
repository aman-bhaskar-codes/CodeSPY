"""One fenced transaction for a scan's complete safe publication."""

import asyncio
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.findings import ScanPublicationDraftV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.scan_publication import project_scan, stored_draft
from app.db.time import database_now
from app.domain.canonical_json import canonical_bytes, load_json
from app.domain.errors import DuplicateConflict, FinalizationOutcomeUnknown, StaleAttempt
from app.models.scan_tables import (
    finding_occurrences,
    finding_support,
    scan_observations,
    scan_reports,
    verification_decisions,
)
from app.models.tables import analysis_runs
from app.repositories.event_repository import append_event
from app.repositories.evidence_repository import EvidenceRepository
from app.repositories.job_repository import attempts, jobs, load_job, worker_context
from app.repositories.scan_repository import ScanRepository
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate
from app.services.jobs import JobService
from app.services.publication import PublicationExecutor


def encoded(payload: dict[str, Any]) -> dict[str, Any]:
    from app.domain.canonical_json import semantic_digest

    raw = canonical_bytes(payload)
    return {
        "payload_json": load_json(raw.decode()),
        "canonical_text": raw.decode(),
        "payload_digest": semantic_digest(payload),
    }


class FindingFinalizer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        budget_seconds: float = 60,
        safety_seconds: float = 5,
        cleanup_seconds: float = 5,
    ):
        self.factory, self.jobs = factory, JobService(factory)
        self.safety_seconds, self.cleanup_seconds = safety_seconds, cleanup_seconds
        self.executor = PublicationExecutor(
            factory,
            budget_seconds=budget_seconds,
            safety_seconds=safety_seconds,
            cleanup_seconds=cleanup_seconds,
        )

    async def _stored(
        self, session: AsyncSession, lease: AttemptLeaseV2, draft_digest: str, batch_hash: str
    ) -> ScanPublicationDraftV1:
        job = await load_job(session, lease.org_id, lease.job_id)
        if (
            lease.run_ref is None
            or job["state"] not in {"succeeded", "partial"}
            or (
                job["operation"] != "scan_snapshot"
                or job["fence"] != lease.fence
                or job["current_attempt_id"] != lease.attempt_id
                or job["result_run_id"] != lease.run_ref.run_id
                or job["snapshot_id"] != lease.run_ref.snapshot_id
            )
        ):
            raise StaleAttempt("scan terminal result belongs to another attempt")
        row = (
            (
                await session.execute(
                    select(scan_reports).where(
                        scan_reports.c.org_id == lease.org_id,
                        scan_reports.c.snapshot_id == lease.run_ref.snapshot_id,
                        scan_reports.c.run_id == lease.run_ref.run_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["draft_digest"] != draft_digest or row["batch_hash"] != batch_hash:
            raise DuplicateConflict("scan terminal publication differs")
        result = stored_draft(row["draft_json"])
        projected = project_scan(result)
        if projected.draft_digest != draft_digest or projected.batch_hash != batch_hash:
            raise DuplicateConflict("stored scan payload differs")
        run_hash = await session.scalar(
            select(analysis_runs.c.result_hash).where(
                analysis_runs.c.org_id == lease.org_id,
                analysis_runs.c.snapshot_id == lease.run_ref.snapshot_id,
                analysis_runs.c.id == lease.run_ref.run_id,
                analysis_runs.c.status.in_(["completed", "partial"]),
            )
        )
        if run_hash != batch_hash:
            raise DuplicateConflict("stored scan run differs")
        return result

    async def _durable(
        self, lease: AttemptLeaseV2, draft_digest: str, batch_hash: str
    ) -> ScanPublicationDraftV1:
        session = self.factory()
        try:
            async with asyncio.timeout(5), session.begin():
                await session.execute(text("SET LOCAL statement_timeout='2s'"))
                await session.execute(text("SET LOCAL lock_timeout='1s'"))
                return await self._stored(session, lease, draft_digest, batch_hash)
        except (StaleAttempt, DuplicateConflict):
            raise
        except Exception:
            raise FinalizationOutcomeUnknown("scan publication outcome requires recovery") from None
        finally:
            await rollback_or_invalidate(session, self.cleanup_seconds)

    async def finish(
        self, lease: AttemptLeaseV2, draft: ScanPublicationDraftV1
    ) -> ScanPublicationDraftV1:
        draft = ScanPublicationDraftV1.model_validate(draft.model_dump())
        if lease.run_ref is None or draft.spec.scope != lease.run_ref:
            raise DuplicateConflict("scan result scope differs")
        projection = project_scan(draft)
        scope = {
            "org_id": lease.org_id,
            "snapshot_id": lease.run_ref.snapshot_id,
            "run_id": lease.run_ref.run_id,
        }

        async def work(session: AsyncSession, budget: FinalizationBudget) -> ScanPublicationDraftV1:
            job, ctx = await self.jobs._locked(session, lease.org_id, lease.job_id)
            assert ctx is not None and lease.run_ref is not None
            if job["state"] in {"succeeded", "partial"}:
                return await self._stored(
                    session, lease, projection.draft_digest, projection.batch_hash
                )
            if job["operation"] != "scan_snapshot":
                raise DuplicateConflict("scan operation differs")
            attempt = await self.jobs._current(session, lease, job)
            now = await database_now(session)
            if (
                min(
                    (attempt["lease_expires_at"] - now).total_seconds(),
                    (job["deadline"] - now).total_seconds(),
                )
                < budget.remaining() + self.safety_seconds
            ):
                raise StaleAttempt("insufficient scan finalization lease or deadline")
            await worker_context(session, lease)
            stored = await ScanRepository(session).get_attempt_spec(lease)
            if stored != draft.spec:
                raise DuplicateConflict("scan publication specification differs")
            version = await session.scalar(
                select(analysis_runs.c.write_version)
                .where(
                    analysis_runs.c.org_id == lease.org_id,
                    analysis_runs.c.snapshot_id == lease.run_ref.snapshot_id,
                    analysis_runs.c.id == lease.run_ref.run_id,
                    analysis_runs.c.status == "running",
                )
                .with_for_update()
            )
            if version is None:
                raise StaleAttempt("scan run unavailable")
            raw = canonical_bytes(draft.model_dump())
            await session.execute(
                insert(scan_reports).values(
                    **scope,
                    repository_id=job["repository_id"],
                    spec_id=job["scan_spec_id"],
                    config_hash=job["config_hash"],
                    draft_json=load_json(raw.decode()),
                    canonical_text=raw.decode(),
                    draft_digest=projection.draft_digest,
                    batch_hash=projection.batch_hash,
                    coverage=draft.coverage,
                    omitted_findings=draft.omitted_findings,
                )
            )
            for offset in range(0, len(projection.observations), 200):
                await session.execute(
                    insert(scan_observations),
                    [
                        {**scope, **o.model_dump(mode="json")}
                        for o in projection.observations[offset : offset + 200]
                    ],
                )
            for finding in projection.findings:
                occurrence = finding.occurrence
                await session.execute(
                    insert(finding_occurrences).values(
                        **scope,
                        id=occurrence.id,
                        file_id=occurrence.entity.file_id,
                        observation_key=finding.observation_key,
                        category=occurrence.category,
                        rule_id=occurrence.rule_id,
                        **encoded(occurrence.model_dump()),
                    )
                )
            await EvidenceRepository(session).insert_batch(ctx, lease.run_ref, list(draft.evidence))
            for finding_id, evidence_id in projection.support:
                await session.execute(
                    insert(finding_support).values(
                        **scope, finding_id=finding_id, evidence_id=evidence_id
                    )
                )
            for decision in projection.decisions:
                await session.execute(
                    insert(verification_decisions).values(
                        **scope,
                        finding_id=decision.finding_id,
                        disposition=decision.disposition,
                        **encoded(decision.model_dump()),
                    )
                )
            await self.jobs.persistence._persist_result_in_session(
                session, ctx, lease.run_ref, projection.batch, version
            )
            updated = dict(
                (
                    await session.execute(
                        update(jobs)
                        .where(jobs.c.org_id == lease.org_id, jobs.c.id == lease.job_id)
                        .values(
                            state="succeeded" if draft.coverage == "complete" else "partial",
                            result_run_id=lease.run_ref.run_id,
                            finished_at=func.clock_timestamp(),
                        )
                        .returning(jobs)
                    )
                )
                .mappings()
                .one()
            )
            await session.execute(
                update(attempts)
                .where(
                    attempts.c.org_id == lease.org_id,
                    attempts.c.job_id == lease.job_id,
                    attempts.c.id == lease.attempt_id,
                )
                .values(state="succeeded", finished_at=func.clock_timestamp())
            )
            await append_event(
                session,
                updated,
                "report_completed" if draft.coverage == "complete" else "report_partial",
                {"stage": "publication"},
            )
            return draft

        return await self.executor.execute(
            work, lambda: self._durable(lease, projection.draft_digest, projection.batch_hash)
        )
