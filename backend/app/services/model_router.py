"""Policy-first model capability routing with disclosure enforcement and pre-dispatch
reservations.
"""

from collections.abc import Sequence
from uuid import UUID

from app.contracts.model import (
    ModelCapabilityV1,
    ModelProvider,
    ModelRoutingRequirementV1,
    RoutedModelV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.services.model_budget import ModelBudgetService


class ModelRoutingError(Exception):
    """Base error for model routing failures."""


class DisclosureViolationError(ModelRoutingError):
    """Model candidate violates data residency or disclosure policy."""


class ModelCapabilityMismatchError(ModelRoutingError):
    """No available model satisfies task capability requirements."""


DEFAULT_MODEL_CATALOG: tuple[ModelCapabilityV1, ...] = (
    # 1. Offline Deterministic Fake Provider (Always Qualified)
    ModelCapabilityV1(
        provider=ModelProvider.FAKE,
        model_id="codespy-fake-investigator-v1",
        revision="2026-10-22",
        capability_type="generation",
        max_context_tokens=128000,
        max_output_tokens=8192,
        supports_tools=True,
        supports_structured_output=True,
        supports_streaming=True,
        supports_thinking=False,
        geographic_regions=("local", "us-east-1", "ap-south-1"),
        requires_disclosure_approval=False,
        default_price_schedule_version="2026-10-22.1",
    ),
    # 2. AWS Bedrock Claude Candidate (India / Global Qualified)
    ModelCapabilityV1(
        provider=ModelProvider.BEDROCK_CLAUDE,
        model_id="in.anthropic.claude-haiku-4-5-20251001-v1:0",
        revision="2026-10-22",
        capability_type="generation",
        max_context_tokens=200000,
        max_output_tokens=8192,
        supports_tools=True,
        supports_structured_output=False,  # Fallback JSON mode for India profile
        supports_streaming=True,
        supports_thinking=False,
        geographic_regions=("in", "ap-south-1"),
        requires_disclosure_approval=True,
        default_price_schedule_version="2026-10-22.1",
    ),
    # 3. Google Gemini Candidate
    ModelCapabilityV1(
        provider=ModelProvider.GOOGLE_GEMINI,
        model_id="gemini-2.5-flash-lite",
        revision="2026-10-22",
        capability_type="generation",
        max_context_tokens=1048576,
        max_output_tokens=8192,
        supports_tools=True,
        supports_structured_output=True,
        supports_streaming=True,
        supports_thinking=True,
        geographic_regions=("global", "us-central1"),
        requires_disclosure_approval=True,
        default_price_schedule_version="2026-10-22.1",
    ),
)


class ModelRouter:
    """Enforces policy-first routing, capability validation, and atomic budget reservations."""

    def __init__(
        self,
        budget_service: ModelBudgetService,
        catalog: Sequence[ModelCapabilityV1] = DEFAULT_MODEL_CATALOG,
    ) -> None:
        self.budget_svc = budget_service
        self.catalog = list(catalog)

    async def route_and_reserve(
        self,
        org_id: UUID,
        repository_id: UUID,
        principal_id: UUID,
        investigation_id: UUID,
        logical_call_id: UUID,
        attempt_id: int,
        requirement: ModelRoutingRequirementV1,
        disclosure_policy: DisclosurePolicyV1,
        idempotency_key: str,
        preferred_provider: str | None = None,
        request_digest: str | None = None,
    ) -> RoutedModelV1:
        # Step 1: Filter candidates based on disclosure policy
        if requirement.required_region and not disclosure_policy.is_region_permitted(
            requirement.required_region
        ):
            raise DisclosureViolationError(
                f"Requested region '{requirement.required_region}' is not permitted "
                "by disclosure policy"
            )

        admissible_candidates: list[ModelCapabilityV1] = []
        for model in self.catalog:
            # Fake provider is admissible locally if regions match
            if model.provider == ModelProvider.FAKE:
                if (
                    requirement.required_region
                    and requirement.required_region not in model.geographic_regions
                ):
                    continue
                admissible_candidates.append(model)
                continue

            if not disclosure_policy.allow_external_disclosure:
                continue

            if not disclosure_policy.is_provider_permitted(model.provider.value):
                continue

            # Check geographic residency requirement
            if requirement.required_region:
                if requirement.required_region not in model.geographic_regions:
                    continue
            elif not any(
                disclosure_policy.is_region_permitted(r) for r in model.geographic_regions
            ):
                continue

            admissible_candidates.append(model)

        if not admissible_candidates:
            raise DisclosureViolationError(
                "No candidate model satisfies the repository disclosure and data residency policy"
            )

        # Step 2: Filter by task capability requirements
        capable_candidates: list[ModelCapabilityV1] = []
        for model in admissible_candidates:
            if requirement.requires_tools and not model.supports_tools:
                continue
            if (
                requirement.requires_structured_output
                and not model.supports_structured_output
                and model.provider != ModelProvider.FAKE
            ):
                continue
            if (
                requirement.estimated_output_tokens > model.max_output_tokens
                or requirement.estimated_context_tokens + requirement.estimated_output_tokens
                > model.max_context_tokens
            ):
                continue
            capable_candidates.append(model)

        if not capable_candidates:
            req_desc = (
                f"tools={requirement.requires_tools}, "
                f"schema={requirement.requires_structured_output}"
            )
            raise ModelCapabilityMismatchError(
                f"No admitted model satisfies required capabilities ({req_desc})"
            )

        # Step 3: Select best candidate (respecting preferred provider if admissible)
        selected_model = capable_candidates[0]
        if preferred_provider:
            for cand in capable_candidates:
                if cand.provider.value == preferred_provider:
                    selected_model = cand
                    break

        # Step 4: Validate price schedule & estimate worst-case cost
        price = await self.budget_svc.get_generation_price_schedule(
            provider=selected_model.provider.value,
            model_id=selected_model.model_id,
        )

        estimated_micros = price.calculate_cost_micros(
            input_tokens=requirement.estimated_context_tokens,
            output_tokens=requirement.estimated_output_tokens,
        )

        # Step 5: Reserve funds atomically
        reservation_id = await self.budget_svc.reserve_generation_budget(
            org_id=org_id,
            repository_id=repository_id,
            principal_id=principal_id,
            investigation_id=investigation_id,
            logical_call_id=logical_call_id,
            attempt_id=attempt_id,
            provider=selected_model.provider.value,
            model_id=selected_model.model_id,
            estimated_input_tokens=requirement.estimated_context_tokens,
            estimated_output_tokens=requirement.estimated_output_tokens,
            estimated_micros=estimated_micros,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            price_snapshot=price if request_digest is not None else None,
        )

        return RoutedModelV1(
            provider=selected_model.provider.value,
            model_id=selected_model.model_id,
            price_schedule_version=price.version,
            reservation_id=reservation_id,
            estimated_micros=estimated_micros,
            is_fallback=selected_model.provider.value != preferred_provider
            if preferred_provider
            else False,
        )
