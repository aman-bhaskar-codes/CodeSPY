"""Fenced atomic publication of structural analysis reports with durable COMMIT recovery."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analyzers.identities import normalize_report
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import AnalysisConfigV1, ArchitectureReportV1, StructuralResultV1
from app.db.time import database_now
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, FinalizationOutcomeUnknown, StaleAttempt
from app.models.phase3_tables import analysis_specs, structural_reports
from app.models.tables import analysis_runs
from app.ports.object_store import ObjectStore
from app.repositories.event_repository import append_event
from app.repositories.job_repository import attempts, jobs, load_job, worker_context
from app.services.finalization_budget import FinalizationBudget
from app.services.jobs import JobService
from app.services.publication import PublicationExecutor


class StructuralFinalizer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        *,
        budget_seconds: float = 60,
        safety_seconds: float = 5,
        cleanup_seconds: float = 5,
    ):
        if not 0 < budget_seconds <= 60 or not 0 < cleanup_seconds <= safety_seconds <= 5:
            raise ValueError("invalid finalization profile")
        self.factory = factory
        self.store = store
        self.jobs = JobService(factory)
        self.budget_seconds = budget_seconds
        self.safety_seconds = safety_seconds
        self.cleanup_seconds = cleanup_seconds
        self.executor = PublicationExecutor(
            factory,
            budget_seconds=budget_seconds,
            safety_seconds=safety_seconds,
            cleanup_seconds=cleanup_seconds,
        )

    async def _entry(
        self,
        s: AsyncSession,
        lease: AttemptLeaseV2,
        job: dict[str, Any],
        budget: FinalizationBudget,
    ) -> dict[str, Any]:
        attempt = await self.jobs._current(s, lease, job)
        now = await database_now(s)
        required = budget.remaining() + self.safety_seconds
        if (
            min(
                (attempt["lease_expires_at"] - now).total_seconds(),
                (job["deadline"] - now).total_seconds(),
            )
            < required
        ):
            raise StaleAttempt("insufficient lease or deadline for finalization")
        await worker_context(s, lease)
        return attempt

    async def _durable(
        self, lease: AttemptLeaseV2, expected_report_sha256: str, expected_batch_hash: str
    ) -> ArchitectureReportV1:
        s = self.factory()
        try:
            async with asyncio.timeout(5), s.begin():
                await s.execute(text("SET LOCAL statement_timeout='2s'"))
                await s.execute(text("SET LOCAL lock_timeout='1s'"))
                job = await load_job(s, lease.org_id, lease.job_id)
                if (
                    job["state"] not in {"succeeded", "partial"}
                    or job["fence"] != lease.fence
                    or job["current_attempt_id"] != lease.attempt_id
                ):
                    raise StaleAttempt("terminal outcome belongs to another result")
                stmt = select(structural_reports).where(
                    structural_reports.c.org_id == lease.org_id,
                    structural_reports.c.snapshot_id == job["snapshot_id"],
                    structural_reports.c.run_id == job["result_run_id"],
                )
                row = (await s.execute(stmt)).mappings().one_or_none()
                if row is None or row["report_sha256"] != expected_report_sha256:
                    raise DuplicateConflict("durable structural publication digest differs")
                digest = await s.scalar(
                    select(analysis_runs.c.result_hash).where(
                        analysis_runs.c.org_id == lease.org_id,
                        analysis_runs.c.snapshot_id == job["snapshot_id"],
                        analysis_runs.c.id == job["result_run_id"],
                        analysis_runs.c.status.in_(["completed", "partial"]),
                    )
                )
                if digest != expected_batch_hash:
                    raise DuplicateConflict("durable structural batch digest differs")
                return ArchitectureReportV1.model_validate(row["report_json"])
        except (StaleAttempt, DuplicateConflict):
            raise
        except Exception:
            raise FinalizationOutcomeUnknown("publication outcome requires recovery") from None
        finally:
            await s.close()

    async def finish_analysis(
        self,
        lease: AttemptLeaseV2,
        result: StructuralResultV1,
        *,
        before_lock: Callable[[], Awaitable[None]] | None = None,
    ) -> ArchitectureReportV1:
        if lease.run_ref is None:
            raise ValueError("analysis finalization requires run_ref")

        result = StructuralResultV1.model_validate(result.model_dump())
        if result.run_ref != lease.run_ref:
            raise DuplicateConflict("result run differs")
        report = result.report
        report_sha256 = result.report_sha256
        normalized_sha256 = result.normalized_sha256
        expected_batch_hash = result.batch.semantic_hash()
        if normalized_sha256 != semantic_digest(normalize_report(report)):
            raise DuplicateConflict("normalized report differs")

        async def work(s: AsyncSession, budget: FinalizationBudget) -> ArchitectureReportV1:
            assert lease.run_ref is not None
            run_ref = lease.run_ref
            job, ctx = await self.jobs._locked(s, lease.org_id, lease.job_id)
            assert ctx is not None

            if job["state"] in {"succeeded", "partial"}:
                if job["fence"] != lease.fence or job["current_attempt_id"] != lease.attempt_id:
                    raise StaleAttempt("job already finished by another attempt")
                # Idempotent re-check
                existing = (
                    (
                        await s.execute(
                            select(structural_reports).where(
                                structural_reports.c.org_id == lease.org_id,
                                structural_reports.c.snapshot_id == job["snapshot_id"],
                                structural_reports.c.run_id == job["result_run_id"],
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing and existing["report_sha256"] == report_sha256:
                    stored_hash = await s.scalar(
                        select(analysis_runs.c.result_hash).where(
                            analysis_runs.c.org_id == lease.org_id,
                            analysis_runs.c.snapshot_id == run_ref.snapshot_id,
                            analysis_runs.c.id == run_ref.run_id,
                            analysis_runs.c.status.in_(["completed", "partial"]),
                        )
                    )
                    if stored_hash == expected_batch_hash:
                        return ArchitectureReportV1.model_validate(existing["report_json"])
                raise DuplicateConflict("terminal report mismatch")

            if job["operation"] != "analyze_structure":
                raise DuplicateConflict("operation differs")

            await self._entry(s, lease, job, budget)

            spec_row = (
                (
                    await s.execute(
                        select(analysis_specs).where(
                            analysis_specs.c.org_id == lease.org_id,
                            analysis_specs.c.id == job["analysis_spec_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )

            if (
                spec_row["config_hash"] != report.config_hash
                or spec_row["toolchain_hash"] != report.toolchain_hash
            ):
                raise DuplicateConflict("draft hashes differ from spec")

            config = AnalysisConfigV1.model_validate(spec_row["config_json"])
            if (
                config
                != AnalysisConfigV1.model_validate(
                    {
                        **config.model_dump(),
                        "toolchain_hash": report.toolchain_hash,
                    }
                )
                or report.limits != config.limits
            ):
                raise DuplicateConflict("report configuration differs")
            version = await s.scalar(
                select(analysis_runs.c.write_version)
                .where(
                    analysis_runs.c.org_id == lease.org_id,
                    analysis_runs.c.snapshot_id == run_ref.snapshot_id,
                    analysis_runs.c.id == run_ref.run_id,
                )
                .with_for_update()
            )
            if version is None:
                raise StaleAttempt("run unavailable")
            # Insert structural_reports row.
            # Trigger codespy_structural_report_guard verifies DB authority.
            await s.execute(
                insert(structural_reports).values(
                    org_id=lease.org_id,
                    snapshot_id=job["snapshot_id"],
                    run_id=run_ref.run_id,
                    spec_id=job["analysis_spec_id"],
                    repository_id=job["repository_id"],
                    config_hash=job["config_hash"],
                    report_json=report.model_dump(mode="json"),
                    report_sha256=report_sha256,
                    normalized_sha256=normalized_sha256,
                )
            )

            persisted = await self.jobs.persistence._persist_result_in_session(
                s, ctx, run_ref, result.batch, version
            )
            job_state = "partial" if persisted.status == "partial" else "succeeded"

            updated_job = dict(
                (
                    await s.execute(
                        update(jobs)
                        .where(jobs.c.org_id == lease.org_id, jobs.c.id == lease.job_id)
                        .values(
                            state=job_state,
                            result_run_id=run_ref.run_id,
                            finished_at=func.clock_timestamp(),
                        )
                        .returning(jobs)
                    )
                )
                .mappings()
                .one()
            )

            await s.execute(
                update(attempts)
                .where(
                    attempts.c.org_id == lease.org_id,
                    attempts.c.job_id == lease.job_id,
                    attempts.c.id == lease.attempt_id,
                )
                .values(state="succeeded", finished_at=func.clock_timestamp())
            )

            await append_event(
                s,
                updated_job,
                "report_partial" if job_state == "partial" else "report_completed",
                {"file_count": report.coverage.processed_files, "stage": "publication"},
            )
            return report

        if before_lock is not None:
            await before_lock()

        return await self.executor.execute(
            work, lambda: self._durable(lease, report_sha256, expected_batch_hash)
        )
