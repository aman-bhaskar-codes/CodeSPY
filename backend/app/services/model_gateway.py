"""High-level model gateway orchestrating routing, reservations, dispatch,
validation, and settlement.
"""

import logging
from typing import Any
from uuid import UUID

from app.contracts.model import (
    ChatMessageV1,
    ModelRequestV1,
    ModelResponseV1,
    ModelRole,
    ModelRoutingRequirementV1,
    RoutedModelV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.ports.model import ModelPort
from app.services.model_budget import ModelBudgetService
from app.services.model_router import ModelRouter
from app.services.output_validator import OutputValidationError, OutputValidator

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
    ) -> None:
        self.router = router
        self.budget_svc = budget_service
        self.adapters = adapters

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
        preferred_provider: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
        attempt_id: int = 1,
    ) -> ModelResponseV1:
        # Step 1: Policy-first route and reserve
        routed: RoutedModelV1 = await self.router.route_and_reserve(
            org_id=org_id,
            repository_id=repository_id,
            principal_id=principal_id,
            investigation_id=investigation_id,
            logical_call_id=logical_call_id,
            attempt_id=attempt_id,
            requirement=requirement,
            disclosure_policy=disclosure_policy,
            idempotency_key=idempotency_key,
            preferred_provider=preferred_provider,
        )

        adapter = self.adapters.get(routed.provider)
        if not adapter:
            await self.budget_svc.release_reservation(
                org_id=org_id,
                reservation_id=routed.reservation_id,
                failure_reason=f"No adapter registered for provider {routed.provider}",
            )
            raise ValueError(f"Unregistered provider adapter: {routed.provider}")

        # Step 2: Commit dispatch intent
        await self.budget_svc.commit_dispatch_intent(
            org_id=org_id, reservation_id=routed.reservation_id
        )

        # Step 3: Construct request and mark dispatched
        request = ModelRequestV1(
            investigation_id=investigation_id,
            call_id=logical_call_id,
            attempt_id=attempt_id,
            provider=routed.provider,
            model_id=routed.model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_schema=response_schema,
            reservation_id=routed.reservation_id,
        )

        await self.budget_svc.mark_dispatched(org_id=org_id, reservation_id=routed.reservation_id)

        # Step 4: Dispatch inference with uncertain hold guard
        try:
            response = await adapter.generate(request)
        except Exception as err:
            logger.warning("Model call failed during dispatch (holding uncertain charge): %s", err)
            await self.budget_svc.hold_uncertain(
                org_id=org_id,
                reservation_id=routed.reservation_id,
                failure_reason=str(err),
            )
            raise

        # Step 5: Optional Output Validation & Bounded Repair (max 1 repair attempt)
        if response_schema and response.content:
            try:
                OutputValidator.validate_json_schema(response.content, response_schema)
            except OutputValidationError as val_err:
                logger.info("Structured output failed validation: %s. Attempting repair.", val_err)
                repair_prompt = OutputValidator.create_repair_prompt(
                    response.content, str(val_err), response_schema
                )
                repair_messages = (
                    *messages,
                    ChatMessageV1(role=ModelRole.ASSISTANT, content=response.content),
                    ChatMessageV1(role=ModelRole.USER, content=repair_prompt),
                )
                repair_req = request.model_copy(
                    update={
                        "attempt_id": attempt_id + 1,
                        "messages": repair_messages,
                    }
                )
                try:
                    repaired_resp = await adapter.generate(repair_req)
                    OutputValidator.validate_json_schema(repaired_resp.content, response_schema)
                    response = response.model_copy(
                        update={
                            "content": repaired_resp.content,
                            "usage": response.usage.model_copy(
                                update={
                                    "input_tokens": response.usage.input_tokens
                                    + repaired_resp.usage.input_tokens,
                                    "output_tokens": response.usage.output_tokens
                                    + repaired_resp.usage.output_tokens,
                                }
                            ),
                        }
                    )
                except Exception as rep_err:
                    logger.warning("Schema repair attempt failed: %s", rep_err)

        # Step 6: Settle reservation based on actual observed usage
        price = await self.budget_svc.get_generation_price_schedule(
            provider=routed.provider,
            model_id=routed.model_id,
            version=routed.price_schedule_version,
        )

        actual_charge = price.calculate_cost_micros(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            thinking_tokens=response.usage.thinking_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
        )

        await self.budget_svc.settle_reservation(
            org_id=org_id,
            reservation_id=routed.reservation_id,
            actual_input_tokens=response.usage.input_tokens,
            actual_output_tokens=response.usage.output_tokens,
            actual_thinking_tokens=response.usage.thinking_tokens,
            actual_micros=actual_charge,
            provider_request_id=response.provider_request_id,
        )

        return response.model_copy(update={"charge_micros": actual_charge})
