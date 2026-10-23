"""Unit tests for deterministic ClaimVerificationService and InvestigationStreamBuffer."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.adapters.model.fake import FakeModelAdapter
from app.contracts.investigation import (
    InvestigationClaimV1,
    InvestigationCreateRequestV1,
    InvestigationEventType,
    InvestigationEventV1,
    InvestigationStatus,
    InvestigationVerdict,
)
from app.contracts.model import (
    GenerationPriceScheduleV1,
    ModelResponseV1,
    TokenUsageV1,
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
from app.services.investigator_tools import ToolExecutionContext
from app.services.model_budget import ModelBudgetService
from app.services.model_gateway import ModelGatewayService
from app.services.model_router import ModelRouter
from app.services.short_path_investigator import ShortPathInvestigator
from app.services.stream_buffer import InvestigationStreamBuffer
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
async def test_claim_verification_exact_match() -> None:
    verifier = ClaimVerificationService(minimum_grounding_ratio=0.8)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    mock_files = {
        "services/auth.py": (
            "import os\n"
            "# Line 2\n"
            "SECRET_KEY = 'insecure-secret-key-12345'\n"
            "def verify_token():\n"
            "    pass\n"
        )
    }
    context = ToolExecutionContext(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        mock_files=mock_files,
    )

    claims = [
        InvestigationClaimV1(
            claim_id="c1",
            statement="Insecure secret key hardcoded at line 3",
            file_path="services/auth.py",
            line_start=3,
            line_end=3,
            quoted_source="SECRET_KEY = 'insecure-secret-key-12345'",
        )
    ]

    report = await verifier.verify_claims(
        claims=claims,
        context=context,
        original_verdict=InvestigationVerdict.TRUE_POSITIVE.value,
        original_confidence=0.95,
    )

    assert report.is_grounded is True
    assert report.total_claims == 1
    assert report.verified_count == 1
    assert report.refuted_count == 0
    assert report.unsupported_count == 0
    assert report.grounding_ratio == 1.0
    assert report.adjusted_verdict == InvestigationVerdict.TRUE_POSITIVE.value
    assert report.adjusted_confidence == 0.95
    assert report.verified_claims[0].status == "verified"


@pytest.mark.asyncio
async def test_claim_verification_out_of_bounds() -> None:
    verifier = ClaimVerificationService(minimum_grounding_ratio=0.8)
    context = ToolExecutionContext(
        org_id=uuid4(),
        repository_id=uuid4(),
        principal_id=uuid4(),
        mock_files={"main.py": "print('hello')\n"},
    )

    claims = [
        InvestigationClaimV1(
            claim_id="c1",
            statement="Exploit on non-existent line 99",
            file_path="main.py",
            line_start=99,
            line_end=100,
        )
    ]

    report = await verifier.verify_claims(
        claims=claims,
        context=context,
        original_verdict=InvestigationVerdict.TRUE_POSITIVE.value,
        original_confidence=0.90,
    )

    assert report.is_grounded is False
    assert report.unsupported_count == 1
    assert report.verified_count == 0
    assert report.adjusted_verdict == InvestigationVerdict.INDETERMINATE.value
    assert report.adjusted_confidence is not None and report.adjusted_confidence <= 0.40
    assert report.verified_claims[0].status == "unsupported"


@pytest.mark.asyncio
async def test_claim_verification_refuted_hallucination() -> None:
    verifier = ClaimVerificationService(minimum_grounding_ratio=0.8)
    context = ToolExecutionContext(
        org_id=uuid4(),
        repository_id=uuid4(),
        principal_id=uuid4(),
        mock_files={"api/user.py": "def get_user():\n    return 'safe_user'\n"},
    )

    claims = [
        InvestigationClaimV1(
            claim_id="c1",
            statement="Model hallucinations SQL injection",
            file_path="api/user.py",
            line_start=1,
            line_end=2,
            quoted_source="cursor.execute('SELECT * FROM users WHERE id = ' + req_id)",
        )
    ]

    report = await verifier.verify_claims(
        claims=claims,
        context=context,
        original_verdict=InvestigationVerdict.TRUE_POSITIVE.value,
        original_confidence=0.95,
    )

    assert report.is_grounded is False
    assert report.refuted_count == 1
    assert report.verified_count == 0
    assert report.adjusted_verdict == InvestigationVerdict.INDETERMINATE.value
    assert report.adjusted_confidence is not None and report.adjusted_confidence <= 0.40
    assert report.verified_claims[0].status == "refuted"
    assert "does not exist in 'api/user.py'" in (report.verified_claims[0].verification_notes or "")


@pytest.mark.asyncio
async def test_claim_verification_missing_file() -> None:
    verifier = ClaimVerificationService(minimum_grounding_ratio=0.8)
    context = ToolExecutionContext(
        org_id=uuid4(),
        repository_id=uuid4(),
        principal_id=uuid4(),
        mock_files={"existing.py": "x = 1\n"},
    )

    claims = [
        InvestigationClaimV1(
            claim_id="c1",
            statement="Ghost file citation",
            file_path="ghost/missing.py",
            line_start=1,
        )
    ]

    report = await verifier.verify_claims(
        claims=claims,
        context=context,
        original_verdict=InvestigationVerdict.TRUE_POSITIVE.value,
        original_confidence=0.88,
    )

    assert report.is_grounded is False
    assert report.unsupported_count == 1
    assert report.verified_claims[0].status == "unsupported"
    assert "was not found" in (report.verified_claims[0].verification_notes or "")


@pytest.mark.asyncio
async def test_stream_buffer_pub_sub_live() -> None:
    stream_buffer = InvestigationStreamBuffer()
    org_id = uuid4()
    inv_id = uuid4()

    event1 = InvestigationEventV1(
        id=uuid4(),
        org_id=org_id,
        investigation_id=inv_id,
        step_index=1,
        event_type=InvestigationEventType.STEP_STARTED.value,
        node_name="loop",
        payload={"step": 1},
        created_at=datetime.now(UTC),
    )
    event2 = InvestigationEventV1(
        id=uuid4(),
        org_id=org_id,
        investigation_id=inv_id,
        step_index=2,
        event_type=InvestigationEventType.VERDICT_REACHED.value,
        node_name="finalizer",
        payload={"verdict": "true_positive"},
        created_at=datetime.now(UTC),
    )

    received_events: list[InvestigationEventV1] = []

    async def subscriber_task() -> None:
        async for ev in stream_buffer.subscribe(org_id, inv_id, timeout_seconds=2.0):
            received_events.append(ev)

    task = asyncio.create_task(subscriber_task())
    await asyncio.sleep(0.02)  # Give subscriber time to register queue

    await stream_buffer.publish_event(org_id, inv_id, event1)
    await stream_buffer.publish_event(org_id, inv_id, event2)

    await asyncio.wait_for(task, timeout=2.0)

    assert len(received_events) == 2
    assert received_events[0].id == event1.id
    assert received_events[1].id == event2.id
    assert received_events[1].event_type == InvestigationEventType.VERDICT_REACHED.value


def test_stream_buffer_sse_formatting() -> None:
    org_id = uuid4()
    inv_id = uuid4()
    ev_id = uuid4()
    now = datetime.now(UTC)

    event = InvestigationEventV1(
        id=ev_id,
        org_id=org_id,
        investigation_id=inv_id,
        step_index=3,
        event_type=InvestigationEventType.TOOL_CALLED.value,
        node_name="investigator_tools",
        payload={"tool": "search_code", "query": "auth"},
        created_at=now,
    )

    sse_text = InvestigationStreamBuffer.format_sse(event)
    assert sse_text.startswith(f"id: {ev_id}\n")
    assert "event: tool_called\n" in sse_text
    assert "data: {" in sse_text
    assert sse_text.endswith("\n\n")

    # Verify JSON content is valid
    data_line = next(line for line in sse_text.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line[len("data: ") :])
    assert payload["id"] == str(ev_id)
    assert payload["investigation_id"] == str(inv_id)
    assert payload["step_index"] == 3
    assert payload["payload"]["tool"] == "search_code"


@pytest.mark.asyncio
async def test_stream_buffer_replay_and_live(session: AsyncSession) -> None:
    stream_buffer = InvestigationStreamBuffer()
    checkpointer = FencedCheckpointer(session, event_sink=stream_buffer)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Test stream replay",
        ),
    )

    # Pre-populate 2 events in durable storage
    ev1_id = await checkpointer.append_event(
        org_id=org_id,
        investigation_id=inv.id,
        step_index=1,
        event_type=InvestigationEventType.STEP_STARTED.value,
        node_name="replay_node",
        payload={"step": 1},
    )
    ev2_id = await checkpointer.append_event(
        org_id=org_id,
        investigation_id=inv.id,
        step_index=2,
        event_type=InvestigationEventType.TOOL_CALLED.value,
        node_name="replay_node",
        payload={"tool": "search_code"},
    )

    received: list[InvestigationEventV1] = []

    async def run_subscriber() -> None:
        async for ev in stream_buffer.subscribe(
            org_id=org_id,
            investigation_id=inv.id,
            checkpointer=checkpointer,
            after_step=None,
            timeout_seconds=2.0,
        ):
            received.append(ev)

    sub_task = asyncio.create_task(run_subscriber())
    await asyncio.sleep(0.02)

    # Publish live terminal event via checkpointer
    ev3_id = await checkpointer.append_event(
        org_id=org_id,
        investigation_id=inv.id,
        step_index=3,
        event_type=InvestigationEventType.VERDICT_REACHED.value,
        node_name="finalizer",
        payload={"verdict": "benign"},
    )

    await asyncio.wait_for(sub_task, timeout=2.0)

    assert len(received) == 3
    assert received[0].id == ev1_id
    assert received[1].id == ev2_id
    assert received[2].id == ev3_id
    assert received[2].event_type == InvestigationEventType.VERDICT_REACHED.value


@pytest.mark.asyncio
async def test_short_path_investigator_hallucination_downgrade_and_events(
    session: AsyncSession,
) -> None:
    """Tests that a model claiming a true positive with hallucinated code has its verdict
    deterministically downgraded to indeterminate, confidence capped, and events emitted.
    """
    stream_buffer = InvestigationStreamBuffer()
    checkpointer = FencedCheckpointer(session, event_sink=stream_buffer)
    budget_svc = ModelBudgetService(session)
    router = ModelRouter(budget_svc)

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

    # Model returns terminal verdict claiming a vulnerability on phantom code
    hallucinated_verdict = {
        "verdict": "true_positive",
        "confidence": 0.99,
        "summary": "Critical remote code execution vulnerability found.",
        "claims": [
            {
                "claim_id": "c1",
                "statement": "Unsanitized eval of user input",
                "file_path": "server/handler.py",
                "line_start": 4,
                "line_end": 4,
                "quoted_source": "eval(request.GET['cmd'])",
            }
        ],
    }
    model_resp = ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="fake",
        model_id="codespy-fake-investigator-v1",
        content=f"```json\n{json.dumps(hallucinated_verdict)}\n```",
        usage=TokenUsageV1(input_tokens=150, output_tokens=80),
        charge_micros=0,
        finish_reason="stop",
        provider_request_id="hallucination-req",
    )
    adapter = FakeModelAdapter(canned_responses=[model_resp])
    gateway = ModelGatewayService(
        router=router, budget_service=budget_svc, adapters={"fake": adapter}
    )

    investigator = ShortPathInvestigator(
        model_gateway=gateway,
        checkpointer=checkpointer,
        claim_verifier=ClaimVerificationService(minimum_grounding_ratio=0.8),
    )

    inv = await checkpointer.create_investigation(
        org_id=org_id,
        principal_id=principal_id,
        req=InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Verify RCE vulnerability in server handler",
            max_steps=5,
        ),
    )

    fence_epoch = await checkpointer.acquire_lease(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-audit",
        lease_ttl_seconds=30,
    )

    # Ground truth: server/handler.py is completely benign and does NOT contain eval()
    context = ToolExecutionContext(
        org_id=org_id,
        repository_id=repo_id,
        principal_id=principal_id,
        mock_files={
            "server/handler.py": (
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/status')\n"
                "def status():\n"
                "    return {'status': 'healthy'}\n"
            )
        },
    )

    final_state = await investigator.run_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-audit",
        fencing_token=fence_epoch,
        disclosure_policy=DisclosurePolicyV1(allow_external_disclosure=False),
        context=context,
    )

    # Verification must have intercepted the hallucination:
    # 1. Verdict downgraded from true_positive to indeterminate
    assert final_state.verdict == InvestigationVerdict.INDETERMINATE.value
    # 2. Confidence capped at <= 0.40
    assert final_state.verdict_confidence is not None
    assert final_state.verdict_confidence <= 0.40
    # 3. Claim status marked as refuted
    assert len(final_state.claims) == 1
    assert final_state.claims[0].status == "refuted"
    # 4. Summary includes Grounding warning
    assert "Grounding verification failed" in (final_state.summary or "")

    # Check database status
    db_inv = await checkpointer.get_investigation(org_id, inv.id)
    assert db_inv["status"] == InvestigationStatus.COMPLETED.value
    assert db_inv["verdict"] == InvestigationVerdict.INDETERMINATE.value

    # Verify event stream records verification audit trail
    events = await checkpointer.list_events(org_id, inv.id)
    event_types = [e.event_type for e in events]
    assert InvestigationEventType.CLAIM_EXTRACTED.value in event_types
    assert InvestigationEventType.VERIFICATION_STARTED.value in event_types
    assert InvestigationEventType.VERIFICATION_RESULT.value in event_types
    assert InvestigationEventType.VERDICT_REACHED.value in event_types
