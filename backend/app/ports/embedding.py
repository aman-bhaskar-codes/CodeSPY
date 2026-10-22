"""Typed port for embedding provider integrations."""

from typing import Protocol

from app.contracts.provider_budget import EmbeddingRequestV1, EmbeddingResponseV1


class EmbeddingPort(Protocol):
    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        """Generate vector embedding for the requested text."""
        ...
