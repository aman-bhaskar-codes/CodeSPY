"""Query embeddings are redacted and accounted for external providers, and fail closed."""

from uuid import uuid4

import pytest
from app.contracts.provider_budget import (
    DisclosurePolicyV1,
    EmbeddingRequestV1,
    EmbeddingResponseV1,
)
from app.contracts.search import SearchScopeV1
from app.services.accounted_embedding import AccountedQueryEmbedder

SCOPE = SearchScopeV1(
    org_id=uuid4(), repository_id=uuid4(), snapshot_id=uuid4(), principal_id=uuid4()
)
SECRET = "AKIAIOSFODNN7EXAMPLE"  # gitleaks:allow  # noqa: S105


class RecordingGate:
    def __init__(self):
        self.seen = []

    async def embed(self, scope, request):
        self.seen.append((scope, request))
        return EmbeddingResponseV1(vector=(1.0,), token_count=1, model_id="m", provider="p")


def request(text):
    return EmbeddingRequestV1(text=text, provider="aws_bedrock")


async def test_secrets_are_redacted_before_the_query_leaves_the_process():
    gate = RecordingGate()
    policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("aws_bedrock",))
    embedder = AccountedQueryEmbedder(gate, lambda org, repo: policy)
    await embedder.embed_query(SCOPE, request(f"where is {SECRET} used"))
    scope, sent = gate.seen[0]
    assert SECRET not in sent.text and "REDACTED" in sent.text
    assert (scope.org_id, scope.repository_id, scope.principal_id) == (
        SCOPE.org_id,
        SCOPE.repository_id,
        SCOPE.principal_id,
    )
    assert scope.policy is policy


async def test_each_query_is_its_own_billed_operation():
    gate = RecordingGate()
    embedder = AccountedQueryEmbedder(gate, lambda org, repo: DisclosurePolicyV1())
    await embedder.embed_query(SCOPE, request("same question"))
    await embedder.embed_query(SCOPE, request("same question"))
    keys = {scope.operation_key for scope, _ in gate.seen}
    assert len(keys) == 2 and all(k.startswith("query:") for k in keys)


async def test_a_query_that_is_only_a_secret_is_refused():
    embedder = AccountedQueryEmbedder(RecordingGate(), lambda org, repo: DisclosurePolicyV1())
    with pytest.raises(ValueError, match="empty"):
        await embedder.embed_query(SCOPE, request("   "))


async def test_dense_search_refuses_a_raw_external_port(qualified_profile):
    from unittest.mock import AsyncMock

    from app.services.dense_search import DenseSearchService

    port = AsyncMock()
    service = DenseSearchService(AsyncMock(), embedding_port=port)
    service._verify_scope = AsyncMock()
    service._resolve_index = AsyncMock(
        return_value={
            "id": uuid4(),
            "embedding_model": "amazon.titan-embed-text-v2:0",
            "embedding_provider": "aws_bedrock",
            "dimension": 1024,
            "state": "ready",
        }
    )
    from tests.phase5.unit.test_dense_availability import request as search_request

    with pytest.raises(ValueError, match="accounted"):
        await service.search_dense(search_request())
    port.embed.assert_not_called()
