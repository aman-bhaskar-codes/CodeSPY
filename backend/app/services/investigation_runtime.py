"""Explicit attempt-local investigation composition over existing services."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.jobs import AttemptLeaseV2
from app.db.time import database_now
from app.models.phase2_tables import attempts, jobs
from app.ports.investigation_execution import (
    InvestigationSupportPort,
    InvestigationToolPort,
    ModelDispatchAuthorityPort,
    ModelResultStorePort,
)
from app.repositories.investigation_execution_repository import (
    SqlInvestigationEvidenceRepository,
    SqlInvestigationExecutionCheckpointRepository,
    SqlInvestigationSpecRepository,
    SqlInvestigationStepReceiptRepository,
)
from app.security.authorization import lock_scope
from app.services.configured_model_gateway import ModelConnectionError
from app.services.investigation_executor import (
    InvestigationExecutorService,
    SqlInvestigationJobAuthority,
)
from app.services.investigation_support import SqlInvestigationSupport
from app.services.investigation_verification import InvestigationVerificationService
from app.services.model_runtime import ConfiguredModelRuntime
from app.workers.investigation_finalizer import InvestigationFinalizer
from app.workers.operations.investigate_snapshot import InvestigateSnapshotOperation


class BoundModelDispatchAuthority(ModelDispatchAuthorityPort):
    """Check the exact attempt inside the gateway dispatch-marker transaction."""

    def __init__(self, lease: AttemptLeaseV2, support: InvestigationSupportPort) -> None:
        self.lease = lease
        self.support = support
        self.authority = SqlInvestigationJobAuthority()

    async def assert_dispatch_allowed(
        self,
        session: AsyncSession,
        investigation_id: UUID,
        logical_call_id: UUID,
    ) -> None:
        repo = await session.scalar(
            select(jobs.c.repository_id).where(
                jobs.c.org_id == self.lease.org_id, jobs.c.id == self.lease.job_id
            )
        )
        if repo is None:
            raise ModelConnectionError("investigation_dispatch_denied")
        principal = await session.scalar(
            select(jobs.c.principal_id).where(
                jobs.c.org_id == self.lease.org_id, jobs.c.id == self.lease.job_id
            )
        )
        if principal is None:
            raise ModelConnectionError("investigation_dispatch_denied")
        await lock_scope(session, self.lease.org_id, principal, repo)
        current = await self.authority.assert_current(session, self.lease, lock=True)
        if (
            current.scope.investigation_id != investigation_id
            or current.is_cancelled
            or current.is_expired
        ):
            raise ModelConnectionError("investigation_dispatch_denied")
        state = await session.scalar(
            select(jobs.c.state).where(
                jobs.c.org_id == self.lease.org_id, jobs.c.id == self.lease.job_id
            )
        )
        owner = await session.scalar(
            select(attempts.c.owner).where(
                attempts.c.org_id == self.lease.org_id,
                attempts.c.id == self.lease.attempt_id,
                attempts.c.job_id == self.lease.job_id,
            )
        )
        expiry = await session.scalar(
            select(attempts.c.lease_expires_at).where(
                attempts.c.org_id == self.lease.org_id,
                attempts.c.id == self.lease.attempt_id,
                attempts.c.job_id == self.lease.job_id,
            )
        )
        now = await database_now(session)
        if expiry is None or expiry <= now or current.deadline <= now:
            raise ModelConnectionError("investigation_dispatch_denied")
        await self.support.assert_current(session, current.scope)
        if state != "running" or owner != self.lease.owner:
            raise ModelConnectionError("investigation_dispatch_denied")


def build_investigation_operation(
    *,
    runtime: ConfiguredModelRuntime,
    worker_factory: async_sessionmaker[AsyncSession],
    tools: InvestigationToolPort,
    result_store: ModelResultStorePort,
    propose_report_memory: bool = False,
) -> InvestigateSnapshotOperation:
    """Compose one worker operation; create a fresh gateway for every attempt."""

    if propose_report_memory and runtime.source_reader is None:
        raise ValueError("report memory proposals require an isolated source reader")
    support = SqlInvestigationSupport(worker_factory, runtime.scan_capabilities)

    def executor(lease: AttemptLeaseV2) -> InvestigationExecutorService:
        return InvestigationExecutorService(
            gateway=runtime.gateway(
                result_store=result_store,
                dispatch_authority=BoundModelDispatchAuthority(lease, support),
            ),
            tools=tools,
            authority=SqlInvestigationJobAuthority(),
            checkpoint_repo=SqlInvestigationExecutionCheckpointRepository(),
            step_receipt_repo=SqlInvestigationStepReceiptRepository(),
            session_factory=worker_factory,
            spec_repo=SqlInvestigationSpecRepository(),
            result_store=result_store,
            verification_service=InvestigationVerificationService(),
            evidence_repo=SqlInvestigationEvidenceRepository(),
            bound_lease=lease,
            policy_resolver=lambda scope: runtime.policy_for(scope.org_id, scope.repository_id),
        )

    operation = InvestigateSnapshotOperation(
        worker_factory,
        executor_factory=executor,
        finalizer=InvestigationFinalizer(
            worker_factory, source_support=support, memory_proposals=propose_report_memory
        ),
    )

    if propose_report_memory:
        from app.workers.report_memory import ReportMemoryProposalWorker

        operation.memory_proposals = ReportMemoryProposalWorker(
            worker_factory, runtime.source_reader, runtime.scan_capabilities
        )
    return operation
