"""Worker operation for investigate_snapshot.

Executes sequential bounded investigation nodes through InvestigationExecutorPort.
Owns operation task lifecycle, heartbeat synchronization, and transitions to finalization.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationAdvanceV1,
    InvestigationExecutionContextV1,
    InvestigationNodeKind,
    InvestigationScopeV1,
    InvestigationStopReason,
    compute_node_id,
)
from app.contracts.jobs import AttemptLeaseV2
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.ports.investigation_execution import InvestigationExecutorPort
from app.repositories.investigation_execution_repository import (
    SqlInvestigationSpecRepository,
)
from app.repositories.job_repository import load_job
from app.services.jobs import JobService
from app.workers.report_memory import ReportMemoryProposalWorker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class InvestigateSnapshotOperation:
    """Executes investigate_snapshot worker steps sequentially."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        executor: InvestigationExecutorPort | None = None,
        finalizer: Any | None = None,
        spec_repo: SqlInvestigationSpecRepository | None = None,
        *,
        executor_factory: Callable[[AttemptLeaseV2], InvestigationExecutorPort] | None = None,
    ) -> None:
        if (executor is None) == (executor_factory is None):
            raise ValueError("exactly one executor or attempt factory required")
        self.memory_proposals: ReportMemoryProposalWorker | None = None
        self.executor_factory = executor_factory
        self.factory = factory
        self.executor = executor
        self.finalizer = finalizer
        self.spec_repo = spec_repo or SqlInvestigationSpecRepository()
        self.jobs = JobService(factory)

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> InvestigationAdvanceV1:
        # 1. Validate job & spec under transaction
        async with transaction(self.factory) as session:
            job = await load_job(session, lease.org_id, lease.job_id)
            if job["operation"] != "investigate_snapshot":
                raise DuplicateConflict(
                    f"operation mismatch: expected investigate_snapshot, got {job['operation']}"
                )
            inv_id: UUID | None = job.get("investigation_id")
            spec_hash: str | None = job.get("spec_hash")
            if inv_id is None or spec_hash is None:
                raise DuplicateConflict("investigation metadata missing on job")

            spec = await self.spec_repo.get_spec(session, lease.org_id, spec_hash)
            if spec is None:
                raise ScopeViolation(f"spec {spec_hash} not found")

            limits = spec.limits
            deadline = job["deadline"]
            repo_id = job["repository_id"]
            principal_id = job["principal_id"]
            snapshot_id = spec.snapshot_id

        scope = InvestigationScopeV1(
            org_id=lease.org_id,
            repository_id=repo_id,
            snapshot_id=snapshot_id,
            principal_id=principal_id,
            investigation_id=inv_id,
            spec_hash=spec_hash,
            execution_revision=spec.runtime_revision,
            finding_id=spec.finding_id,
            finding_run_id=spec.finding_run_id,
            chunk_set_id=spec.chunk_set_id,
        )

        executor = self.executor_factory(lease) if self.executor_factory else self.executor
        assert executor is not None

        # 2. Execution advance loop
        while True:
            node_id = compute_node_id(
                scope.org_id,
                scope.investigation_id,
                scope.spec_hash,
                InvestigationNodeKind.GATHER,
                "investigator",
                0,
            )
            ctx = InvestigationExecutionContextV1(
                scope=scope,
                logical_node_id=node_id,
                node_kind=InvestigationNodeKind.GATHER,
                role="investigator",
                logical_ordinal=0,
                attempt_job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                attempt_fence=lease.fence,
                deadline=deadline,
                limits=limits,
            )

            advance = await executor.advance(ctx)

            if advance.status == InvestigationAdvanceStatus.PROGRESS:
                continue

            elif advance.status == InvestigationAdvanceStatus.READY_TO_PUBLISH:
                if enter_finalization is not None:
                    await enter_finalization()
                if self.finalizer is not None:
                    if hasattr(self.finalizer, "publish"):
                        await self.finalizer.publish(lease, advance)
                    elif hasattr(self.finalizer, "finish"):
                        await self.finalizer.finish(lease, advance)
                return advance

            elif advance.status == InvestigationAdvanceStatus.NEEDS_REVIEW:
                await self.jobs.fail_attempt(
                    lease,
                    "model_outcome_unknown",
                    transient=False,
                )
                return advance

            elif advance.status == InvestigationAdvanceStatus.TERMINAL_FAILURE:
                fail_code = (
                    "deadline_exceeded"
                    if advance.stop_reason == InvestigationStopReason.DEADLINE_EXCEEDED
                    else (
                        "user_requested"
                        if advance.stop_reason == InvestigationStopReason.CANCELLED
                        else "investigation_process_failed"
                    )
                )
                await self.jobs.fail_attempt(
                    lease,
                    fail_code,
                    transient=False,
                )
                return advance

            else:
                raise RuntimeError(f"unexpected advance status: {advance.status}")
