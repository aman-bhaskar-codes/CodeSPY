"""Offline admission and deterministic model-slot selection."""

import pytest
from app.contracts.model import ModelProvider, ModelRoutingRequirementV1
from app.contracts.model_configuration import (
    ModelProfileV1,
    ModelRegistryConfigV1,
    ModelTaskRouteV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.services.model_registry import ModelRegistry, ModelRegistryError
from pydantic import ValidationError


def profile(alias="openai_fast", **changes):
    provider = {
        "claude_fast": ModelProvider.BEDROCK_CLAUDE,
        "claude_reasoning": ModelProvider.BEDROCK_CLAUDE,
        "gemini_general": ModelProvider.GOOGLE_GEMINI,
    }.get(alias, ModelProvider.OPENAI)
    endpoint = {
        "openai": "openai_responses",
        "bedrock_claude": "bedrock_converse",
        "google_gemini": "gemini_generate_content",
    }[provider.value]
    args = {
        "alias": alias,
        "provider": provider,
        "model_id": f"synthetic-{alias}",
        "profile_revision": "test-v1",
        "enabled": True,
        "endpoint_kind": endpoint,
        "credential_ref": "SYNTHETIC_KEY",
        "processing_regions": ("approved-zone",),
        "source_region": "ap-south-1" if provider == ModelProvider.BEDROCK_CLAUDE else None,
        "max_context_tokens": 8192,
        "max_output_tokens": 2048,
        "price_version": "test-price-v1",
        "locally_qualified": True,
        "allowed_task_types": ("short_path",),
        "supports_tools": True,
        "supports_native_schema": True,
    }
    return ModelProfileV1(**{**args, **changes})


def registry(profiles, *, registered=None, prices=None, mode="offline"):
    return ModelRegistry(
        ModelRegistryConfigV1(
            profiles=tuple(profiles),
            task_routes=(
                ModelTaskRouteV1(task_type="short_path", aliases=tuple(p.alias for p in profiles)),
            ),
        ),
        registered_providers=frozenset(p.provider for p in profiles)
        if registered is None
        else registered,
        active_prices=frozenset((p.provider.value, p.model_id, p.price_version) for p in profiles)
        if prices is None
        else prices,
        execution_mode=mode,
    )


def policy(**changes):
    return DisclosurePolicyV1(
        **{
            **{
                "allow_external_disclosure": True,
                "allowed_providers": ("openai", "bedrock_claude", "google_gemini"),
                "allowed_regions": ("approved-zone",),
            },
            **changes,
        }
    )


def test_all_five_slots_resolve_exact_profile():
    for alias in (
        "openai_fast",
        "openai_reasoning",
        "claude_fast",
        "claude_reasoning",
        "gemini_general",
    ):
        candidate = profile(alias)
        resolved = registry([candidate]).resolve(
            ModelRoutingRequirementV1(estimated_context_tokens=100), policy(), requested_alias=alias
        )
        assert resolved.profile == candidate
        assert len(resolved.profile_digest) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"locally_qualified": False},
        {"processing_regions": ()},
        {"processing_regions": ("approved-zone", "unapproved-zone")},
        {"supports_tools": False},
        {"max_context_tokens": 100},
    ],
)
def test_unavailable_or_policy_incompatible_profile_fails_closed(changes):
    p = profile(**changes)
    with pytest.raises(ModelRegistryError):
        registry([p]).resolve(
            ModelRoutingRequirementV1(estimated_context_tokens=100, requires_tools=True), policy()
        )


def test_no_registered_adapter_or_active_price_is_unavailable():
    p = profile()
    for reg in (registry([p], registered=frozenset()), registry([p], prices=frozenset())):
        with pytest.raises(ModelRegistryError):
            reg.resolve(ModelRoutingRequirementV1(estimated_context_tokens=1), policy())


def test_strict_requested_alias_does_not_fall_through():
    a, b = profile(enabled=False), profile("gemini_general")
    reg = registry([a, b])
    req = ModelRoutingRequirementV1(estimated_context_tokens=1)
    assert reg.resolve(req, policy()).profile.alias == "gemini_general"
    with pytest.raises(ModelRegistryError):
        reg.resolve(req, policy(), requested_alias="openai_fast")
    with pytest.raises(ModelRegistryError):
        reg.resolve(req, policy(allow_external_disclosure=False))


def test_local_qualification_does_not_enable_live_egress():
    p = profile()
    with pytest.raises(ModelRegistryError):
        registry([p], mode="live").resolve(
            ModelRoutingRequirementV1(estimated_context_tokens=1), policy()
        )


def test_duplicate_alias_and_enabled_provider_model_pair_rejected():
    a = profile()
    for b in (a, profile("openai_reasoning", model_id=a.model_id)):
        with pytest.raises(ValidationError):
            ModelRegistryConfigV1(profiles=(a, b))


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint_kind": "bedrock_converse"},
        {"credential_ref": "secret value"},
        {"source_region": "us-east-1"},
        {"max_output_tokens": 32769},
    ],
)
def test_invalid_configuration_rejected(changes):
    with pytest.raises(ValidationError):
        profile(**changes)
