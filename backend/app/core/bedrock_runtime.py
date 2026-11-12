"""Owner-side assembly of the Bedrock clients, embedding gate and query embedder.

Nothing here reads the environment or discovers credentials: the caller supplies an
``aioboto3`` session it created (profile, role or test keys) and the accounting session
factories. The result plugs into ``build_model_runtime(bedrock_client=...)`` for generation and
into ``ApplicationPorts.query_embedder`` / the embedding workers for embeddings.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.adapters.embedding.titan import AmazonTitanEmbeddingAdapter
from app.adapters.model.bedrock import ConfiguredAsyncConverseClient
from app.adapters.model.bedrock_client import open_bedrock_runtime
from app.contracts.provider_budget import DisclosurePolicyV1
from app.core.model_config import ModelRuntimeSettings
from app.services.accounted_embedding import (
    AccountedEmbeddingGate,
    AccountedQueryEmbedder,
    SessionFactory,
)


@dataclass(frozen=True)
class BedrockWiring:
    converse_client: ConfiguredAsyncConverseClient
    gate: AccountedEmbeddingGate
    query_embedder: AccountedQueryEmbedder


def repository_policy_resolver(settings: ModelRuntimeSettings) -> Any:
    policies = {(p.org_id, p.repository_id): p.policy for p in settings.repository_policies}

    def policy_for(org_id: UUID, repository_id: UUID) -> DisclosurePolicyV1:
        # Unknown repositories get the default policy, which refuses every external provider.
        return policies.get((org_id, repository_id), DisclosurePolicyV1())

    return policy_for


@asynccontextmanager
async def build_bedrock_wiring(
    settings: ModelRuntimeSettings,
    *,
    session: Any,
    reserve_sessions: SessionFactory,
    dispatch_sessions: SessionFactory,
    embedding_timeout_seconds: float = 30.0,
) -> AsyncIterator[BedrockWiring]:
    if settings.execution_mode != "live" or not settings.allow_live_egress:
        raise ValueError("Bedrock wiring requires live execution with explicit egress approval")
    async with open_bedrock_runtime(session) as client:
        gate = AccountedEmbeddingGate(
            AmazonTitanEmbeddingAdapter(client, timeout_seconds=embedding_timeout_seconds),
            reserve_sessions,
            dispatch_sessions,
        )
        yield BedrockWiring(
            converse_client=ConfiguredAsyncConverseClient(client),
            gate=gate,
            query_embedder=AccountedQueryEmbedder(gate, repository_policy_resolver(settings)),
        )
