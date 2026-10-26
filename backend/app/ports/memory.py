"""Repository memory boundaries shared by HTTP, workers and read-only tools."""

from typing import Protocol
from uuid import UUID

from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryEntryV1,
    MemoryQueryRequestV1,
    MemoryQueryResponseV1,
    MemoryReviewRequestV1,
)


class MemoryEvidencePort(Protocol):
    async def verify(
        self,
        org: UUID,
        repo: UUID,
        snapshot: UUID,
        actor: UUID,
        pointer: EvidencePointerV1,
    ) -> None: ...

    async def verify_current(
        self,
        org: UUID,
        repo: UUID,
        snapshot: UUID,
        actor: UUID,
        pointer: EvidencePointerV1,
    ) -> None: ...


class MemorySelectionPort(Protocol):
    async def query_memory(self, request: MemoryQueryRequestV1) -> MemoryQueryResponseV1: ...


class MemoryCommandPort(Protocol):
    async def record_memory(self, request: MemoryCreateRequestV1) -> MemoryEntryV1: ...

    async def review_memory(
        self,
        org: UUID,
        repo: UUID,
        actor: UUID,
        memory_id: UUID,
        request: MemoryReviewRequestV1,
    ) -> MemoryEntryV1: ...
