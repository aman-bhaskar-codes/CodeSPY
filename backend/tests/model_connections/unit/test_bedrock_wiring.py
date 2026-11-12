"""Composition refuses to build without explicit live approval and resolves policies closed."""

from uuid import uuid4

import aioboto3
import pytest
from app.contracts.provider_budget import DisclosurePolicyV1
from app.core.bedrock_runtime import build_bedrock_wiring, repository_policy_resolver
from app.core.model_config import ModelRuntimeSettings, RepositoryModelPolicyV1


def session():
    return aioboto3.Session(
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",  # gitleaks:allow
        aws_secret_access_key="x" * 40,  # gitleaks:allow
    )


async def test_offline_settings_cannot_open_a_bedrock_client():
    with pytest.raises(ValueError, match="live execution"):
        async with build_bedrock_wiring(
            ModelRuntimeSettings(), session=session(), reserve_sessions=None, dispatch_sessions=None
        ):
            raise AssertionError("must not open")


async def test_live_settings_yield_a_wired_gate_without_any_network_call():
    settings = ModelRuntimeSettings(execution_mode="live", allow_live_egress=True)
    async with build_bedrock_wiring(
        settings, session=session(), reserve_sessions=object, dispatch_sessions=object
    ) as wiring:
        assert wiring.converse_client.retry_total_attempts == 1
        assert wiring.query_embedder.gate is wiring.gate


def test_unknown_repository_gets_the_refusing_default_policy():
    org, repo = uuid4(), uuid4()
    allowed = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("aws_bedrock",))
    settings = ModelRuntimeSettings(
        repository_policies=(
            RepositoryModelPolicyV1(org_id=org, repository_id=repo, policy=allowed),
        )
    )
    policy_for = repository_policy_resolver(settings)
    assert policy_for(org, repo) is allowed or policy_for(org, repo) == allowed
    assert not policy_for(uuid4(), uuid4()).is_provider_permitted("aws_bedrock")
