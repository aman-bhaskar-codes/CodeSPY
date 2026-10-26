"""Short authorized fenced transactions around the existing checkpointer."""

from datetime import UTC
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation import (
    InvestigationEventV1,
    InvestigationStateV1,
    InvestigationSummaryV1,
)
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import ScopeViolation
from app.domain.scope import TenantContext
from app.models.investigation_tables import investigations
from app.security.authorization import require_repository
from app.services.investigation_checkpointer import (
    EventSink,
    FencedCheckpointer,
    StaleWorkerFencingError,
)


class TransactionalInvestigationCheckpointer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        actor: TenantContext,
        repository_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        event_sink: EventSink | None = None,
    ) -> None:
        self.factory = factory
        self.actor = actor
        self.repository_id = repository_id
        self.investigation_id = investigation_id
        self.worker_id = worker_id
        self.fencing_token = fencing_token
        self.event_sink = event_sink

    async def _guard(
        self, session: AsyncSession, org_id: UUID, investigation_id: UUID, operation: str
    ) -> None:
        if org_id != self.actor.org_id or investigation_id != self.investigation_id:
            raise ScopeViolation("investigation unavailable")
        await require_repository(
            session, org_id, self.actor.principal_id, self.repository_id, write=True
        )
        stmt = select(investigations).where(
            investigations.c.org_id == org_id,
            investigations.c.id == investigation_id,
            investigations.c.repository_id == self.repository_id,
        )
        writing = operation in {
            "append_event",
            "renew_lease",
            "save_checkpoint",
            "complete_investigation",
        }
        if writing:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise ScopeViolation("investigation unavailable")
        if writing:
            now = await database_now(session)
            expiry = row["worker_lease_expires_at"]
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if (
                row["worker_id"] != self.worker_id
                or row["fencing_token"] != self.fencing_token
                or (
                    row["status"] in {"completed", "cancelled", "failed"}
                    and operation != "append_event"
                )
                or (
                    row["status"] not in {"completed", "cancelled", "failed"}
                    and (expiry is None or expiry <= now)
                )
            ):
                raise StaleWorkerFencingError("investigation worker authority expired")

    async def get_investigation(self, org_id: UUID, investigation_id: UUID) -> dict[str, Any]:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "get_investigation")
            return await FencedCheckpointer(session, event_sink=self.event_sink).get_investigation(
                org_id=org_id,
                investigation_id=investigation_id,
            )

    async def get_investigation_summary(
        self, org_id: UUID, investigation_id: UUID
    ) -> InvestigationSummaryV1:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "get_investigation_summary")
            return await FencedCheckpointer(
                session, event_sink=self.event_sink
            ).get_investigation_summary(
                org_id=org_id,
                investigation_id=investigation_id,
            )

    async def renew_lease(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        lease_ttl_seconds: int = 30,
    ) -> None:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "renew_lease")
            return await FencedCheckpointer(session, event_sink=self.event_sink).renew_lease(
                org_id=org_id,
                investigation_id=investigation_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                lease_ttl_seconds=lease_ttl_seconds,
            )

    async def save_checkpoint(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        step_index: int,
        graph_node: str,
        state: InvestigationStateV1,
        new_spent_micros: int = 0,
    ) -> None:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "save_checkpoint")
            return await FencedCheckpointer(session, event_sink=self.event_sink).save_checkpoint(
                org_id=org_id,
                investigation_id=investigation_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                step_index=step_index,
                graph_node=graph_node,
                state=state,
                new_spent_micros=new_spent_micros,
            )

    async def load_latest_checkpoint(
        self, org_id: UUID, investigation_id: UUID
    ) -> tuple[int, InvestigationStateV1] | None:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "load_latest_checkpoint")
            return await FencedCheckpointer(
                session, event_sink=self.event_sink
            ).load_latest_checkpoint(
                org_id=org_id,
                investigation_id=investigation_id,
            )

    async def append_event(
        self,
        org_id: UUID,
        investigation_id: UUID,
        step_index: int,
        event_type: str,
        node_name: str,
        payload: dict[str, Any],
    ) -> UUID:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "append_event")
            return await FencedCheckpointer(session, event_sink=self.event_sink).append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=step_index,
                event_type=event_type,
                node_name=node_name,
                payload=payload,
            )

    async def list_events(
        self, org_id: UUID, investigation_id: UUID, after_step: int | None = None
    ) -> list[InvestigationEventV1]:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "list_events")
            return await FencedCheckpointer(session, event_sink=self.event_sink).list_events(
                org_id=org_id,
                investigation_id=investigation_id,
                after_step=after_step,
            )

    async def complete_investigation(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        verdict: str,
        verdict_confidence: float | None = None,
        summary: str | None = None,
    ) -> None:
        async with transaction(self.factory) as session:
            await self._guard(session, org_id, investigation_id, "complete_investigation")
            return await FencedCheckpointer(
                session, event_sink=self.event_sink
            ).complete_investigation(
                org_id=org_id,
                investigation_id=investigation_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                verdict=verdict,
                verdict_confidence=verdict_confidence,
                summary=summary,
            )
