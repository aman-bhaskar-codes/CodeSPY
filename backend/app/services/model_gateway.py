"""High-level model gateway orchestrating routing, reservations, dispatch,
validation, and settlement.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.model import (
    ChatMessageV1,
    ModelRequestV1,
    ModelResponseV1,
    ModelRoutingRequirementV1,
    RoutedModelV1,
    ToolDefinitionV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.ports.model import ModelPort
from app.security.authorization import require_repository
from app.services.model_budget import ModelBudgetService
from app.services.model_router import ModelRouter
from app.services.output_validator import OutputValidator

logger = logging.getLogger(__name__)


class ModelGatewayService:
    """Manages full model call lifecycle: policy routing, pre-dispatch reservation,
    dispatch, output validation, and usage settlement.
    """

    def __init__(
        self,
        router: ModelRouter,
        budget_service: ModelBudgetService,
        adapters: dict[str, ModelPort],
        *,
        accounting_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.router = router
        self.budget_svc = budget_service
        self.adapters = adapters
        self.accounting_factory = accounting_factory

    @asynccontextmanager
    async def _accounting(self) -> AsyncIterator[tuple[ModelRouter, ModelBudgetService]]:
        if self.accounting_factory is None:
            yield self.router, self.budget_svc
        else:
            async with transaction(self.accounting_factory) as session:
                budget = ModelBudgetService(session)
                yield ModelRouter(budget, catalog=self.router.catalog), budget

    async def _hold_failed_dispatch(self, org_id: UUID, reservation_id: UUID) -> None:
        try:
            async with asyncio.timeout(5):
                async with self._accounting() as (_, budget):
                    await budget.hold_uncertain(
                        org_id=org_id,
                        reservation_id=reservation_id,
                        failure_reason="provider_dispatch_failed",
                    )
        except (Exception, asyncio.CancelledError):
            # A committed dispatch still holds capacity if recovery persistence fails.
            logger.warning("provider_uncertainty_persistence_failed")

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
    ) -> ModelResponseV1:
        output_limit = requirement.estimated_output_tokens if max_tokens is None else max_tokens
        if isinstance(output_limit, bool) or not 1 <= output_limit <= 32768:
            raise ValueError("Invalid model output limit")
        bounded_requirement = requirement.model_copy(
            update={"estimated_output_tokens": output_limit}
        )
        request_digest = semantic_digest(
            {
                "identity_version": 1,
                "org_id": org_id,
                "repository_id": repository_id,
                "principal_id": principal_id,
                "messages": [message.model_dump(mode="json") for message in messages],
                "tools": [tool.model_dump(mode="json") for tool in tools or ()],
                "temperature": temperature,
                "max_tokens": output_limit,
                "response_schema": response_schema,
                "requirement": bounded_requirement.model_dump(mode="json"),
                "disclosure_policy": disclosure_policy.model_dump(mode="json"),
                "preferred_provider": preferred_provider,
            }
        )
        async with self._accounting() as (router, budget):
            if self.accounting_factory is not None:
                await require_repository(budget.s, org_id, principal_id, repository_id, write=True)
            routed: RoutedModelV1 = await router.route_and_reserve(
                org_id=org_id,
                repository_id=repository_id,
                principal_id=principal_id,
                investigation_id=investigation_id,
                logical_call_id=logical_call_id,
                attempt_id=attempt_id,
                requirement=bounded_requirement,
                disclosure_policy=disclosure_policy,
                idempotency_key=idempotency_key,
                preferred_provider=preferred_provider,
                request_digest=request_digest,
            )
            adapter = self.adapters.get(routed.provider)
            if not adapter or (routed.provider != "fake" and self.accounting_factory is None):
                await budget.release_reservation(
                    org_id=org_id,
                    reservation_id=routed.reservation_id,
                    failure_reason="provider_adapter_unavailable",
                )
                raise ValueError("Provider requires a registered adapter and durable accounting")

            request = ModelRequestV1(
                investigation_id=investigation_id,
                call_id=logical_call_id,
                attempt_id=attempt_id,
                provider=routed.provider,
                model_id=routed.model_id,
                messages=messages,
                tools=tools or (),
                temperature=temperature,
                max_tokens=output_limit,
                response_schema=response_schema,
                reservation_id=routed.reservation_id,
            )
            price = await budget.get_reserved_price(org_id, routed.reservation_id)
            await budget.commit_dispatch_intent(org_id, routed.reservation_id)
            await budget.mark_dispatched(org_id, routed.reservation_id)

        # Factory-backed accounting is committed and its connection closed before inference.
        try:
            response = await adapter.generate(request)
            actual_charge = price.calculate_cost_micros(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                thinking_tokens=response.usage.thinking_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_write_tokens=response.usage.cache_write_tokens,
            )
            async with self._accounting() as (_, budget):
                await budget.settle_reservation(
                    org_id=org_id,
                    reservation_id=routed.reservation_id,
                    actual_input_tokens=response.usage.input_tokens,
                    actual_output_tokens=response.usage.output_tokens,
                    actual_thinking_tokens=response.usage.thinking_tokens,
                    actual_micros=actual_charge,
                    provider_request_id=response.provider_request_id,
                )
        except (Exception, asyncio.CancelledError):
            logger.warning("provider_dispatch_failed")
            await self._hold_failed_dispatch(org_id, routed.reservation_id)
            raise

        # Invalid output still incurred the known first-call usage settled above.
        # A repair is a separate caller-owned request with its own reservation.
        if response_schema is not None:
            OutputValidator.validate_json_schema(response.content, response_schema)

        return response.model_copy(update={"charge_micros": actual_charge})
