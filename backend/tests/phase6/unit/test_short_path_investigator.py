"""Unit tests for ShortPathInvestigator multi-step agentic loop and deterministic tools."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.adapters.model.fake import FakeModelAdapter
from app.contracts.investigation import (
    InvestigationCreateRequestV1,
    InvestigationDepth,
    InvestigationMode,
    InvestigationStatus,
    InvestigationVerdict,
)
from app.contracts.model import (
    GenerationPriceScheduleV1,
    ModelResponseV1,
    TokenUsageV1,
    ToolCallV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.models.budget_tables import embedding_budgets, price_schedules
from app.models.investigation_tables import (
    investigation_checkpoints,
    investigation_events,
    investigations,
)
from app.models.model_gateway_tables import (
    model_capabilities,
    model_reservations,
    model_spend_ledger,
)
from app.models.phase2_tables import md
from app.services.investigation_checkpointer import FencedCheckpointer
from app.services.investigator_tools import (
    InvestigatorToolRegistry,
    ToolExecutionContext,
)
from app.services.model_budget import ModelBudgetService
from app.services.model_gateway import ModelGatewayService
from app.services.model_router import ModelRouter
from app.services.short_path_investigator import ShortPathInvestigator
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
                    investigations,
                    investigation_checkpoints,
                    investigation_events,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_short_path_investigator_multi_step_flow(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    checkpointer = FencedCheckpointer(session)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

    # 1. Budget & pricing setup
    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=10_000_000)
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

    # 2. Configure FakeModelAdapter responses for 2 steps:
    # Step 1: Model asks to search code
    # Step 2: Model asks to read lines 1-15 of src/auth.py
    # Step 3: Model produces final verdict
    adapter = FakeModelAdapter(fixed_input_tokens=100, fixed_output_tokens=50)

    # Queue step 1 tool call
    call1 = ToolCallV1(
        id="call_search_1",
        name="search_code",
        arguments_json='{"query": "api_key", "limit": 3}',
    )
    resp1 = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="",
        tool_calls=(call1,),
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=0,
        finish_reason="tool_calls",
        provider_request_id="req-1",
    )
    adapter.queue_response(resp1)

    # Queue step 2 tool call
    call2 = ToolCallV1(
        id="call_read_1",
        name="read_file_range",
        arguments_json='{"file_path": "src/auth.py", "start_line": 1, "end_line": 5}',
    )
    resp2 = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="",
        tool_calls=(call2,),
        usage=TokenUsageV1(input_tokens=200, output_tokens=50),
        charge_micros=0,
        finish_reason="tool_calls",
        provider_request_id="req-2",
    )
    adapter.queue_response(resp2)

    # Queue step 3 terminal verdict
    verdict_json = {
        "verdict": "true_positive",
        "confidence": 0.96,
        "summary": "Hardcoded production API key found in src/auth.py line 3.",
        "claims": [
            {
                "claim_id": "c1",
                "statement": "Production key is committed in plaintext",
                "file_path": "src/auth.py",
                "line_start": 3,
                "line_end": 3,
                "quoted_source": "API_KEY = 'AKIAEXAMPLE12345678'",
            }
        ],
    }
    resp3 = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content=f"```json\n{json.dumps(verdict_json)}\n```",
        usage=TokenUsageV1(input_tokens=300, output_tokens=80),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-3",
    )
    adapter.queue_response(resp3)

    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )
    tool_registry = InvestigatorToolRegistry()
    investigator = ShortPathInvestigator(gateway, checkpointer, tool_registry)

    # 3. Create investigation
    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Verify hardcoded credential finding in auth service",
            mode=InvestigationMode.SHORT_PATH,
            depth=InvestigationDepth.STANDARD,
            max_steps=5,
            budget_limit_micros=1_000_000,
        ),
    )

    fence = await checkpointer.acquire_lease(
        org_id, inv.id, worker_id="worker-agent", lease_ttl_seconds=60
    )

    # Context with mock files and search hits
    context = ToolExecutionContext(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        mock_files={
            "src/auth.py": (
                "import os\n\nAPI_KEY = 'AKIAEXAMPLE12345678'\ndef get_auth():\n    return API_KEY"
            )
        },
        mock_search_results=[{"path": "src/auth.py", "line": 3, "snippet": "API_KEY = '...'"}],
    )

    # 4. Run investigator loop
    final_state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-agent",
        fencing_token=fence,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
        context=context,
    )

    assert final_state.verdict == InvestigationVerdict.TRUE_POSITIVE.value
    assert final_state.verdict_confidence == 0.96
    assert len(final_state.claims) == 1
    assert final_state.claims[0].file_path == "src/auth.py"
    assert final_state.claims[0].line_start == 3
    assert len(final_state.tool_history) == 2
    assert len(final_state.collected_evidence) == 2
    assert final_state.total_spent_micros > 0

    # 5. Verify database final status
    stored_inv = await checkpointer.get_investigation(org_id, inv.id)
    assert stored_inv["status"] == InvestigationStatus.COMPLETED.value
    assert stored_inv["verdict"] == InvestigationVerdict.TRUE_POSITIVE.value

    # 6. Verify event stream
    events = await checkpointer.list_events(org_id, inv.id)
    assert len(events) >= 6  # step_started, tool_called, tool_result, checkpoint_saved, etc.


@pytest.mark.asyncio
async def test_short_path_investigator_step_limit_bound(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    checkpointer = FencedCheckpointer(session)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=5_000_000)
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

    # Adapter continuously returns tool calls without finishing
    call = ToolCallV1(id="loop_call", name="search_code", arguments_json='{"query": "loop"}')
    continuous_tool_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="",
        tool_calls=(call,),
        usage=TokenUsageV1(input_tokens=100, output_tokens=30),
        charge_micros=0,
        finish_reason="tool_calls",
        provider_request_id="loop-req",
    )
    # Queue 3 responses, but max_steps will be 2
    adapter = FakeModelAdapter(
        canned_responses=[continuous_tool_resp, continuous_tool_resp, continuous_tool_resp]
    )
    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )
    investigator = ShortPathInvestigator(gateway, checkpointer)

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Analyze potential loop",
            max_steps=2,  # Strict 2-step limit
            budget_limit_micros=1_000_000,
        ),
    )

    fence = await checkpointer.acquire_lease(
        org_id, inv.id, worker_id="worker-loop", lease_ttl_seconds=30
    )
    final_state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-loop",
        fencing_token=fence,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
    )

    assert final_state.current_step == 2
    assert final_state.verdict == InvestigationVerdict.INDETERMINATE.value
    assert "Maximum steps reached" in (final_state.summary or "")


@pytest.mark.asyncio
async def test_short_path_investigator_budget_exhaustion(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    checkpointer = FencedCheckpointer(session)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=500_000)
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

    # Step 1 spends 60,000 micros against a 50,000 budget limit
    call = ToolCallV1(id="c1", name="search_code", arguments_json='{"query": "expensive"}')
    expensive_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="",
        tool_calls=(call,),
        usage=TokenUsageV1(input_tokens=100_000, output_tokens=10_000),  # High token usage
        charge_micros=0,
        finish_reason="tool_calls",
        provider_request_id="exp-req",
    )
    adapter = FakeModelAdapter(canned_responses=[expensive_resp, expensive_resp])
    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )
    investigator = ShortPathInvestigator(gateway, checkpointer)

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Expensive search query",
            max_steps=10,
            budget_limit_micros=40_000,  # 40k microdollars limit
        ),
    )

    fence = await checkpointer.acquire_lease(
        org_id, inv.id, worker_id="worker-budget", lease_ttl_seconds=30
    )
    final_state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-budget",
        fencing_token=fence,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
    )

    # Next iteration detects state.total_spent_micros >= budget_limit and terminates safely
    assert final_state.verdict == InvestigationVerdict.INDETERMINATE.value
    assert "Budget limit" in (final_state.summary or "")
