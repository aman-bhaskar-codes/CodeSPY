"""Accounted embedding gate against real PostgreSQL accounting routines."""

from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.provider_budget import (
    DisclosurePolicyV1,
    EmbeddingRequestV1,
    EmbeddingResponseV1,
    PriceScheduleV1,
)
from app.services.accounted_embedding import (
    AccountedEmbeddingGate,
    EmbeddingReplayError,
    EmbeddingScope,
)
from app.services.provider_budget import (
    BudgetExceededError,
    DisclosureDeniedError,
    ProviderBudgetService,
)
from sqlalchemy import text
from tests.retrieval.storage.test_index_guards import source_case

MODEL = "amazon.titan-embed-text-v2:0"
ALLOWED = DisclosurePolicyV1(
    allow_external_disclosure=True,
    allowed_providers=("aws_bedrock",),
    allowed_regions=("ap-south-1",),
)


class Inner:
    def __init__(self, *, fail: Exception | None = None, reject: bool = False, tokens: int = 10):
        self.calls = 0
        self.fail, self.reject, self.tokens = fail, reject, tokens

    def validate(self, request):
        if self.reject:
            raise ValueError("cannot be sent")

    async def embed(self, request):
        self.calls += 1
        if self.fail:
            raise self.fail
        return EmbeddingResponseV1(
            vector=(0.6, 0.8),
            token_count=self.tokens,
            model_id=MODEL,
            provider="aws_bedrock",
            provider_request_id="req",
        )


def request(text_value="hello world"):
    return EmbeddingRequestV1(text=text_value, provider="aws_bedrock", model_id=MODEL)


@pytest.fixture
async def case(migration_factory, tmp_path):
    ctx, source, _ = await source_case(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ProviderBudgetService(session)
        await service.ensure_budget(ctx.org_id, source.repository_id, 10_000)
        now = datetime.now(UTC)
        await service.set_price_schedule(
            PriceScheduleV1(
                provider="aws_bedrock",
                model_id=MODEL,
                version="test",
                input_micros_per_1k=1000,
                effective_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
    return ctx, source


def scope(ctx, source, policy=ALLOWED, key="op-1"):
    return EmbeddingScope(
        org_id=ctx.org_id,
        repository_id=source.repository_id,
        principal_id=ctx.principal_id,
        policy=policy,
        operation_key=key,
    )


async def position(api_factory, ctx, source):
    async with api_factory() as session:
        return await ProviderBudgetService(session).get_budget(ctx.org_id, source.repository_id)


async def test_success_reserves_claims_calls_once_and_settles_the_actual_charge(
    case, api_factory, broker_factory
):
    ctx, source = case
    inner = Inner(tokens=2000)
    gate = AccountedEmbeddingGate(inner, api_factory, broker_factory)
    result = await gate.bind(scope(ctx, source)).embed(request())
    assert result.provider == "aws_bedrock" and inner.calls == 1
    state = await position(api_factory, ctx, source)
    assert state["reserved_micros"] == 0 and state["balance_micros"] == 10_000 - 2000
    with pytest.raises(EmbeddingReplayError):  # same operation and content: never sent twice
        await gate.bind(scope(ctx, source)).embed(request())
    assert inner.calls == 1
    await gate.bind(scope(ctx, source, key="op-2")).embed(request())  # a new operation may repeat
    assert inner.calls == 2


@pytest.mark.parametrize(
    "policy",
    [
        DisclosurePolicyV1(),
        DisclosurePolicyV1(
            allow_external_disclosure=True,
            allowed_providers=("aws_bedrock",),
            allowed_regions=("us-east-1",),
        ),
    ],
)
async def test_policy_denial_reserves_nothing_and_sends_nothing(
    case, api_factory, broker_factory, policy
):
    ctx, source = case
    inner = Inner()
    gate = AccountedEmbeddingGate(inner, api_factory, broker_factory)
    with pytest.raises(DisclosureDeniedError):
        await gate.bind(scope(ctx, source, policy)).embed(request())
    assert inner.calls == 0
    assert (await position(api_factory, ctx, source))["reserved_micros"] == 0


async def test_unsendable_request_is_refused_before_any_reservation(
    case, api_factory, broker_factory
):
    ctx, source = case
    inner = Inner(reject=True)
    gate = AccountedEmbeddingGate(inner, api_factory, broker_factory)
    with pytest.raises(ValueError, match="cannot be sent"):
        await gate.bind(scope(ctx, source)).embed(request())
    assert inner.calls == 0
    assert (await position(api_factory, ctx, source))["reserved_micros"] == 0


async def test_insufficient_budget_never_reaches_the_provider(case, api_factory, broker_factory):
    ctx, source = case
    inner = Inner()
    gate = AccountedEmbeddingGate(inner, api_factory, broker_factory)
    with pytest.raises(BudgetExceededError):  # ~66,700 estimated tokens > the 10,000 balance
        await gate.bind(scope(ctx, source)).embed(request("y" * 200_000))
    assert inner.calls == 0
    state = await position(api_factory, ctx, source)
    assert state["reserved_micros"] == 0 and state["balance_micros"] == 10_000


async def test_provider_failure_after_the_claim_becomes_an_uncertain_hold(
    case, api_factory, broker_factory, migration_factory
):
    ctx, source = case
    inner = Inner(fail=ProviderTransportError("provider_timeout"))
    gate = AccountedEmbeddingGate(inner, api_factory, broker_factory)
    with pytest.raises(ProviderTransportError):
        await gate.bind(scope(ctx, source)).embed(request())
    assert inner.calls == 1
    async with migration_factory() as session:
        rows = (
            await session.execute(
                text("SELECT state FROM embedding_reservations WHERE org_id=:o"),
                {"o": ctx.org_id},
            )
        ).all()
    assert [row.state for row in rows] == ["uncertain_hold"]
    assert (await position(api_factory, ctx, source))["reserved_micros"] > 0  # never auto-released
    with pytest.raises(EmbeddingReplayError):
        await gate.bind(scope(ctx, source)).embed(request())
    assert inner.calls == 1
