"""Durable jobs. Queue delivery never supplies publication or lifecycle authority."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.indexing import EnqueueIndexJobV5, IndexJobRecordV5
from app.contracts.investigation_jobs import EnqueueInvestigationJobV1, InvestigationJobRecordV1
from app.contracts.jobs import AttemptLeaseV2, EnqueueJobV2, EnqueueJobV3, JobRecordV2, JobRecordV3
from app.contracts.persistence import RunCreate
from app.contracts.scan_jobs import EnqueueScanJobV4, ScanJobRecordV4
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import CapacityExceeded, DuplicateConflict, ScopeViolation, StaleAttempt
from app.domain.scope import RunRef, TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import analysis_runs
from app.repositories.event_repository import append_event
from app.repositories.job_repository import (
    attempts,
    job_record,
    job_record_v3,
    jobs,
    load_job,
    public_job_record,
    worker_context,
)
from app.repositories.outbox_repository import enqueue_wakeup
from app.security.authorization import lock_scope, require_repository
from app.services.persistence import PersistenceService


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
# ASCII namespace CSPY2, shared by all broker processes, not an in-memory semaphore.
CLAIM_CAPACITY_LOCK = 0x4353505932


class JobService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], *, lease_seconds: float = 90):
        if not 0 < lease_seconds <= 90:
            raise ValueError("invalid lease duration")
        self.factory = factory
        self.lease_seconds = lease_seconds
        self.persistence = PersistenceService(factory)

    async def enqueue(
        self,
        ctx: TenantContext,
        request: (
            EnqueueJobV2
            | EnqueueJobV3
            | EnqueueScanJobV4
            | EnqueueIndexJobV5
            | EnqueueInvestigationJobV1
        ),
    ) -> JobRecordV2 | JobRecordV3 | ScanJobRecordV4 | IndexJobRecordV5 | InvestigationJobRecordV1:
        if request.request_key.startswith(("job:", "child:")):
            raise ValueError("reserved request namespace")
        async with transaction(self.factory) as s:
            await lock_scope(
                s, ctx.org_id, ctx.principal_id, request.repository_id, request.snapshot_id
            )
            row = await self._enqueue(s, ctx, request)
        if row.get("operation") in {"scan_snapshot", "index_snapshot", "investigate_snapshot"}:
            return public_job_record(row)
        if isinstance(request, EnqueueJobV3) or row.get("operation") == "analyze_structure":
            return job_record_v3(row)
        return job_record(row)

    async def _enqueue(
        self,
        s: AsyncSession,
        ctx: TenantContext,
        request: (
            EnqueueJobV2
            | EnqueueJobV3
            | EnqueueScanJobV4
            | EnqueueIndexJobV5
            | EnqueueInvestigationJobV1
        ),
    ) -> dict[str, Any]:
        digest = semantic_digest(request.model_dump())
        target_jobs = jobs
        if isinstance(request, EnqueueIndexJobV5):
            from app.models.index_tables import index_jobs

            target_jobs = index_jobs
        elif isinstance(request, EnqueueInvestigationJobV1):
            from app.models.investigation_execution_tables import investigation_jobs

            target_jobs = investigation_jobs
        elif isinstance(request, EnqueueScanJobV4):
            from app.models.scan_tables import scan_jobs

            target_jobs = scan_jobs
        elif isinstance(request, EnqueueJobV3):
            from app.models.phase3_tables import structural_jobs

            target_jobs = structural_jobs
        old = (
            (
                await s.execute(
                    select(target_jobs).where(
                        target_jobs.c.org_id == ctx.org_id,
                        target_jobs.c.request_key == request.request_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if old:
            await require_repository(
                s, ctx.org_id, ctx.principal_id, old["repository_id"], write=True
            )
            if old["request_hash"] != digest or (
                request.operation in {"scan_snapshot", "index_snapshot"}
                and old["principal_id"] != ctx.principal_id
            ):
                raise DuplicateConflict("request key has different input")
            return dict(old)
        limits = PHASE2_TABLES["org_work_limits"]
        cap = await s.scalar(select(limits.c.max_pending_jobs).where(limits.c.org_id == ctx.org_id))
        pending = await s.scalar(
            select(func.count())
            .select_from(jobs)
            .where(jobs.c.org_id == ctx.org_id, jobs.c.state.not_in(TERMINAL))
        )
        assert pending is not None and cap is not None
        if pending >= cap:
            raise CapacityExceeded("pending job quota reached")
        row: dict[str, Any] = dict(
            (
                await s.execute(
                    insert(target_jobs)
                    .values(
                        org_id=ctx.org_id,
                        id=uuid4(),
                        principal_id=ctx.principal_id,
                        request_hash=digest,
                        deadline=func.clock_timestamp() + text("interval '15 minutes'"),
                        **request.model_dump(),
                    )
                    .returning(target_jobs)
                )
            )
            .mappings()
            .one()
        )
        await enqueue_wakeup(s, row)
        await append_event(s, row, "queued")
        return row

    async def get(
        self, ctx: TenantContext, job_id: UUID
    ) -> JobRecordV2 | JobRecordV3 | ScanJobRecordV4 | IndexJobRecordV5 | InvestigationJobRecordV1:
        async with transaction(self.factory) as s:
            row = await load_job(s, ctx.org_id, job_id)
            await require_repository(s, ctx.org_id, ctx.principal_id, row["repository_id"])
            if row["operation"] == "admit_source":
                row["inventory_job_id"] = await s.scalar(
                    select(jobs.c.id)
                    .where(
                        jobs.c.org_id == ctx.org_id,
                        jobs.c.parent_job_id == job_id,
                        jobs.c.operation == "inventory_snapshot",
                    )
                    .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
                    .limit(1)
                )
        if row.get("operation") in {"scan_snapshot", "index_snapshot", "investigate_snapshot"}:
            return public_job_record(row)
        if row.get("operation") == "analyze_structure":
            return job_record_v3(row)
        return job_record(row)

    async def _locked(
        self, s: AsyncSession, org_id: UUID, job_id: UUID, *, access: bool = True
    ) -> tuple[dict[str, Any], TenantContext | None]:
        hint = await load_job(s, org_id, job_id)
        ctx = await lock_scope(
            s,
            org_id,
            hint["principal_id"],
            hint["repository_id"],
            hint["snapshot_id"],
            access=access,
        )
        return await load_job(s, org_id, job_id, lock=True), ctx

    async def _current(
        self, s: AsyncSession, lease: AttemptLeaseV2, job: dict[str, Any], *, live: bool = True
    ) -> dict[str, Any]:
        row = (
            (
                await s.execute(
                    select(attempts)
                    .where(
                        attempts.c.org_id == lease.org_id,
                        attempts.c.job_id == lease.job_id,
                        attempts.c.id == lease.attempt_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        now = await database_now(s)
        if (
            row is None
            or job["state"] != "running"
            or job["current_attempt_id"] != lease.attempt_id
            or job["fence"] != lease.fence
            or row["fence"] != lease.fence
            or row["owner"] != lease.owner
            or row["state"] != "active"
            or job["cancel_requested_at"] is not None
            or (live and (row["lease_expires_at"] <= now or job["deadline"] <= now))
        ):
            raise StaleAttempt("attempt no longer owns this job")
        expected = (
            RunRef(org_id=lease.org_id, snapshot_id=row["run_snapshot_id"], run_id=row["run_id"])
            if row["run_id"] is not None
            else None
        )
        if lease.run_ref != expected:
            raise StaleAttempt("attempt run binding differs")
        return dict(row)

    async def claim(
        self, org_id: UUID, job_id: UUID, generation: int, owner: str
    ) -> AttemptLeaseV2 | str:
        if not owner or len(owner.encode()) > 128:
            raise ValueError("invalid worker owner")
        async with transaction(self.factory) as s:
            await s.execute(select(func.pg_advisory_xact_lock(CLAIM_CAPACITY_LOCK)))
            try:
                job, ctx = await self._locked(s, org_id, job_id)
            except ScopeViolation:
                return "unavailable"
            now = await database_now(s)
            if job["state"] in TERMINAL or generation != job["dispatch_generation"]:
                return "obsolete"
            if job["state"] == "running":
                return "already_leased"
            if job["state"] not in {"queued", "retry_wait"} or job["not_before"] > now:
                return "deferred"
            if job["deadline"] <= now:
                await self._finish_failure(s, job, None, "deadline_exceeded", transient=False)
                return "obsolete"
            active = await s.scalar(
                select(func.count()).select_from(attempts).where(attempts.c.state == "active")
            )
            org_active = await s.scalar(
                select(func.count())
                .select_from(attempts)
                .where(attempts.c.org_id == org_id, attempts.c.state == "active")
            )
            cap = await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.max_active_jobs).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == org_id
                )
            )
            assert active is not None and org_active is not None and cap is not None
            if active >= 2 or org_active >= cap:
                return "capacity"
            if job["attempt_count"] >= job["max_attempts"]:
                await self._finish_failure(s, job, None, "attempts_exhausted", transient=False)
                return "obsolete"
            assert ctx is not None
            attempt_id = uuid4()
            run_id = (
                uuid4()
                if job["operation"] in ("inventory_snapshot", "analyze_structure", "scan_snapshot")
                else None
            )
            fence = job["fence"] + 1
            row = (
                (
                    await s.execute(
                        insert(attempts)
                        .values(
                            org_id=org_id,
                            job_id=job_id,
                            id=attempt_id,
                            fence=fence,
                            owner=owner,
                            lease_expires_at=func.clock_timestamp()
                            + timedelta(seconds=self.lease_seconds),
                            heartbeat_at=func.clock_timestamp(),
                            run_snapshot_id=job["snapshot_id"] if run_id else None,
                            run_id=run_id,
                        )
                        .returning(attempts)
                    )
                )
                .mappings()
                .one()
            )
            target_jobs = jobs
            if job.get("operation") == "index_snapshot":
                from app.models.index_tables import index_jobs

                target_jobs = index_jobs
            elif job.get("operation") == "investigate_snapshot":
                from app.models.investigation_execution_tables import investigation_jobs

                target_jobs = investigation_jobs
            elif job.get("operation") == "scan_snapshot":
                from app.models.scan_tables import scan_jobs

                target_jobs = scan_jobs
            elif job.get("operation") == "analyze_structure":
                from app.models.phase3_tables import structural_jobs

                target_jobs = structural_jobs
            job = dict(
                (
                    await s.execute(
                        update(target_jobs)
                        .where(target_jobs.c.org_id == org_id, target_jobs.c.id == job_id)
                        .values(
                            state="running",
                            fence=fence,
                            current_attempt_id=attempt_id,
                            attempt_count=job["attempt_count"] + 1,
                            failure_code=None,
                        )
                        .returning(target_jobs)
                    )
                )
                .mappings()
                .one()
            )
            lease = AttemptLeaseV2(
                org_id=org_id,
                job_id=job_id,
                attempt_id=attempt_id,
                fence=fence,
                owner=owner,
                lease_expires_at=row["lease_expires_at"],
                heartbeat_at=row["heartbeat_at"],
                run_ref=RunRef(org_id=org_id, snapshot_id=job["snapshot_id"], run_id=run_id)
                if run_id
                else None,
            )
            await worker_context(s, lease)
            if job["operation"] == "index_snapshot":
                from app.models.index_tables import index_attempt_specs, index_specs

                spec_hash = await s.scalar(
                    select(index_specs.c.spec_hash).where(
                        index_specs.c.org_id == org_id, index_specs.c.id == job["index_spec_id"]
                    )
                )
                await s.execute(
                    insert(index_attempt_specs).values(
                        org_id=org_id,
                        id=attempt_id,
                        job_id=job_id,
                        fence=fence,
                        spec_id=job["index_spec_id"],
                        repository_id=job["repository_id"],
                        snapshot_id=job["snapshot_id"],
                        spec_hash=spec_hash,
                    )
                )
            elif job["operation"] == "investigate_snapshot" and job.get("investigation_id"):
                from app.models.investigation_tables import investigations

                await s.execute(
                    update(investigations)
                    .where(
                        investigations.c.org_id == org_id,
                        investigations.c.id == job["investigation_id"],
                        investigations.c.status.in_(["pending", "checkpointed"]),
                    )
                    .values(
                        status="running",
                        current_job_id=job_id,
                        worker_id=owner,
                        worker_lease_expires_at=row["lease_expires_at"],
                    )
                )
            if run_id:
                if job["operation"] == "scan_snapshot":
                    from app.contracts.scan_jobs import stored_config
                    from app.models.scan_tables import scan_specs

                    spec_row = (
                        (
                            await s.execute(
                                select(scan_specs).where(
                                    scan_specs.c.org_id == org_id,
                                    scan_specs.c.id == job["scan_spec_id"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    scan_config = stored_config(spec_row["config_json"])
                    analyzer_name = "codespy-findings"
                    run_config = scan_config.model_dump(mode="json")
                elif job["operation"] == "analyze_structure":
                    from app.models.phase3_tables import analysis_specs

                    spec_row = (
                        (
                            await s.execute(
                                select(analysis_specs).where(
                                    analysis_specs.c.org_id == org_id,
                                    analysis_specs.c.id == job["analysis_spec_id"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    analyzer_name = "codespy-structure"
                    run_config = dict(spec_row["config_json"])
                else:
                    analyzer_name = "source-inventory"
                    run_config = {"policy_hash": job["config_hash"]}

                run = await self.persistence._create_run_in_session(
                    s,
                    ctx,
                    RunCreate(
                        snapshot_id=job["snapshot_id"],
                        request_key=f"job:{job_id}:attempt:{job['attempt_count']}",
                        analyzer_name=analyzer_name,
                        analyzer_version="1",
                        config=run_config,
                    ),
                    run_id=run_id,
                    worker_binding=(job_id, attempt_id, fence),
                )
                await self.persistence._transition_in_session(s, ctx, run.ref, 0, "running")
                if job["operation"] == "scan_snapshot":
                    from app.repositories.scan_repository import ScanRepository

                    await ScanRepository(s).create_attempt_spec(
                        lease, job["scan_spec_id"], job["repository_id"], scan_config, now
                    )
            elif job["submission_id"]:
                sub = PHASE2_TABLES["source_submissions"]
                await s.execute(
                    update(sub)
                    .where(
                        sub.c.org_id == org_id,
                        sub.c.id == job["submission_id"],
                        sub.c.state == "admission_queued",
                    )
                    .values(state="admitting")
                )
            await append_event(s, job, "attempt_started")
        return lease

    async def heartbeat(self, lease: AttemptLeaseV2) -> AttemptLeaseV2:
        async with transaction(self.factory) as s:
            job = await load_job(s, lease.org_id, lease.job_id, lock=True)
            await self._current(s, lease, job)
            row = (
                (
                    await s.execute(
                        update(attempts)
                        .where(
                            attempts.c.org_id == lease.org_id,
                            attempts.c.job_id == lease.job_id,
                            attempts.c.id == lease.attempt_id,
                        )
                        .values(
                            heartbeat_at=func.clock_timestamp(),
                            lease_expires_at=func.clock_timestamp()
                            + timedelta(seconds=self.lease_seconds),
                        )
                        .returning(attempts)
                    )
                )
                .mappings()
                .one()
            )
        return lease.model_copy(
            update={
                "heartbeat_at": row["heartbeat_at"],
                "lease_expires_at": row["lease_expires_at"],
            }
        )

    async def _close_attempt(
        self,
        s: AsyncSession,
        job: dict[str, Any],
        attempt: dict[str, Any],
        code: str,
        *,
        cancel: bool = False,
    ) -> None:
        await s.execute(
            update(attempts)
            .where(
                attempts.c.org_id == job["org_id"],
                attempts.c.job_id == job["id"],
                attempts.c.id == attempt["id"],
            )
            .values(
                state="cancelled" if cancel else "failed",
                finished_at=func.clock_timestamp(),
                error_code=code,
            )
        )
        if attempt["run_id"] is not None:
            await s.execute(text("SELECT set_config('codespy.recovery','true',true)"))
            await s.execute(
                update(analysis_runs)
                .where(
                    analysis_runs.c.org_id == job["org_id"],
                    analysis_runs.c.snapshot_id == attempt["run_snapshot_id"],
                    analysis_runs.c.id == attempt["run_id"],
                    analysis_runs.c.status.in_(["queued", "running"]),
                )
                .values(
                    status="cancelled" if cancel else "failed",
                    write_version=analysis_runs.c.write_version + 1,
                    finished_at=func.clock_timestamp(),
                    failure_code=None if cancel else code,
                )
            )

    async def release_reservation(
        self,
        s: AsyncSession,
        job: dict[str, Any],
        state: str,
        code: str | None = None,
        *,
        snapshot_id: UUID | None = None,
    ) -> None:
        if job["operation"] != "admit_source" or job["submission_id"] is None:
            return
        sub = PHASE2_TABLES["source_submissions"]
        row = (
            (
                await s.execute(
                    select(sub)
                    .where(sub.c.org_id == job["org_id"], sub.c.id == job["submission_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if row["state"] in {"admitted", "rejected", "failed", "expired", "cancelled"}:
            return
        if row["reservation_released_at"] is None:
            limits = PHASE2_TABLES["org_work_limits"]
            await s.execute(
                update(limits)
                .where(limits.c.org_id == job["org_id"])
                .values(reserved_bytes=limits.c.reserved_bytes - row["reserved_bytes"])
            )
        await s.execute(
            update(sub)
            .where(sub.c.org_id == job["org_id"], sub.c.id == row["id"])
            .values(
                state=state,
                finished_at=func.clock_timestamp(),
                reservation_released_at=func.clock_timestamp(),
                error_code=code,
                upload_owner=None,
                upload_expires_at=None,
                snapshot_id=snapshot_id,
            )
        )
        if row["input_object_id"] is not None and state != "admitted":
            objects = PHASE2_TABLES["object_records"]
            await s.execute(
                update(objects)
                .where(
                    objects.c.org_id == job["org_id"],
                    objects.c.id == row["input_object_id"],
                    objects.c.state == "verified",
                )
                .values(
                    expires_at=func.least(
                        objects.c.expires_at, func.clock_timestamp() + text("interval '24 hours'")
                    )
                )
            )

    async def _finish_failure(
        self,
        s: AsyncSession,
        job: dict[str, Any],
        attempt: dict[str, Any] | None,
        code: str,
        *,
        transient: bool,
        cancel: bool = False,
    ) -> None:
        now = await database_now(s)
        if attempt is not None:
            await self._close_attempt(s, job, attempt, code, cancel=cancel)
        job_deadline = _ensure_utc(job["deadline"])
        retry = (
            transient
            and not cancel
            and job["attempt_count"] < job["max_attempts"]
            and job_deadline > now + timedelta(seconds=5)
        )
        if retry:
            delay = min(60, 5 * 2 ** max(0, job["attempt_count"] - 1))
            target_jobs = jobs
            if job.get("operation") == "index_snapshot":
                from app.models.index_tables import index_jobs

                target_jobs = index_jobs
            elif job.get("operation") == "investigate_snapshot":
                from app.models.investigation_execution_tables import investigation_jobs

                target_jobs = investigation_jobs
            elif job.get("operation") == "scan_snapshot":
                from app.models.scan_tables import scan_jobs

                target_jobs = scan_jobs
            elif job.get("operation") == "analyze_structure":
                from app.models.phase3_tables import structural_jobs

                target_jobs = structural_jobs
            job = dict(
                (
                    await s.execute(
                        update(target_jobs)
                        .where(target_jobs.c.org_id == job["org_id"], target_jobs.c.id == job["id"])
                        .values(
                            state="retry_wait",
                            current_attempt_id=None,
                            dispatch_generation=job["dispatch_generation"] + 1,
                            not_before=min(job["deadline"], now + timedelta(seconds=delay)),
                            failure_code=code,
                        )
                        .returning(target_jobs)
                    )
                )
                .mappings()
                .one()
            )
            if job["submission_id"] is not None and job["operation"] == "admit_source":
                sub = PHASE2_TABLES["source_submissions"]
                await s.execute(
                    update(sub)
                    .where(sub.c.org_id == job["org_id"], sub.c.id == job["submission_id"])
                    .values(state="admission_queued")
                )
            if job.get("operation") == "investigate_snapshot" and job.get("investigation_id"):
                from app.models.investigation_tables import investigations

                await s.execute(
                    update(investigations)
                    .where(
                        investigations.c.org_id == job["org_id"],
                        investigations.c.id == job["investigation_id"],
                        investigations.c.status == "running",
                    )
                    .values(status="checkpointed")
                )
            await enqueue_wakeup(s, job)
            await append_event(s, job, "retry_scheduled", {"error_code": code})
            return
        final_code = (
            "deadline_exceeded"
            if job_deadline <= now
            else ("attempts_exhausted" if transient else code)
        )
        state = "cancelled" if cancel else "failed"
        await s.execute(
            update(jobs)
            .where(jobs.c.org_id == job["org_id"], jobs.c.id == job["id"])
            .values(
                state=state,
                fence=job["fence"] + 1,
                finished_at=func.clock_timestamp(),
                failure_code=final_code,
                cancel_requested_at=func.clock_timestamp()
                if cancel
                else job["cancel_requested_at"],
            )
        )
        if job.get("operation") == "investigate_snapshot" and job.get("investigation_id"):
            from app.models.investigation_tables import investigations

            inv_state = (
                "cancelled"
                if state == "cancelled"
                else (
                    "awaiting_human_decision" if final_code == "model_outcome_unknown" else "failed"
                )
            )
            await s.execute(
                update(investigations)
                .where(
                    investigations.c.org_id == job["org_id"],
                    investigations.c.id == job["investigation_id"],
                    investigations.c.status.not_in(["completed", "failed", "cancelled"]),
                )
                .values(status=inv_state)
            )
        substate = (
            "cancelled"
            if cancel
            else (
                "expired"
                if final_code == "deadline_exceeded"
                else ("failed" if transient or final_code == "attempts_exhausted" else "rejected")
            )
        )
        await self.release_reservation(s, job, substate, final_code)
        await append_event(s, job, state, {"error_code": final_code})
        if state == "failed" and transient and job["attempt_count"] >= job["max_attempts"]:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            outbox = PHASE2_TABLES["outbox"]
            await s.execute(
                pg_insert(outbox)
                .values(
                    org_id=job["org_id"],
                    id=uuid4(),
                    job_id=job["id"],
                    kind="dead_letter",
                    generation=job["dispatch_generation"],
                    payload={
                        "schema_version": 2,
                        "org_id": str(job["org_id"]),
                        "job_id": str(job["id"]),
                        "fence": job["fence"] + 1,
                        "error_code": code,
                    },
                )
                .on_conflict_do_nothing()
            )

    async def fail_attempt(
        self, lease: AttemptLeaseV2, code: str, *, transient: bool = False
    ) -> None:
        allowed = {
            "access_revoked",
            "storage_unavailable",
            "queue_unavailable",
            "provider_unavailable",
            "admission_process_failed",
            "finalization_timeout",
            "invalid_archive",
            "unsafe_archive_path",
            "archive_corrupt",
            "archive_limit_exceeded",
            "unsupported_format",
            "integrity_failure",
            "internal_invariant",
            "index_process_failed",
            "index_cleanup_pending",
            "index_timeout",
            "index_runtime_unavailable",
            "investigation_timeout",
            "investigation_deadline_exceeded",
            "model_outcome_unknown",
            "model_result_unavailable",
            "context_budget_exceeded",
            "investigation_process_failed",
            "investigation_runtime_unavailable",
            "investigation_failed",
        }
        if code not in allowed:
            raise ValueError("unknown failure code")
        async with transaction(self.factory) as s:
            job, _ = await self._locked(s, lease.org_id, lease.job_id)
            if code.startswith("index_") and job["operation"] != "index_snapshot":
                raise ValueError("unknown failure code")
            if (
                code.startswith("investigation_")
                or code
                in {
                    "model_outcome_unknown",
                    "model_result_unavailable",
                    "context_budget_exceeded",
                }
            ) and job["operation"] != "investigate_snapshot":
                raise ValueError("unknown failure code")
            attempt = await self._current(s, lease, job)
            await self._finish_failure(
                s, job, attempt, code, transient=transient, cancel=code == "access_revoked"
            )

    async def request_cancel(
        self, ctx: TenantContext, job_id: UUID
    ) -> JobRecordV2 | JobRecordV3 | ScanJobRecordV4 | IndexJobRecordV5 | InvestigationJobRecordV1:
        """API cancellation fences work; broker alone closes an inventory run."""
        async with transaction(self.factory) as s:
            hint = await load_job(s, ctx.org_id, job_id)
            await lock_scope(s, ctx.org_id, ctx.principal_id, hint["repository_id"])
            job = await load_job(s, ctx.org_id, job_id, lock=True)
            if job["state"] in TERMINAL or job["state"] == "cancelling":
                return public_job_record(job)
            # Admission has no analysis run and may release its reservation here.
            if job["operation"] == "admit_source" or job["state"] != "running":
                attempt = (
                    (
                        await s.execute(
                            select(attempts)
                            .where(
                                attempts.c.org_id == ctx.org_id,
                                attempts.c.job_id == job_id,
                                attempts.c.id == job["current_attempt_id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                await self._finish_failure(
                    s,
                    job,
                    dict(attempt) if attempt else None,
                    "user_requested",
                    transient=False,
                    cancel=True,
                )
            else:
                await s.execute(
                    update(jobs)
                    .where(jobs.c.org_id == ctx.org_id, jobs.c.id == job_id)
                    .values(
                        state="cancelling",
                        fence=jobs.c.fence + 1,
                        cancel_requested_at=func.clock_timestamp(),
                    )
                )
                await append_event(s, job, "cancel_requested")
            return public_job_record(await load_job(s, ctx.org_id, job_id))

    async def cancel(
        self, ctx: TenantContext, job_id: UUID
    ) -> JobRecordV2 | JobRecordV3 | ScanJobRecordV4 | IndexJobRecordV5 | InvestigationJobRecordV1:
        async with transaction(self.factory) as s:
            hint = await load_job(s, ctx.org_id, job_id)
            await lock_scope(
                s, ctx.org_id, ctx.principal_id, hint["repository_id"], hint["snapshot_id"]
            )
            job = await load_job(s, ctx.org_id, job_id, lock=True)
            if job["state"] not in TERMINAL:
                attempt = (
                    (
                        await s.execute(
                            select(attempts)
                            .where(
                                attempts.c.org_id == ctx.org_id,
                                attempts.c.job_id == job_id,
                                attempts.c.id == job["current_attempt_id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                await self._finish_failure(
                    s,
                    job,
                    dict(attempt) if attempt else None,
                    "user_requested",
                    transient=False,
                    cancel=True,
                )
                job = await load_job(s, ctx.org_id, job_id)
        return public_job_record(job)

    async def recover_due(self, limit: int = 100) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("invalid sweep batch size")
        from app.repositories.index_recovery import index_recovery_due

        async with transaction(self.factory) as s:
            candidates = (
                await s.execute(
                    select(jobs.c.org_id, jobs.c.id)
                    .outerjoin(
                        attempts,
                        and_(
                            attempts.c.org_id == jobs.c.org_id,
                            attempts.c.job_id == jobs.c.id,
                            attempts.c.id == jobs.c.current_attempt_id,
                        ),
                    )
                    .where(
                        jobs.c.state.not_in(TERMINAL),
                        or_(
                            jobs.c.deadline <= func.clock_timestamp(),
                            jobs.c.state == "cancelling",
                            and_(
                                jobs.c.operation.in_(["index_snapshot", "investigate_snapshot"]),
                                jobs.c.cancel_requested_at.is_not(None),
                            ),
                            index_recovery_due(),
                            and_(
                                jobs.c.state == "running",
                                attempts.c.lease_expires_at <= func.clock_timestamp(),
                            ),
                        ),
                    )
                    .order_by(jobs.c.org_id, jobs.c.id)
                    .limit(limit)
                )
            ).all()
        changed = 0
        for org_id, job_id in candidates:
            async with transaction(self.factory) as s:
                job, _ = await self._locked(s, org_id, job_id, access=False)
                if job["state"] in TERMINAL:
                    continue
                now = await database_now(s)
                attempt = (
                    (
                        await s.execute(
                            select(attempts)
                            .where(
                                attempts.c.org_id == org_id,
                                attempts.c.job_id == job_id,
                                attempts.c.id == job["current_attempt_id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                attempt_lease_expiry = (
                    _ensure_utc(attempt["lease_expires_at"])
                    if attempt is not None and attempt["lease_expires_at"] is not None
                    else None
                )
                job_deadline = _ensure_utc(job["deadline"])
                expired = (
                    attempt is not None
                    and attempt["state"] == "active"
                    and attempt_lease_expiry is not None
                    and attempt_lease_expiry <= now
                )
                if job["operation"] in {"index_snapshot", "investigate_snapshot"}:
                    if job["cancel_requested_at"] is not None:
                        await self._finish_failure(
                            s,
                            job,
                            dict(attempt) if attempt else None,
                            "user_requested",
                            transient=False,
                            cancel=True,
                        )
                        changed += 1
                        continue
                    try:
                        await require_repository(
                            s,
                            org_id,
                            job["principal_id"],
                            job["repository_id"],
                            write=True,
                            lock=True,
                        )
                    except ScopeViolation:
                        await self._finish_failure(
                            s,
                            job,
                            dict(attempt) if attempt else None,
                            "access_revoked",
                            transient=False,
                        )
                        changed += 1
                        continue
                if job_deadline <= now or expired:
                    await self._finish_failure(
                        s,
                        job,
                        dict(attempt) if attempt else None,
                        "deadline_exceeded" if job_deadline <= now else "lease_expired",
                        transient=job_deadline > now,
                    )
                    changed += 1
        return changed
