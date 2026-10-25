"""Unit tests for Model Gateway, capability routing, microdollar budgeting,
and output validation.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.adapters.model.fake import (
    FakeFailureMode,
    FakeModelAdapter,
    FakeModelRateLimitError,
    FakeModelRefusalError,
    FakeModelTimeoutError,
)
from app.contracts.model import (
    ChatMessageV1,
    GenerationPriceScheduleV1,
    ModelRequestV1,
    ModelResponseV1,
    ModelRole,
    ModelRoutingRequirementV1,
    ToolCallV1,
    ToolDefinitionV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.models.budget_tables import embedding_budgets, price_schedules
from app.models.model_gateway_tables import (
    model_capabilities,
    model_reservations,
    model_spend_ledger,
)
from app.models.phase2_tables import md
from app.services.model_budget import (
    ModelBudgetExceededError,
    ModelBudgetService,
)
from app.services.model_gateway import ModelGatewayService
from app.services.model_router import (
    DisclosureViolationError,
    ModelRouter,
)
from app.services.output_validator import (
    DangerousArgumentError,
    InvalidJsonOutputError,
    OutputValidator,
    SchemaMismatchError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    embedding_budgets,
                    price_schedules,
                    model_capabilities,
                    model_reservations,
                    model_spend_ledger,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_fake_model_adapter_generate_and_stream() -> None:
    adapter = FakeModelAdapter(
        default_content="Analyzed authentication flow safely.",
        fixed_input_tokens=120,
        fixed_output_tokens=45,
    )

    req = ModelRequestV1(
        investigation_id=uuid4(),
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        messages=(ChatMessageV1(role=ModelRole.USER, content="Explain auth flow"),),
        reservation_id=uuid4(),
    )

    res: ModelResponseV1 = await adapter.generate(req)
    assert res.content == "Analyzed authentication flow safely."
    assert res.usage.input_tokens == 120
    assert res.usage.output_tokens == 45
    assert res.finish_reason == "stop"
    assert len(adapter.call_history) == 1

    # Test streaming
    chunks = [chunk async for chunk in adapter.stream(req)]
    assert len(chunks) > 1
    assert chunks[-1].is_final is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 165
    reconstructed = "".join(c.delta_text for c in chunks)
    assert reconstructed.strip() == "Analyzed authentication flow safely."


@pytest.mark.asyncio
async def test_fake_model_adapter_failure_modes() -> None:
    adapter = FakeModelAdapter()
    req = ModelRequestV1(
        investigation_id=uuid4(),
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        messages=(ChatMessageV1(role=ModelRole.USER, content="Hello"),),
        reservation_id=uuid4(),
    )

    adapter.set_failure_mode(FakeFailureMode.TIMEOUT)
    with pytest.raises(FakeModelTimeoutError, match="timeout connecting"):
        await adapter.generate(req)

    adapter.set_failure_mode(FakeFailureMode.RATE_LIMIT)
    with pytest.raises(FakeModelRateLimitError, match="rate limit exceeded"):
        await adapter.generate(req)

    adapter.set_failure_mode(FakeFailureMode.PROVIDER_REFUSAL)
    with pytest.raises(FakeModelRefusalError, match="safety refusal"):
        await adapter.generate(req)


@pytest.mark.asyncio
async def test_model_budget_service_reservations_and_settlement(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    inv_id = uuid4()
    now = datetime.now(UTC)

    # 1. Seed initial $10 budget (10,000,000 microdollars)
    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=10_000_000)

    # 2. Add generation price schedule ($1.00/1M in, $2.00/1M out -> 1000/1k in, 2000/1k out)
    sched = GenerationPriceScheduleV1(
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        version="v1.0",
        input_micros_per_1k=1000,
        output_micros_per_1k=2000,
        effective_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
    )
    await budget_svc.set_generation_price_schedule(sched)

    # 3. Reserve 200,000 microdollars
    res_id = await budget_svc.reserve_generation_budget(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        investigation_id=inv_id,
        logical_call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        estimated_input_tokens=50000,
        estimated_output_tokens=75000,
        estimated_micros=200_000,
        idempotency_key="idemp_call_001",
    )
    assert res_id is not None

    b_after_res = await budget_svc.get_budget(org_id, repo_id)
    assert b_after_res["balance_micros"] == 10_000_000
    assert b_after_res["reserved_micros"] == 200_000
    assert b_after_res["available_micros"] == 9_800_000

    # 4. Commit intent and mark dispatched
    await budget_svc.commit_dispatch_intent(org_id, res_id)
    await budget_svc.mark_dispatched(org_id, res_id)

    # 5. Settle with actual usage of only 80,000 microdollars
    await budget_svc.settle_reservation(
        org_id=org_id,
        reservation_id=res_id,
        actual_input_tokens=20000,
        actual_output_tokens=30000,
        actual_thinking_tokens=0,
        actual_micros=80_000,
        provider_request_id="req-12345",
    )

    b_after_settle = await budget_svc.get_budget(org_id, repo_id)
    assert b_after_settle["balance_micros"] == 9_920_000  # 10M - 80k
    assert b_after_settle["reserved_micros"] == 0
    assert b_after_settle["available_micros"] == 9_920_000


@pytest.mark.asyncio
async def test_model_budget_service_uncertain_hold(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    inv_id = uuid4()

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    res_id = await budget_svc.reserve_generation_budget(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        investigation_id=inv_id,
        logical_call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        estimated_input_tokens=10000,
        estimated_output_tokens=20000,
        estimated_micros=150_000,
        idempotency_key="idemp_hold_001",
    )

    await budget_svc.commit_dispatch_intent(org_id, res_id)
    await budget_svc.mark_dispatched(org_id, res_id)

    # Simulate network drop during dispatch -> hold uncertain
    await budget_svc.hold_uncertain(
        org_id=org_id,
        reservation_id=res_id,
        failure_reason="timeout_during_dispatch",
    )

    b = await budget_svc.get_budget(org_id, repo_id)
    # The 150k reserved micros MUST be retained as an active hold, not zeroed out!
    assert b["reserved_micros"] == 150_000
    assert b["available_micros"] == 850_000


@pytest.mark.asyncio
async def test_model_budget_service_insufficient_funds(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=50_000)

    with pytest.raises(ModelBudgetExceededError, match="Insufficient funds"):
        await budget_svc.reserve_generation_budget(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            investigation_id=uuid4(),
            logical_call_id=uuid4(),
            attempt_id=1,
            provider="fake",
            model_id="codespy-fake-investigator-v1",
            estimated_input_tokens=10000,
            estimated_output_tokens=20000,
            estimated_micros=100_000,  # Exceeds 50k balance
            idempotency_key="idemp_over_001",
        )


@pytest.mark.asyncio
async def test_model_router_policy_first_enforcement(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    inv_id = uuid4()
    now = datetime.now(UTC)

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=5_000_000)

    # Seed price schedules
    await budget_svc.set_generation_price_schedule(
        GenerationPriceScheduleV1(
            provider="fake",
            model_id="codespy-fake-investigator-v1",
            version="2026-10-22.1",
            input_micros_per_1k=500,
            output_micros_per_1k=1000,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )
    await budget_svc.set_generation_price_schedule(
        GenerationPriceScheduleV1(
            provider="bedrock_claude",
            model_id="in.anthropic.claude-haiku-4-5-20251001-v1:0",
            version="2026-10-22.1",
            input_micros_per_1k=1000,
            output_micros_per_1k=5000,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    # 1. Zero external disclosure policy -> Bedrock request fails, only Fake allowed
    strict_policy = DisclosurePolicyV1(allow_external_disclosure=False)
    routed = await router.route_and_reserve(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        investigation_id=inv_id,
        logical_call_id=uuid4(),
        attempt_id=1,
        requirement=ModelRoutingRequirementV1(
            estimated_context_tokens=1000, estimated_output_tokens=500
        ),
        disclosure_policy=strict_policy,
        idempotency_key="idemp_route_strict",
    )
    assert routed.provider == "fake"

    # 2. External disclosure allowed for India residency
    india_policy = DisclosurePolicyV1(
        allow_external_disclosure=True,
        allowed_providers=("bedrock_claude", "fake"),
        allowed_regions=("ap-south-1", "in"),
    )
    routed_india = await router.route_and_reserve(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        investigation_id=inv_id,
        logical_call_id=uuid4(),
        attempt_id=1,
        requirement=ModelRoutingRequirementV1(
            estimated_context_tokens=2000,
            estimated_output_tokens=1000,
            required_region="ap-south-1",
        ),
        disclosure_policy=india_policy,
        idempotency_key="idemp_route_india",
        preferred_provider="bedrock_claude",
    )
    assert routed_india.provider == "bedrock_claude"
    assert routed_india.model_id == "in.anthropic.claude-haiku-4-5-20251001-v1:0"

    # 3. Forbidden foreign region request raises DisclosureViolationError
    with pytest.raises(DisclosureViolationError):
        await router.route_and_reserve(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            investigation_id=inv_id,
            logical_call_id=uuid4(),
            attempt_id=1,
            requirement=ModelRoutingRequirementV1(
                estimated_context_tokens=1000,
                estimated_output_tokens=500,
                required_region="eu-west-1",  # Not in allowed regions
            ),
            disclosure_policy=india_policy,
            idempotency_key="idemp_route_fail",
            preferred_provider="bedrock_claude",
        )


def test_output_validator_json_and_security_guards() -> None:
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
            "is_vulnerable": {"type": "boolean"},
        },
        "required": ["summary", "confidence"],
    }

    # 1. Valid JSON passes
    valid_json = '{"summary": "Safe endpoint", "confidence": 0.95}'
    parsed = OutputValidator.validate_json_schema(valid_json, schema)
    assert parsed["summary"] == "Safe endpoint"
    assert parsed["confidence"] == 0.95

    # 2. Malformed JSON raises InvalidJsonOutputError
    with pytest.raises(InvalidJsonOutputError):
        OutputValidator.validate_json_schema('{"broken: json', schema)

    # 3. Missing required field raises SchemaMismatchError
    with pytest.raises(SchemaMismatchError, match="Missing required schema field: 'confidence'"):
        OutputValidator.validate_json_schema('{"summary": "Incomplete"}', schema)

    # 4. Tool call security invariants
    tool_def = ToolDefinitionV1(
        name="read_file_range",
        description="Read file line range",
        parameters_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    # Clean tool call passes
    clean_call = ToolCallV1(
        id="call-1",
        name="read_file_range",
        arguments_json='{"path": "src/auth/jwt.py"}',
    )
    args = OutputValidator.validate_tool_call(clean_call, (tool_def,))
    assert args["path"] == "src/auth/jwt.py"

    # Path traversal argument raises DangerousArgumentError
    traversal_call = ToolCallV1(
        id="call-2",
        name="read_file_range",
        arguments_json='{"path": "../../../etc/passwd"}',
    )
    with pytest.raises(DangerousArgumentError, match="Path traversal detected"):
        OutputValidator.validate_tool_call(traversal_call, (tool_def,))

    # Shell command injection argument raises DangerousArgumentError
    shell_call = ToolCallV1(
        id="call-3",
        name="read_file_range",
        arguments_json='{"path": "foo.py; rm -rf /"}',
    )
    with pytest.raises(DangerousArgumentError, match="Dangerous shell characters"):
        OutputValidator.validate_tool_call(shell_call, (tool_def,))


@pytest.mark.asyncio
async def test_model_gateway_end_to_end_execution(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    adapter = FakeModelAdapter(
        default_content='{"findings_count": 0, "status": "clean"}',
        fixed_input_tokens=150,
        fixed_output_tokens=60,
    )
    gateway = ModelGatewayService(
        router=router,
        budget_service=budget_svc,
        adapters={"fake": adapter},
    )

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    inv_id = uuid4()
    now = datetime.now(UTC)

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=1_000_000)

    await budget_svc.set_generation_price_schedule(
        GenerationPriceScheduleV1(
            provider="fake",
            model_id="codespy-fake-investigator-v1",
            version="2026-10-22.1",
            input_micros_per_1k=500,
            output_micros_per_1k=1000,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    response = await gateway.execute_call(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        investigation_id=inv_id,
        logical_call_id=uuid4(),
        messages=(ChatMessageV1(role=ModelRole.USER, content="Audit snapshot"),),
        requirement=ModelRoutingRequirementV1(
            estimated_context_tokens=1000,
            estimated_output_tokens=500,
            requires_structured_output=True,
        ),
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
        idempotency_key="idemp_gw_001",
        response_schema={
            "type": "object",
            "properties": {"findings_count": {"type": "integer"}, "status": {"type": "string"}},
            "required": ["findings_count", "status"],
        },
    )

    assert response.content == '{"findings_count": 0, "status": "clean"}'
    assert response.charge_micros > 0  # Actual settled charge

    budget = await budget_svc.get_budget(org_id, repo_id)
    assert budget["reserved_micros"] == 0  # Cleanly settled
    assert budget["balance_micros"] == 1_000_000 - response.charge_micros


async def configured_gateway(session, adapter):
    budget = ModelBudgetService(session)
    org, repo = uuid4(), uuid4()
    now = datetime.now(UTC)
    await budget.ensure_budget(org, repo, initial_balance_micros=1_000_000)
    await budget.set_generation_price_schedule(
        GenerationPriceScheduleV1(
            provider="fake",
            model_id="codespy-fake-investigator-v1",
            version="2026-10-22.1",
            input_micros_per_1k=500,
            output_micros_per_1k=1000,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=1),
        )
    )
    gateway = ModelGatewayService(ModelRouter(budget), budget, {"fake": adapter})
    args = {
        "org_id": org,
        "repository_id": repo,
        "principal_id": uuid4(),
        "investigation_id": uuid4(),
        "logical_call_id": uuid4(),
        "messages": (ChatMessageV1(role=ModelRole.USER, content="source-canary-78219"),),
        "requirement": ModelRoutingRequirementV1(
            estimated_context_tokens=1000,
            estimated_output_tokens=500,
            requires_structured_output=True,
        ),
        "disclosure_policy": DisclosurePolicyV1(allow_external_disclosure=False),
        "idempotency_key": "gateway-safety",
        "response_schema": {"type": "object", "required": ["status"]},
    }
    return gateway, budget, args


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["source-canary-78219", '{"other": "source-canary-78219"}', ""])
async def test_invalid_output_is_settled_without_implicit_repair(session, caplog, content):
    from app.services.output_validator import OutputValidationError
    from sqlalchemy import select

    adapter = FakeModelAdapter(
        default_content=content, fixed_input_tokens=150, fixed_output_tokens=60
    )
    gateway, budget, args = await configured_gateway(session, adapter)
    with pytest.raises(OutputValidationError):
        await gateway.execute_call(**args)
    assert len(adapter.call_history) == 1
    row = (await session.execute(select(model_reservations))).mappings().one()
    assert row["state"] == "settled"
    ledger = (await session.execute(select(model_spend_ledger))).mappings().one()
    assert ledger["input_tokens"] == 150
    assert ledger["output_tokens"] == 60
    assert row["settled_micros"] == ledger["charged_micros"] == 135
    assert (await budget.get_budget(args["org_id"], args["repository_id"]))["reserved_micros"] == 0
    assert "source-canary-78219" not in caplog.text


@pytest.mark.asyncio
async def test_dispatch_exception_is_preserved_and_sanitized(session, caplog):
    from sqlalchemy import select

    original = RuntimeError("source-canary-78219 provider credentials text")

    def failing(request):
        raise original

    adapter = FakeModelAdapter(custom_handler=failing)
    gateway, budget, args = await configured_gateway(session, adapter)
    with pytest.raises(RuntimeError) as raised:
        await gateway.execute_call(**args)
    assert raised.value is original
    assert len(adapter.call_history) == 1
    row = (await session.execute(select(model_reservations))).mappings().one()
    assert row["state"] == "uncertain_hold"
    assert row["failure_reason"] == "provider_dispatch_failed"
    assert "source-canary-78219" not in caplog.text
    assert (await budget.get_budget(args["org_id"], args["repository_id"]))["reserved_micros"] > 0


@pytest.mark.asyncio
async def test_dispatch_cancellation_propagates_without_implicit_retry(session, caplog):
    import asyncio

    from sqlalchemy import select

    cancelled = asyncio.CancelledError("source-canary-78219")

    def cancel(request):
        raise cancelled

    adapter = FakeModelAdapter(custom_handler=cancel)
    gateway, _, args = await configured_gateway(session, adapter)
    with pytest.raises(asyncio.CancelledError) as raised:
        await gateway.execute_call(**args)
    assert raised.value is cancelled
    assert len(adapter.call_history) == 1
    row = (await session.execute(select(model_reservations))).mappings().one()
    assert row["state"] == "uncertain_hold"
    assert "source-canary-78219" not in caplog.text
