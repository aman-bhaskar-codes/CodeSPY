"""Port protocol for vendor-neutral model inference."""

from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import UUID

from app.contracts.model import (
    ChatMessageV1,
    ModelRequestV1,
    ModelResponseV1,
    ModelRoutingRequirementV1,
    ModelStreamChunkV1,
    ToolDefinitionV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1


class ModelPort(Protocol):
    """Protocol for model inference adapters (Fake, Bedrock, Gemini)."""

    async def generate(self, request: ModelRequestV1) -> ModelResponseV1:
        """Execute non-streaming inference with structured/tool validation."""
        ...

    def stream(self, request: ModelRequestV1) -> AsyncIterator[ModelStreamChunkV1]:
        """Stream model inference chunks with usage and finalization."""
        ...


class ModelGatewayPort(Protocol):
    """Shared orchestration boundary for offline and configured provider gateways."""

    async def execute_call(
        self,
        org_id: UUID,
        repository_id: UUID,
        principal_id: UUID,
        investigation_id: UUID,
        logical_call_id: UUID,
        messages: tuple[ChatMessageV1, ...],
        requirement: ModelRoutingRequirementV1,
        disclosure_policy: DisclosurePolicyV1,
        idempotency_key: str,
        tools: tuple[ToolDefinitionV1, ...] | None = None,
        preferred_provider: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
        attempt_id: int = 1,
    ) -> ModelResponseV1: ...
