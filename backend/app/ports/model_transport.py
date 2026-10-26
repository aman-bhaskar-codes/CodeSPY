"""Explicit provider boundary: preparation is pure and invocation sends once."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.contracts.model import ModelRequestV1
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.model_transport import PreparedModelCallV1, ProviderOutcomeV1


class ProviderPreparationPort(Protocol):
    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1: ...


class ProviderCodecPort(ProviderPreparationPort, Protocol):
    def decode(
        self, request: ModelRequestV1, profile: ModelProfileV1, payload: dict[str, Any]
    ) -> ProviderOutcomeV1: ...


class ProviderTransportPort(Protocol):
    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1: ...

    async def invoke(
        self,
        prepared: PreparedModelCallV1,
        *,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> ProviderOutcomeV1: ...

    async def aclose(self) -> None: ...
