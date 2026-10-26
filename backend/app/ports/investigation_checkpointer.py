"""Typed checkpoint operations used by model-driven investigators."""

from typing import Any, Protocol
from uuid import UUID

from app.contracts.investigation import (
    InvestigationEventV1,
    InvestigationStateV1,
    InvestigationSummaryV1,
)


class InvestigationCheckpointerPort(Protocol):
    async def get_investigation(self, org_id: UUID, investigation_id: UUID) -> dict[str, Any]: ...

    async def get_investigation_summary(
        self, org_id: UUID, investigation_id: UUID
    ) -> InvestigationSummaryV1: ...

    async def renew_lease(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        lease_ttl_seconds: int = 30,
    ) -> None: ...

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
    ) -> None: ...

    async def load_latest_checkpoint(
        self, org_id: UUID, investigation_id: UUID
    ) -> tuple[int, InvestigationStateV1] | None: ...

    async def append_event(
        self,
        org_id: UUID,
        investigation_id: UUID,
        step_index: int,
        event_type: str,
        node_name: str,
        payload: dict[str, Any],
    ) -> UUID: ...

    async def list_events(
        self, org_id: UUID, investigation_id: UUID, after_step: int | None = None
    ) -> list[InvestigationEventV1]: ...

    async def complete_investigation(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        verdict: str,
        verdict_confidence: float | None = None,
        summary: str | None = None,
    ) -> None: ...
