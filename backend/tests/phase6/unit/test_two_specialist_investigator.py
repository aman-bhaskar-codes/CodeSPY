"""Unit tests for TwoSpecialistInvestigator adversarial reasoning architecture."""

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
from app.services.claim_verification import ClaimVerificationService
from app.services.investigation_checkpointer import FencedCheckpointer
from app.services.investigator_tools import (
    InvestigatorToolRegistry,
    ToolExecutionContext,
)
from app.services.model_budget import ModelBudgetService
from app.services.model_gateway import ModelGatewayService
from app.services.model_router import ModelRouter
from app.services.two_specialist_investigator import TwoSpecialistInvestigator
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
async def test_two_specialist_hypothesis_refuted_by_adversary(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    checkpointer = FencedCheckpointer(session)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

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

    adapter = FakeModelAdapter(fixed_input_tokens=100, fixed_output_tokens=50)

    # 1. Hypothesis Specialist generates exploit hypothesis
    hypo_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="Candidate SQL injection found in search handler: query concatenated directly.",
        tool_calls=(),
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-hypo",
    )
    adapter.queue_response(hypo_resp)

    # 2. Adversarial Specialist refutes with counter-evidence
    adv_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="Refutation: Input is cast to integer and sanitized by query builder.",
        tool_calls=(),
        usage=TokenUsageV1(input_tokens=120, output_tokens=60),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-adv",
    )
    adapter.queue_response(adv_resp)

    # 3. Arbiter weighs evidence and concludes false_positive
    arbiter_content = json.dumps(
        {
            "verdict": "false_positive",
            "confidence": 0.95,
            "summary": "Adversarial analysis verified query builder sanitization.",
            "claims": [
                {
                    "claim_id": "c-refute-1",
                    "statement": "Parameter is cast to int and parameterized",
                    "file_path": "backend/app/api/search.py",
                    "line_start": 42,
                    "line_end": 43,
                    "quoted_source": "query_id = int(request.args.get('id'))",
                }
            ],
        }
    )
    arbiter_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content=arbiter_content,
        tool_calls=(),
        usage=TokenUsageV1(input_tokens=200, output_tokens=100),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-arbiter",
    )
    adapter.queue_response(arbiter_resp)

    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )

    tools = InvestigatorToolRegistry()

    class StubClaimVerifier(ClaimVerificationService):
        async def verify_claims(
            self, claims, context, original_verdict=None, original_confidence=None
        ):  # type: ignore[no-untyped-def]
            from app.services.claim_verification import ClaimVerificationReportV1

            updated = [c.model_copy(update={"status": "verified"}) for c in claims]
            return ClaimVerificationReportV1(
                total_claims=len(claims),
                verified_count=len(claims),
                refuted_count=0,
                unsupported_count=0,
                grounding_ratio=1.0,
                is_grounded=True,
                verified_claims=tuple(updated),
                adjusted_verdict=original_verdict,
                adjusted_confidence=original_confidence,
                verification_summary="Verified against ground-truth source.",
            )

    investigator = TwoSpecialistInvestigator(
        model_gateway=gateway,
        checkpointer=checkpointer,
        tool_registry=tools,
        claim_verifier=StubClaimVerifier(),
    )

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Verify potential SQLi in search handler",
            mode=InvestigationMode.TWO_SPECIALIST,
            depth=InvestigationDepth.STANDARD,
            max_steps=6,
            budget_limit_micros=500_000,
        ),
    )

    fence = await checkpointer.acquire_lease(org_id, inv.id, worker_id="worker-adv-1")

    final_state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-adv-1",
        fencing_token=fence,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
        context=ToolExecutionContext(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
        ),
    )

    assert final_state.verdict == InvestigationVerdict.FALSE_POSITIVE.value
    assert final_state.verdict_confidence == 0.95
    assert final_state.summary is not None
    assert "sanitization" in final_state.summary
    assert len(final_state.claims) == 1
    assert final_state.claims[0].status == "verified"

    events = await checkpointer.list_events(org_id, inv.id)
    node_names = [e.node_name for e in events]
    assert "hypothesis_specialist" in node_names
    assert "adversary_specialist" in node_names
    assert "arbiter" in node_names
    assert "claim_verifier" in node_names


