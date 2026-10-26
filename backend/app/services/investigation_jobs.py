"""Durable investigation job and command service.

Implements create, start, cancel, resume, and retry commands with atomic transactions,
durable idempotency receipts, one-active-job enforcement, and lifecycle state transitions.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation import (
    InvestigationStatus,
)
from app.contracts.investigation_execution import (
    InvestigationLimitsV1,
    InvestigationSpecV1,
)
from app.contracts.investigation_jobs import (
    EnqueueInvestigationJobV1,
    InvestigationCancelResponseV1,
    InvestigationCreateRequestV2,
    InvestigationResumeRequestV1,
    InvestigationRetryRequestV1,
    InvestigationStartResponseV1,
    InvestigationSummaryV2,
)
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, InvalidTransition, ScopeViolation
from app.domain.scope import TenantContext
from app.models.investigation_execution_tables import investigation_jobs
from app.models.investigation_tables import investigations
from app.repositories.investigation_execution_repository import (
    SqlInvestigationCommandRepository,
    SqlInvestigationSpecRepository,
)
from app.security.authorization import lock_scope, require_repository
from app.services.jobs import JobService

TERMINAL_INVESTIGATION_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_JOB_STATES = {"succeeded", "partial", "failed", "cancelled"}


class InvestigationJobService:
    """Orchestrates atomic investigation commands and durable job dispatch."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory
        self.job_service = JobService(factory)

    async def create(
        self,
        ctx: TenantContext,
        request: InvestigationCreateRequestV2,
        *,
        request_key: str,
    ) -> InvestigationSummaryV2:
        """Create a new pending investigation with frozen spec and idempotency receipt."""
        if not request_key or len(request_key.encode("utf-8")) > 128:
            raise ValueError("invalid request_key: must be between 1 and 128 bytes")

        request_digest = semantic_digest(
            {
                "principal_id": str(ctx.principal_id),
                "repository_id": str(request.repository_id),
                "snapshot_id": str(request.snapshot_id),
                **request.model_dump(mode="json"),
            }
        )

        async with transaction(self.factory) as s:
            cmd_repo = SqlInvestigationCommandRepository()
            spec_repo = SqlInvestigationSpecRepository()

            # Check idempotency
            replayed = await cmd_repo.replay(
                s,
                ctx.org_id,
                ctx.principal_id,
                "create",
                request_key,
                request_digest,
            )
            if replayed is not None:
                return InvestigationSummaryV2.model_validate(replayed, strict=False)

            # Check repo write access
            await lock_scope(
                s,
                ctx.org_id,
                ctx.principal_id,
                request.repository_id,
                request.snapshot_id,
            )
            await require_repository(
                s, ctx.org_id, ctx.principal_id, request.repository_id, write=True, lock=True
            )

            if request.target_type not in ("repository", "finding"):
                raise ScopeViolation(f"unsupported target type: {request.target_type}")

            limits = InvestigationLimitsV1(
                max_steps=request.max_steps,
                budget_limit_micros=request.budget_limit_micros,
            )
            mode_str: Any = (
                request.mode.value if hasattr(request.mode, "value") else str(request.mode)
            )
            depth_str: Any = (
                request.depth.value if hasattr(request.depth, "value") else str(request.depth)
            )

            spec = InvestigationSpecV1(
                org_id=ctx.org_id,
                repository_id=request.repository_id,
                snapshot_id=request.snapshot_id,
                principal_id=ctx.principal_id,
                target_type=request.target_type,
                finding_id=request.finding_id,
                finding_run_id=request.finding_run_id,
                chunk_set_id=request.chunk_set_id,
                query=request.query,
                mode=mode_str,
                depth=depth_str,
                limits=limits,
                disclosure_policy_digest="0" * 64,
                tool_policy_revision="v1_standard",
                model_route_revision="v1_standard",
                model_profile_revision="v1_standard",
                runtime_revision=1,
                privacy_policy_revision="v1_standard",
            )
            spec_hash = spec.compute_spec_hash()

            # Insert spec if not already created
            existing_spec = await spec_repo.get_spec(s, ctx.org_id, spec_hash)
            if existing_spec is None:
                await spec_repo.save_spec(s, spec)

            investigation_id = uuid4()
            now = await database_now(s)

            await s.execute(
                insert(investigations).values(
                    org_id=ctx.org_id,
                    id=investigation_id,
                    repository_id=request.repository_id,
                    snapshot_id=request.snapshot_id,
                    principal_id=ctx.principal_id,
                    finding_id=request.finding_id,
                    target_type=request.target_type,
                    status="pending",
                    mode=mode_str,
                    depth=depth_str,
                    query=request.query,
                    spec_hash=spec_hash,
                    current_step=0,
                    max_steps=request.max_steps,
                    budget_limit_micros=request.budget_limit_micros,
                    spent_micros=0,
                    next_event_sequence=1,
                    execution_revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )

            summary = InvestigationSummaryV2(
                id=investigation_id,
                org_id=ctx.org_id,
                repository_id=request.repository_id,
                snapshot_id=request.snapshot_id,
                finding_id=request.finding_id,
                target_type=request.target_type,
                query=request.query,
                status="pending",
                mode=mode_str,
                depth=depth_str,
                current_step=0,
                max_steps=request.max_steps,
                budget_limit_micros=request.budget_limit_micros,
                spent_micros=0,
                held_micros=0,
                spec_hash=spec_hash,
                current_job_id=None,
                execution_revision=1,
                created_at=now,
                updated_at=now,
            )

            # Record idempotency receipt
            await cmd_repo.record(
                session=s,
                org_id=ctx.org_id,
                command_kind="create",
                idempotency_key=request_key,
                request_digest=request_digest,
                principal_id=ctx.principal_id,
                investigation_id=investigation_id,
                job_id=None,
                status_code=201,
                response_json=summary.model_dump(mode="json"),
            )

            return summary

    async def start(
        self,
        ctx: TenantContext,
        investigation_id: UUID,
        *,
        request_key: str,
    ) -> InvestigationStartResponseV1:
        """Start an investigation by enqueuing a durable investigate_snapshot job."""
        if not request_key or len(request_key.encode("utf-8")) > 128:
            raise ValueError("invalid request_key")

        request_digest = semantic_digest(
            {
                "principal_id": str(ctx.principal_id),
                "investigation_id": str(investigation_id),
                "command": "start",
            }
        )

        async with transaction(self.factory) as s:
            cmd_repo = SqlInvestigationCommandRepository()

            # Check idempotency
            replayed = await cmd_repo.replay(
                s,
                ctx.org_id,
                ctx.principal_id,
                "start",
                request_key,
                request_digest,
            )
            if replayed is not None:
                return InvestigationStartResponseV1.model_validate(replayed, strict=False)

            # Lock investigation row
            q = (
                select(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.id == investigation_id,
                )
                .with_for_update()
            )
            inv = (await s.execute(q)).mappings().one_or_none()
            if inv is None:
                raise ScopeViolation("investigation not found")
            if inv["principal_id"] != ctx.principal_id:
                raise ScopeViolation("operation requires original requesting principal")

            # Check repository write access
            await lock_scope(
                s,
                ctx.org_id,
                ctx.principal_id,
                inv["repository_id"],
                inv["snapshot_id"],
            )
            await require_repository(
                s, ctx.org_id, ctx.principal_id, inv["repository_id"], write=True, lock=True
            )

            if inv["snapshot_id"] is None:
                raise ScopeViolation("investigation_snapshot_required")

            if inv["status"] in TERMINAL_INVESTIGATION_STATUSES:
                raise ScopeViolation(f"cannot start terminal investigation ({inv['status']})")

            # One active job constraint
            jobs_tbl = investigation_jobs
            active_job_count = await s.scalar(
                select(func.count())
                .select_from(jobs_tbl)
                .where(
                    jobs_tbl.c.org_id == ctx.org_id,
                    jobs_tbl.c.investigation_id == investigation_id,
                    jobs_tbl.c.state.not_in(TERMINAL_JOB_STATES),
                )
            )
            if active_job_count and active_job_count > 0:
                raise DuplicateConflict("investigation already has an active job")

            # Enqueue investigate_snapshot job
            enqueue_req = EnqueueInvestigationJobV1(
                repository_id=inv["repository_id"],
                snapshot_id=inv["snapshot_id"],
                investigation_id=investigation_id,
                spec_hash=inv["spec_hash"],
                request_key=f"inv:{investigation_id}:job:{uuid4()}",
                config_hash=inv["spec_hash"],
            )
            job_row = await self.job_service._enqueue(s, ctx, enqueue_req)
            job_id = job_row["id"]

            # Update investigation with current job
            await s.execute(
                update(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.id == investigation_id,
                )
                .values(
                    current_job_id=job_id,
                )
            )

            resp = InvestigationStartResponseV1(
                investigation_id=investigation_id,
                job_id=job_id,
                status=InvestigationStatus(inv["status"]),
                status_url=f"/api/v1/repositories/{inv['repository_id']}/investigations/{investigation_id}",
                events_url=f"/api/v1/repositories/{inv['repository_id']}/investigations/{investigation_id}/events",
            )

            # Record idempotency receipt
            await cmd_repo.record(
                session=s,
                org_id=ctx.org_id,
                command_kind="start",
                idempotency_key=request_key,
                request_digest=request_digest,
                principal_id=ctx.principal_id,
                investigation_id=investigation_id,
                job_id=job_id,
                status_code=202,
                response_json=resp.model_dump(mode="json"),
            )

            return resp

    async def cancel(
        self,
        ctx: TenantContext,
        investigation_id: UUID,
        *,
        request_key: str,
    ) -> InvestigationCancelResponseV1:
        """Cancel an investigation and its active job."""
        if not request_key or len(request_key.encode("utf-8")) > 128:
            raise ValueError("invalid request_key")

        request_digest = semantic_digest(
            {
                "principal_id": str(ctx.principal_id),
                "investigation_id": str(investigation_id),
                "command": "cancel",
            }
        )

        async with transaction(self.factory) as s:
            cmd_repo = SqlInvestigationCommandRepository()

            # Check idempotency
            replayed = await cmd_repo.replay(
                s,
                ctx.org_id,
                ctx.principal_id,
                "cancel",
                request_key,
                request_digest,
            )
            if replayed is not None:
                return InvestigationCancelResponseV1.model_validate(replayed, strict=False)

            # Lock investigation
            q = (
                select(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.id == investigation_id,
                )
                .with_for_update()
            )
            inv = (await s.execute(q)).mappings().one_or_none()
            if inv is None:
                raise ScopeViolation("investigation not found")

            # Check repository write access
            await lock_scope(s, ctx.org_id, ctx.principal_id, inv["repository_id"])
            await require_repository(
                s, ctx.org_id, ctx.principal_id, inv["repository_id"], write=True, lock=True
            )

            already_terminal = inv["status"] in TERMINAL_INVESTIGATION_STATUSES
            job_id = inv.get("current_job_id")

            if not already_terminal:
                if job_id is not None:
                    job_row = await self.job_service.request_cancel(ctx, job_id)
                    job_state = getattr(job_row, "state", None)
                    if job_state == "cancelled":
                        await s.execute(
                            update(investigations)
                            .where(
                                investigations.c.org_id == ctx.org_id,
                                investigations.c.id == investigation_id,
                            )
                            .values(status="cancelled")
                        )
                else:
                    await s.execute(
                        update(investigations)
                        .where(
                            investigations.c.org_id == ctx.org_id,
                            investigations.c.id == investigation_id,
                        )
                        .values(status="cancelled")
                    )

            resp = InvestigationCancelResponseV1(
                investigation_id=investigation_id,
                job_id=job_id,
                status=InvestigationStatus.CANCELLED,
                already_terminal=already_terminal,
            )

            await cmd_repo.record(
                session=s,
                org_id=ctx.org_id,
                command_kind="cancel",
                idempotency_key=request_key,
                request_digest=request_digest,
                principal_id=ctx.principal_id,
                investigation_id=investigation_id,
                job_id=job_id,
                status_code=200,
                response_json=resp.model_dump(mode="json"),
            )

            return resp

    async def resume(
        self,
        ctx: TenantContext,
        investigation_id: UUID,
        request: InvestigationResumeRequestV1,
        *,
        request_key: str,
    ) -> InvestigationStartResponseV1:
        """Resume an investigation from awaiting_human_decision state."""
        if not request_key or len(request_key.encode("utf-8")) > 128:
            raise ValueError("invalid request_key")

        request_digest = semantic_digest(
            {
                "principal_id": str(ctx.principal_id),
                "investigation_id": str(investigation_id),
                "command": "resume",
                **request.model_dump(mode="json"),
            }
        )

        async with transaction(self.factory) as s:
            cmd_repo = SqlInvestigationCommandRepository()

            replayed = await cmd_repo.replay(
                s,
                ctx.org_id,
                ctx.principal_id,
                "resume",
                request_key,
                request_digest,
            )
            if replayed is not None:
                return InvestigationStartResponseV1.model_validate(replayed, strict=False)

            q = (
                select(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.id == investigation_id,
                )
                .with_for_update()
            )
            inv = (await s.execute(q)).mappings().one_or_none()
            if inv is None:
                raise ScopeViolation("investigation not found")
            if inv["principal_id"] != ctx.principal_id:
                raise ScopeViolation("operation requires original requesting principal")

            await lock_scope(s, ctx.org_id, ctx.principal_id, inv["repository_id"])
            await require_repository(
                s, ctx.org_id, ctx.principal_id, inv["repository_id"], write=True, lock=True
            )

            if inv["status"] != "awaiting_human_decision":
                raise InvalidTransition(
                    f"investigation is in '{inv['status']}' state, expected awaiting_human_decision"
                )

            if inv["current_job_id"] != request.expected_failed_job_id:
                raise ScopeViolation("failed job id does not match current investigation job")

            if inv["spec_hash"] != request.expected_spec_hash:
                raise ScopeViolation("spec hash does not match current investigation spec")

            # Enqueue new job linked to old failed job
            enqueue_req = EnqueueInvestigationJobV1(
                repository_id=inv["repository_id"],
                snapshot_id=inv["snapshot_id"],
                investigation_id=investigation_id,
                spec_hash=inv["spec_hash"],
                request_key=f"inv:{investigation_id}:resume:{uuid4()}",
                config_hash=inv["spec_hash"],
                retry_of_job_id=inv["current_job_id"],
            )
            job_row = await self.job_service._enqueue(s, ctx, enqueue_req)
            job_id = job_row["id"]

            await s.execute(
                update(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.id == investigation_id,
                )
                .values(
                    current_job_id=job_id,
                    status="running",
                )
            )

            resp = InvestigationStartResponseV1(
                investigation_id=investigation_id,
                job_id=job_id,
                status=InvestigationStatus.RUNNING,
                status_url=f"/api/v1/repositories/{inv['repository_id']}/investigations/{investigation_id}",
                events_url=f"/api/v1/repositories/{inv['repository_id']}/investigations/{investigation_id}/events",
            )

            await cmd_repo.record(
                session=s,
                org_id=ctx.org_id,
                command_kind="resume",
                idempotency_key=request_key,
                request_digest=request_digest,
                principal_id=ctx.principal_id,
                investigation_id=investigation_id,
                job_id=job_id,
                status_code=202,
                response_json=resp.model_dump(mode="json"),
            )

            return resp

    async def retry(
        self,
        ctx: TenantContext,
        investigation_id: UUID,
        request: InvestigationRetryRequestV1,
        *,
        request_key: str,
    ) -> InvestigationSummaryV2:
        """Create a new pending investigation linked to the original terminal investigation."""
        if not request_key or len(request_key.encode("utf-8")) > 128:
            raise ValueError("invalid request_key")

        request_digest = semantic_digest(
            {
                "principal_id": str(ctx.principal_id),
                "investigation_id": str(investigation_id),
                "command": "retry",
                **request.model_dump(mode="json"),
            }
        )

        async with transaction(self.factory) as s:
            cmd_repo = SqlInvestigationCommandRepository()
            spec_repo = SqlInvestigationSpecRepository()

            replayed = await cmd_repo.replay(
                s,
                ctx.org_id,
                ctx.principal_id,
                "retry",
                request_key,
                request_digest,
            )
            if replayed is not None:
                return InvestigationSummaryV2.model_validate(replayed, strict=False)

            q = (
                select(investigations)
                .where(
                    investigations.c.org_id == ctx.org_id,
                    investigations.c.id == investigation_id,
                )
                .with_for_update()
            )
            inv = (await s.execute(q)).mappings().one_or_none()
            if inv is None:
                raise ScopeViolation("investigation not found")
            if inv["principal_id"] != ctx.principal_id:
                raise ScopeViolation("operation requires original requesting principal")

            await lock_scope(s, ctx.org_id, ctx.principal_id, inv["repository_id"])
            await require_repository(
                s, ctx.org_id, ctx.principal_id, inv["repository_id"], write=True, lock=True
            )

            if inv["status"] not in ("failed", "cancelled"):
                raise ScopeViolation(
                    f"investigation status is '{inv['status']}', expected failed or cancelled"
                )

            # Load original spec
            orig_spec = await spec_repo.get_spec(s, ctx.org_id, inv["spec_hash"])
            if orig_spec is None:
                raise ScopeViolation("original investigation spec not found")

            max_steps = request.max_steps or inv["max_steps"]
            budget_limit_micros = request.budget_limit_micros or inv["budget_limit_micros"]

            new_limits = InvestigationLimitsV1(
                max_steps=max_steps,
                budget_limit_micros=budget_limit_micros,
            )
            new_spec = orig_spec.model_copy(
                update={
                    "limits": new_limits,
                }
            )
            new_spec_hash = new_spec.compute_spec_hash()

            existing_new_spec = await spec_repo.get_spec(s, ctx.org_id, new_spec_hash)
            if existing_new_spec is None:
                await spec_repo.save_spec(s, new_spec)

            new_investigation_id = uuid4()
            now = await database_now(s)

            await s.execute(
                insert(investigations).values(
                    org_id=ctx.org_id,
                    id=new_investigation_id,
                    repository_id=inv["repository_id"],
                    snapshot_id=inv["snapshot_id"],
                    principal_id=ctx.principal_id,
                    finding_id=inv["finding_id"],
                    target_type=inv["target_type"],
                    status="pending",
                    mode=inv["mode"],
                    depth=inv["depth"],
                    query=inv["query"],
                    spec_hash=new_spec_hash,
                    current_step=0,
                    max_steps=max_steps,
                    budget_limit_micros=budget_limit_micros,
                    spent_micros=0,
                    next_event_sequence=1,
                    execution_revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )

            summary = InvestigationSummaryV2(
                id=new_investigation_id,
                org_id=ctx.org_id,
                repository_id=inv["repository_id"],
                snapshot_id=inv["snapshot_id"],
                finding_id=inv.get("finding_id"),
                target_type=orig_spec.target_type,
                query=inv["query"],
                status="pending",
                mode=inv["mode"],
                depth=inv["depth"],
                current_step=0,
                max_steps=max_steps,
                budget_limit_micros=budget_limit_micros,
                spent_micros=0,
                held_micros=0,
                spec_hash=new_spec_hash,
                current_job_id=None,
                retry_of_id=investigation_id,
                execution_revision=1,
                created_at=now,
                updated_at=now,
            )

            await cmd_repo.record(
                session=s,
                org_id=ctx.org_id,
                command_kind="retry",
                idempotency_key=request_key,
                request_digest=request_digest,
                principal_id=ctx.principal_id,
                investigation_id=new_investigation_id,
                job_id=None,
                status_code=201,
                response_json=summary.model_dump(mode="json"),
            )

            return summary
