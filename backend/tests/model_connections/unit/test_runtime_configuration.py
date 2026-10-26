"""Runtime construction never discovers credentials or silently enables egress."""

import httpx
import pytest
from app.contracts.model_configuration import ModelRegistryConfigV1, ModelTaskRouteV1
from app.core.model_config import ModelRuntimeSettings, build_model_runtime
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.model_connections.unit.test_registry import profile


@pytest.mark.asyncio
async def test_offline_runtime_builds_only_injected_clients():
    accesses = []

    async def secret(ref):
        accesses.append(ref)
        raise AssertionError("must remain lazy")

    p = profile()
    settings = ModelRuntimeSettings(
        registry=ModelRegistryConfigV1(
            profiles=(p,),
            task_routes=(ModelTaskRouteV1(task_type="short_path", aliases=(p.alias,)),),
        ),
        active_prices=((p.provider.value, p.model_id, p.price_version),),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as client:
        async with build_model_runtime(
            settings,
            accounting_factory=async_sessionmaker(),
            credentials=secret,
            http_client=client,
        ) as runtime:
            assert "openai" in runtime.transports
            assert accesses == []
            assert runtime.registry.execution_mode == "offline"
        assert not client.is_closed


@pytest.mark.asyncio
async def test_live_egress_requires_explicit_owner_approval():
    p = profile()
    settings = ModelRuntimeSettings(
        execution_mode="live", allow_live_egress=True, registry=ModelRegistryConfigV1(profiles=(p,))
    )
    with pytest.raises(ValueError):
        async with build_model_runtime(settings, accounting_factory=async_sessionmaker()):
            raise AssertionError("unapproved startup")


@pytest.mark.asyncio
async def test_disabled_runtime_constructs_no_clients():
    async with build_model_runtime(
        ModelRuntimeSettings(), accounting_factory=async_sessionmaker()
    ) as runtime:
        assert not runtime.transports


@pytest.mark.asyncio
async def test_live_registry_rejects_unqualified_serialized_input_bound():
    from app.contracts.model import ModelRoutingRequirementV1
    from app.services.model_registry import ModelRegistry, ModelRegistryError

    from tests.model_connections.unit.test_registry import policy

    p = profile(owner_approved_live=True)
    registry = ModelRegistry(
        ModelRegistryConfigV1(profiles=(p,)),
        registered_providers=frozenset({p.provider}),
        active_prices=frozenset({(p.provider.value, p.model_id, p.price_version)}),
        execution_mode="live",
    )
    with pytest.raises(ModelRegistryError):
        registry.resolve(ModelRoutingRequirementV1(estimated_context_tokens=1), policy(), p.alias)


async def test_runtime_builder_preserves_policy_mapping_and_memory_defaults():
    async with build_model_runtime(
        ModelRuntimeSettings(), accounting_factory=async_sessionmaker()
    ) as runtime:
        assert runtime.gateway().memory_context.mode == "disabled"
        assert runtime.disclosure_policies == {}


async def test_runtime_builder_keeps_memory_configuration_separate_from_disclosure():
    from uuid import uuid4

    from app.contracts.context_manifest import MemoryContextSettingsV1
    from app.contracts.provider_budget import DisclosurePolicyV1
    from app.core.model_config import RepositoryModelPolicyV1

    org, repo = uuid4(), uuid4()
    policy = DisclosurePolicyV1(allow_external_disclosure=False)
    settings = ModelRuntimeSettings(
        memory_context=MemoryContextSettingsV1(mode="shadow"),
        repository_policies=(
            RepositoryModelPolicyV1(org_id=org, repository_id=repo, policy=policy),
        ),
    )
    async with build_model_runtime(settings, accounting_factory=async_sessionmaker()) as runtime:
        assert runtime.gateway().memory_context.mode == "shadow"
        assert runtime.policy_for(org, repo) == policy


async def test_recovery_is_disabled_by_default_and_configurable():
    from app.contracts.accounting_recovery import AccountingRecoverySettingsV1

    settings = ModelRuntimeSettings()
    async with build_model_runtime(settings, accounting_factory=async_sessionmaker()) as runtime:
        assert runtime.accounting_recovery.enabled is False
    enabled = AccountingRecoverySettingsV1(enabled=True, max_provider_call_duration_seconds=120)
    configured = ModelRuntimeSettings(accounting_recovery=enabled)
    async with build_model_runtime(configured, accounting_factory=async_sessionmaker()) as runtime:
        assert runtime.accounting_recovery == enabled
