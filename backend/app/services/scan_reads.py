"""Current support evaluation is separate from immutable historical verification."""

from datetime import datetime

from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.findings import DependencyFindingEntityV1, ScanPublicationDraftV1
from app.contracts.scan_publication import EffectiveVerificationV1, ScanReadResultV1, SupportReason
from app.db.time import database_now
from app.domain.errors import DuplicateConflict
from app.domain.scope import RunRef, TenantContext
from app.ports.scan_execution import ScanCapabilities
from app.repositories.scan_repository import ScanRepository
from app.services.analysis_sources import locate_source


class ScanReadService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], capabilities: ScanCapabilities):
        self.factory, self.capabilities = factory, capabilities

    async def read(self, ctx: TenantContext, ref: RunRef) -> ScanReadResultV1 | None:
        async with self.factory() as session, session.begin():
            draft = await ScanRepository(session).read(ctx, ref)
            if draft is None:
                return None
            now = await database_now(session)
            try:
                await locate_source(session, ref.org_id, draft.spec.repository_id, ref.snapshot_id)
                available = True
            except (NoResultFound, DuplicateConflict):
                available = False
            return ScanReadResultV1(
                historical=draft,
                checked_at=now,
                effective_decisions=effective_decisions(draft, available, self.capabilities, now),
            )


def effective_decisions(
    draft: ScanPublicationDraftV1, available: bool, capabilities: ScanCapabilities, now: datetime
) -> tuple[EffectiveVerificationV1, ...]:
    findings = {f.id: f for f in draft.findings}
    decisions = []
    for decision in draft.decisions:
        effective = decision.disposition
        reason: SupportReason = "current_support"
        if effective == "supported-static":
            if not available:
                effective, reason = "unresolved", "source_unavailable"
            elif isinstance(
                entity := findings[decision.finding_id].entity, DependencyFindingEntityV1
            ):
                request = draft.spec.dependency_request
                assert request is not None
                try:
                    bundle = capabilities.bundle(request.bundle_digest)
                    if bundle.digest() != request.bundle_digest:
                        raise ValueError("retained bundle differs")
                except (KeyError, ValueError):
                    effective, reason = "unresolved", "bundle_unavailable"
                else:
                    assessment = bundle.evaluate(entity.query, now=now)
                    if (
                        assessment.status != "matched"
                        or (entity.advisory_id, entity.record_digest) not in assessment.advisories
                    ):
                        effective, reason = "unresolved", "advisory_not_current"
        decisions.append(
            EffectiveVerificationV1(
                finding_id=decision.finding_id,
                historical_disposition=decision.disposition,
                effective_disposition=effective,
                reason=reason,
            )
        )
    return tuple(decisions)
