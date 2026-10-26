"""Pure profile admission; never resolve credentials or probe vendor services."""

from typing import Literal

from app.contracts.model import ModelProvider, ModelRoutingRequirementV1
from app.contracts.model_configuration import (
    ModelProfileV1,
    ModelRegistryConfigV1,
    ResolvedModelProfileV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.domain.canonical_json import semantic_digest


class ModelRegistryError(Exception):
    """No configured model can safely satisfy the requested operation."""

    def __init__(self, code: str = "model_unavailable") -> None:
        self.code = code
        super().__init__(code)


class ModelRegistry:
    def __init__(
        self,
        config: ModelRegistryConfigV1,
        *,
        registered_providers: frozenset[ModelProvider],
        active_prices: frozenset[tuple[str, str, str]],
        execution_mode: Literal["offline", "live"] = "offline",
    ) -> None:
        if execution_mode not in ("offline", "live"):
            raise ValueError("invalid execution mode")
        self.config = ModelRegistryConfigV1.model_validate(config.model_dump())
        self.registered_providers = frozenset(registered_providers)
        self.active_prices = frozenset(active_prices)
        self.execution_mode = execution_mode

    def resolve(
        self,
        requirement: ModelRoutingRequirementV1,
        policy: DisclosurePolicyV1,
        requested_alias: str | None = None,
    ) -> ResolvedModelProfileV1:
        profiles: dict[str, ModelProfileV1] = {p.alias: p for p in self.config.profiles}
        aliases: tuple[str, ...] = (
            (requested_alias,)
            if requested_alias is not None
            else next(
                (
                    route.aliases
                    for route in self.config.task_routes
                    if route.task_type == requirement.task_type
                ),
                (),
            )
        )
        for alias in aliases:
            profile = profiles.get(alias)
            if profile is not None and self._admissible(profile, requirement, policy):
                return ResolvedModelProfileV1(
                    profile=profile, profile_digest=semantic_digest(profile.model_dump(mode="json"))
                )
        raise ModelRegistryError()

    def _admissible(
        self,
        profile: ModelProfileV1,
        requirement: ModelRoutingRequirementV1,
        policy: DisclosurePolicyV1,
    ) -> bool:
        if not profile.enabled or not profile.locally_qualified:
            return False
        if self.execution_mode == "live" and (
            not profile.owner_approved_live
            or not profile.input_bound_qualified
            or (
                profile.provider == ModelProvider.GOOGLE_GEMINI
                and profile.reasoning_mode == "provider_default"
            )
        ):
            return False
        if profile.provider not in self.registered_providers:
            return False
        if (
            profile.provider.value,
            profile.model_id,
            profile.price_version,
        ) not in self.active_prices:
            return False
        if not policy.allow_external_disclosure or not policy.is_provider_permitted(
            profile.provider.value
        ):
            return False
        if not profile.processing_regions or not all(
            policy.is_region_permitted(r) for r in profile.processing_regions
        ):
            return False
        if requirement.required_region and (
            not policy.is_region_permitted(requirement.required_region)
            or any(r != requirement.required_region for r in profile.processing_regions)
        ):
            return False
        if requirement.task_type not in profile.allowed_task_types:
            return False
        if requirement.requires_tools and not profile.supports_tools:
            return False
        if requirement.requires_structured_output and not profile.supports_native_schema:
            return False
        output = requirement.estimated_output_tokens
        if profile.provider == ModelProvider.GOOGLE_GEMINI:
            output += profile.thinking_token_budget
        return (
            requirement.estimated_output_tokens <= profile.max_output_tokens
            and requirement.estimated_context_tokens + output <= profile.max_context_tokens
        )