@pytest.mark.asyncio
async def test_two_specialist_shared_budget_limit_halt(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    checkpointer = FencedCheckpointer(session)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

    await budget_svc.ensure_budget(org_id, repo_id, initial_balance_micros=10_000_000)
    await budget_svc.set_generation_price_schedule(
        GenerationPriceScheduleV1(
            provider="fake",
            model_id="codespy-fake-investigator-v1",
            version="2026-10-22.1",
            input_micros_per_1k=1000,
            output_micros_per_1k=2000,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    )

    adapter = FakeModelAdapter(fixed_input_tokens=4000, fixed_output_tokens=2000)
    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )

    investigator = TwoSpecialistInvestigator(
        model_gateway=gateway,
        checkpointer=checkpointer,
    )

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Exhaust budget quickly",
            mode=InvestigationMode.TWO_SPECIALIST,
            depth=InvestigationDepth.QUICK,
            max_steps=6,
            budget_limit_micros=5000,
        ),
    )

    fence = await checkpointer.acquire_lease(org_id, inv.id, worker_id="worker-budget-1")

    state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-budget-1",
        fencing_token=fence,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
    )

    assert state.verdict == InvestigationVerdict.INDETERMINATE.value
    assert state.summary is not None
    assert "budget limit" in state.summary.lower()


@pytest.mark.asyncio
async def test_two_specialist_with_tool_calls(session: AsyncSession) -> None:
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)
    checkpointer = FencedCheckpointer(session)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

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

    adapter = FakeModelAdapter(fixed_input_tokens=100, fixed_output_tokens=50)

    # Step 1: Hypothesis specialist asks to search code
    step1_call = ToolCallV1(
        id="call-hypo-1",
        name="search_code",
        arguments_json=json.dumps({"query": "API_SECRET_KEY"}),
    )
    step1_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="Searching for secret key references...",
        tool_calls=(step1_call,),
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=0,
        finish_reason="tool_calls",
        provider_request_id="req-step1",
    )
    adapter.queue_response(step1_resp)

    # Step 2: Hypothesis completes
    step2_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="Hardcoded credential exists in config.py line 12.",
        tool_calls=(),
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-step2",
    )
    adapter.queue_response(step2_resp)

    # Step 3: Adversary confirms finding is unmitigated
    step3_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content="Adversarial check confirms secret is committed in plaintext.",
        tool_calls=(),
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-step3",
    )
    adapter.queue_response(step3_resp)

    # Step 4: Arbiter confirms true_positive
    arbiter_content = json.dumps(
        {
            "verdict": "true_positive",
            "confidence": 0.99,
            "summary": "Both specialists confirm unmitigated hardcoded secret in config.",
            "claims": [
                {
                    "claim_id": "c-secret-1",
                    "statement": "API_SECRET_KEY is hardcoded in config.py",
                    "file_path": "config.py",
                    "line_start": 12,
                    "line_end": 12,
                    "quoted_source": "API_SECRET_KEY = 'super-secret-production-key'",
                }
            ],
        }
    )
    arbiter_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content=arbiter_content,
        tool_calls=(),
        usage=TokenUsageV1(input_tokens=150, output_tokens=80),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="req-step4",
    )
    adapter.queue_response(arbiter_resp)

    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )

    tools = InvestigatorToolRegistry()

    class StubClaimVerifier(ClaimVerificationService):
        async def verify_claims(
            self, claims, context, original_verdict=None, original_confidence=None
        ):  # type: ignore[no-untyped-def]
            from app.services.claim_verification import ClaimVerificationReportV1

            updated = [c.model_copy(update={"status": "verified"}) for c in claims]
            return ClaimVerificationReportV1(
                total_claims=len(claims),
                verified_count=len(claims),
                refuted_count=0,
                unsupported_count=0,
                grounding_ratio=1.0,
                is_grounded=True,
                verified_claims=tuple(updated),
                adjusted_verdict=original_verdict,
                adjusted_confidence=original_confidence,
                verification_summary="Hardcoded secret verified.",
            )

    investigator = TwoSpecialistInvestigator(
        model_gateway=gateway,
        checkpointer=checkpointer,
        tool_registry=tools,
        claim_verifier=StubClaimVerifier(),
    )

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Verify hardcoded secret in config",
            mode=InvestigationMode.TWO_SPECIALIST,
            depth=InvestigationDepth.STANDARD,
            max_steps=6,
            budget_limit_micros=500_000,
        ),
    )

    fence = await checkpointer.acquire_lease(org_id, inv.id, worker_id="worker-tools-1")

    context = ToolExecutionContext(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        mock_files={"config.py": "API_SECRET_KEY = 'super-secret-production-key'"},
        mock_search_results=[{"path": "config.py", "line": 12, "preview": "API_SECRET_KEY = ..."}],
    )

    state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-tools-1",
        fencing_token=fence,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
        context=context,
    )

    assert state.verdict == InvestigationVerdict.TRUE_POSITIVE.value
    assert state.verdict_confidence == 0.99
    assert len(state.claims) == 1
    assert state.claims[0].status == "verified"
    assert len(state.tool_history) >= 1
