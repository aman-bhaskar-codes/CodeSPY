"""Current retained-source and deterministic finding-support checks."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation_execution import InvestigationScopeV1
from app.domain.errors import ScopeViolation
from app.domain.scope import RunRef
from app.models.scan_tables import finding_occurrences
from app.ports.investigation_execution import InvestigationSupportPort
from app.ports.scan_execution import ScanCapabilities
from app.security.authorization import require_repository
from app.services.analysis_sources import lock_retained_snapshot
from app.services.scan_reads import ScanReadService


class SqlInvestigationSupport(InvestigationSupportPort):
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        capabilities: ScanCapabilities | None = None,
    ) -> None:
        self.factory = factory
        self.capabilities = capabilities

    async def assert_current(self, session: AsyncSession, scope: InvestigationScopeV1) -> None:
        actor = await require_repository(
            session, scope.org_id, scope.principal_id, scope.repository_id, write=True
        )
        await lock_retained_snapshot(session, scope.org_id, scope.repository_id, scope.snapshot_id)
        if scope.finding_id is None:
            return
        if self.capabilities is None or scope.finding_run_id is None:
            raise ScopeViolation("investigation finding support unavailable")
        occurrence = await session.scalar(
            select(finding_occurrences.c.id).where(
                finding_occurrences.c.org_id == scope.org_id,
                finding_occurrences.c.snapshot_id == scope.snapshot_id,
                finding_occurrences.c.run_id == scope.finding_run_id,
                finding_occurrences.c.id == scope.finding_id,
            )
        )
        if occurrence is None:
            raise ScopeViolation("investigation finding scope differs")
        report = await ScanReadService(self.factory, self.capabilities).read_in_session(
            session,
            actor,
            RunRef(org_id=scope.org_id, snapshot_id=scope.snapshot_id, run_id=scope.finding_run_id),
        )
        if report is None or not any(
            d.finding_id == scope.finding_id and d.effective_disposition == "supported-static"
            for d in report.effective_decisions
        ):
            raise ScopeViolation("investigation finding support unavailable")
