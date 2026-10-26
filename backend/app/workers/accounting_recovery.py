"""Bounded, queue-independent recovery for stale model gateway reservations."""

import logging
from datetime import timedelta

from app.contracts.accounting_recovery import (
    AccountingRecoverySettingsV1,
    AccountingRecoverySummaryV1,
)
from app.db.time import database_now
from app.db.transaction import transaction
from app.models.model_gateway_tables import model_reservations
from app.services.model_accounting_authority import (
    AuthorityModelBudgetService as ModelBudgetService,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class AccountingRecoveryWorker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: AccountingRecoverySettingsV1 | None = None,
    ) -> None:
        self.factory = factory
        self.settings = settings or AccountingRecoverySettingsV1()

    async def tick(self) -> AccountingRecoverySummaryV1:
        if not self.settings.enabled:
            return AccountingRecoverySummaryV1()

        stale_age = self.settings.stale_age_seconds
        batch_limit = self.settings.batch_limit

        # Discover due scopes using database time
        async with self.factory() as session:
            now = await database_now(session)
            cutoff = now - timedelta(seconds=stale_age)

            stmt = (
                select(
                    model_reservations.c.org_id,
                    model_reservations.c.repository_id,
                )
                .where(
                    model_reservations.c.state.in_(("dispatch_intent_committed", "dispatched")),
                    model_reservations.c.updated_at < cutoff,
                )
                .distinct()
                .limit(batch_limit)
            )
            scopes = (await session.execute(stmt)).all()

        scanned = 0
        held = 0
        failed_scopes = 0

        for org_id, repository_id in scopes:
            try:
                async with transaction(self.factory) as s:
                    budget = ModelBudgetService(s)
                    recovered = await budget.recover_dispatch_intents(
                        org_id=org_id,
                        repository_id=repository_id,
                        minimum_age_seconds=stale_age,
                        limit=batch_limit,
                    )
                    scanned += len(recovered)
                    held += len(recovered)
            except Exception:
                failed_scopes += 1
                logger.warning(
                    "accounting_recovery_scope_failed",
                    extra={"error_category": "dispatch_recovery_exception"},
                )

        continuation = len(scopes) >= batch_limit
        return AccountingRecoverySummaryV1(
            scanned=scanned,
            held=held,
            skipped=0,
            failed_scopes=failed_scopes,
            continuation_pending=continuation,
        )
