"""Fenced atomic publication of per-file AST facts with durable COMMIT recovery."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ast_facts import AstFileSummaryV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import stored_config
from app.domain.errors import DuplicateConflict, FinalizationOutcomeUnknown, StaleAttempt
from app.models.phase3_tables import analysis_specs, ast_file_facts
from app.models.tables import analysis_runs
from app.repositories.event_repository import append_event
from app.repositories.job_repository import attempts, jobs, load_job
from app.services.ast_results import AstResult
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate
from app.services.structural_finalization import StructuralFinalizer


class AstFactsFinalizer(StructuralFinalizer):
    async def _durable_facts(
        self, lease: AttemptLeaseV2, expected_batch_hash: str, count: int
    ) -> int:
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
                stored = await s.scalar(
                    select(func.count())
                    .select_from(ast_file_facts)
                    .where(
                        ast_file_facts.c.org_id == lease.org_id,
                        ast_file_facts.c.snapshot_id == job["snapshot_id"],
                        ast_file_facts.c.run_id == job["result_run_id"],
                    )
                )
                digest = await s.scalar(
                    select(analysis_runs.c.result_hash).where(
                        analysis_runs.c.org_id == lease.org_id,
                        analysis_runs.c.snapshot_id == job["snapshot_id"],
                        analysis_runs.c.id == job["result_run_id"],
                        analysis_runs.c.status.in_(["completed", "partial"]),
                    )
                )
                if stored != count or digest != expected_batch_hash:
                    raise DuplicateConflict("durable AST publication differs")
                return count
        except (StaleAttempt, DuplicateConflict):
            raise
        except Exception:
            raise FinalizationOutcomeUnknown("publication outcome requires recovery") from None
        finally:
            await rollback_or_invalidate(s, self.cleanup_seconds)

    async def finish_facts(
        self,
        lease: AttemptLeaseV2,
        result: AstResult,
        *,
        before_lock: Callable[[], Awaitable[None]] | None = None,
    ) -> list[AstFileSummaryV1]:
        if lease.run_ref is None or result.run_ref != lease.run_ref:
            raise DuplicateConflict("result run differs")
        batch_hash = result.batch.semantic_hash()
        files = result.draft.files

        async def work(s: AsyncSession, budget: FinalizationBudget) -> list[AstFileSummaryV1]:
            assert lease.run_ref is not None
            run_ref = lease.run_ref
            job, ctx = await self.jobs._locked(s, lease.org_id, lease.job_id)
            assert ctx is not None
            if job["state"] in {"succeeded", "partial"}:
                if job["fence"] != lease.fence or job["current_attempt_id"] != lease.attempt_id:
                    raise StaleAttempt("job already finished by another attempt")
                await self._durable_check(s, lease, job, batch_hash, len(files))
                return self._summaries(result)
            if job["operation"] != "analyze_structure":
                raise DuplicateConflict("operation differs")
            await self._entry(s, lease, job, budget)
            spec = (
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
            config = stored_config(spec["config_json"])
            if (
                spec["config_hash"] != result.draft.config_hash
                or spec["toolchain_hash"] != result.draft.toolchain_hash
                or "ast_facts" not in config.adapters
            ):
                raise DuplicateConflict("draft differs from the AST facts spec")
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
            for facts in files:
                await s.execute(
                    insert(ast_file_facts).values(
                        org_id=lease.org_id,
                        snapshot_id=job["snapshot_id"],
                        run_id=run_ref.run_id,
                        file_id=facts.file_id,
                        repository_id=job["repository_id"],
                        spec_id=job["analysis_spec_id"],
                        config_hash=job["config_hash"],
                        language=facts.language,
                        status=facts.status,
                        facts_json=facts.model_dump(mode="json"),
                        facts_sha256=result.digests[str(facts.file_id)],
                        symbol_count=len(facts.symbols),
                        import_count=len(facts.imports),
                        export_count=len(facts.exports),
                    )
                )
            persisted = await self.jobs.persistence._persist_result_in_session(
                s, ctx, run_ref, result.batch, version
            )
            state = "partial" if persisted.status == "partial" else "succeeded"
            updated = dict(
                (
                    await s.execute(
                        update(jobs)
                        .where(jobs.c.org_id == lease.org_id, jobs.c.id == lease.job_id)
                        .values(
                            state=state,
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
                updated,
                "report_partial" if state == "partial" else "report_completed",
                {"file_count": len(files), "stage": "ast_facts"},
            )
            return self._summaries(result)

        if before_lock is not None:
            await before_lock()
        await self.executor.execute(
            work, lambda: self._durable_facts(lease, batch_hash, len(files))
        )
        return self._summaries(result)

    async def _durable_check(
        self,
        s: AsyncSession,
        lease: AttemptLeaseV2,
        job: dict[str, Any],
        batch_hash: str,
        count: int,
    ) -> None:
        stored = await s.scalar(
            select(func.count())
            .select_from(ast_file_facts)
            .where(
                ast_file_facts.c.org_id == lease.org_id,
                ast_file_facts.c.snapshot_id == job["snapshot_id"],
                ast_file_facts.c.run_id == job["result_run_id"],
            )
        )
        digest = await s.scalar(
            select(analysis_runs.c.result_hash).where(
                analysis_runs.c.org_id == lease.org_id,
                analysis_runs.c.snapshot_id == job["snapshot_id"],
                analysis_runs.c.id == job["result_run_id"],
                analysis_runs.c.status.in_(["completed", "partial"]),
            )
        )
        if stored != count or digest != batch_hash:
            raise DuplicateConflict("terminal AST publication mismatch")

    @staticmethod
    def _summaries(result: AstResult) -> list[AstFileSummaryV1]:
        return [
            AstFileSummaryV1(
                file_id=f.file_id,
                path=f.path,
                language=f.language,
                status=f.status,
                facts_sha256=result.digests[str(f.file_id)],
                symbols=len(f.symbols),
                imports=len(f.imports),
                exports=len(f.exports),
            )
            for f in result.draft.files
        ]
