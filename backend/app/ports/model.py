"""Port protocol for vendor-neutral model inference."""

from collections.abc import AsyncIterator
from typing import Protocol

from app.contracts.model import ModelRequestV1, ModelResponseV1, ModelStreamChunkV1


class ModelPort(Protocol):
    """Protocol for model inference adapters (Fake, Bedrock, Gemini)."""

    async def generate(self, request: ModelRequestV1) -> ModelResponseV1:
        """Execute non-streaming inference with structured/tool validation."""
        ...

    def stream(self, request: ModelRequestV1) -> AsyncIterator[ModelStreamChunkV1]:
        """Stream model inference chunks with usage and finalization."""
        ...
