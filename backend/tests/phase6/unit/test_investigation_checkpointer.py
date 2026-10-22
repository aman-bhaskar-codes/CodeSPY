"""Unit tests for investigation domain, fenced checkpoints, and append-only event streams."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation import (
    InvestigationClaimV1,
    InvestigationCreateRequestV1,
    InvestigationDepth,
    InvestigationMode,
    InvestigationStateV1,
    InvestigationStatus,
    InvestigationTargetType,
    InvestigationVerdict,
)
from app.contracts.model import ChatMessageV1, ModelRole
from app.models.investigation_tables import (
    investigation_checkpoints,
    investigation_events,
    investigations,
)
from app.models.phase2_tables import md
from app.services.investigation_checkpointer import (
    FencedCheckpointer,
    InvestigationTerminatedError,
    LeaseAcquisitionError,
    StaleWorkerFencingError,
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
async def test_investigation_lifecycle_creation_and_retrieval(session: AsyncSession) -> None:
    svc = FencedCheckpointer(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    req = InvestigationCreateRequestV1(
        repository_id=repo_id,
        query="Verify SQL injection hypothesis in auth service",
        target_type=InvestigationTargetType.REPOSITORY,
        mode=InvestigationMode.SHORT_PATH,
        depth=InvestigationDepth.STANDARD,
        max_steps=10,
        budget_limit_micros=2_000_000,
    )

    created = await svc.create_investigation(org_id, principal_id, req)
    assert created.status == InvestigationStatus.PENDING.value
    assert created.fencing_token == 1
    assert created.current_step == 0
    assert created.budget_limit_micros == 2_000_000

    fetched = await svc.get_investigation(org_id, created.id)
    assert fetched["query"] == req.query
    assert fetched["spent_micros"] == 0


@pytest.mark.asyncio
async def test_worker_lease_acquisition_and_fencing(session: AsyncSession) -> None:
    svc = FencedCheckpointer(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    inv = await svc.create_investigation(
        org_id,
        principal_id,
        InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Analyze SSRF vector",
        ),
    )

    # 1. Worker 1 acquires lease
    fence1 = await svc.acquire_lease(org_id, inv.id, worker_id="worker-01", lease_ttl_seconds=10)
    assert fence1 == 2

    active_inv = await svc.get_investigation(org_id, inv.id)
    assert active_inv["status"] == InvestigationStatus.RUNNING.value
    assert active_inv["worker_id"] == "worker-01"

    # 2. Worker 2 attempts lease while Worker 1 lease is still active -> rejected
    with pytest.raises(LeaseAcquisitionError, match="Active lease held by worker 'worker-01'"):
        await svc.acquire_lease(org_id, inv.id, worker_id="worker-02", lease_ttl_seconds=10)

    # 3. Worker 1 renews lease
    await svc.renew_lease(
        org_id, inv.id, worker_id="worker-01", fencing_token=fence1, lease_ttl_seconds=20
    )


@pytest.mark.asyncio
async def test_stale_worker_fencing_prevents_split_brain(session: AsyncSession) -> None:
    svc = FencedCheckpointer(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    inv = await svc.create_investigation(
        org_id,
        principal_id,
        InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Check memory corruption vulnerability",
        ),
    )

    # Worker 1 acquires lease with 0-second TTL (immediately expired)
    fence1 = await svc.acquire_lease(org_id, inv.id, worker_id="worker-01", lease_ttl_seconds=-1)
    assert fence1 == 2

    # Worker 2 takes over because lease expired, advancing fencing token to 3
    fence2 = await svc.acquire_lease(org_id, inv.id, worker_id="worker-02", lease_ttl_seconds=30)
    assert fence2 == 3

    # Zombie Worker 1 attempts to save checkpoint with superseded fencing token
    dummy_state = InvestigationStateV1(
        investigation_id=inv.id,
        org_id=org_id,
        repository_id=repo_id,
        target_type="finding",
        query="Check memory corruption vulnerability",
        current_step=1,
    )

    with pytest.raises(StaleWorkerFencingError, match="Worker fence superseded"):
        await svc.save_checkpoint(
            org_id=org_id,
            investigation_id=inv.id,
            worker_id="worker-01",
            fencing_token=fence1,
            step_index=1,
            graph_node="scanner",
            state=dummy_state,
        )

    # Zombie Worker 1 attempts to renew lease -> fails
    with pytest.raises(StaleWorkerFencingError, match="superseded"):
        await svc.renew_lease(
            org_id=org_id,
            investigation_id=inv.id,
            worker_id="worker-01",
            fencing_token=fence1,
        )

    # Active Worker 2 can save checkpoints cleanly
    await svc.save_checkpoint(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-02",
        fencing_token=fence2,
        step_index=1,
        graph_node="retriever",
        state=dummy_state,
        new_spent_micros=50_000,
    )

    check_inv = await svc.get_investigation(org_id, inv.id)
    assert check_inv["current_step"] == 1
    assert check_inv["spent_micros"] == 50_000


@pytest.mark.asyncio
async def test_durable_checkpoint_save_and_recovery(session: AsyncSession) -> None:
    svc = FencedCheckpointer(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    inv = await svc.create_investigation(
        org_id,
        principal_id,
        InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Verify hardcoded secret leak",
        ),
    )

    fence = await svc.acquire_lease(org_id, inv.id, worker_id="worker-rec", lease_ttl_seconds=60)

    # Step 1: initial hypothesis
    state1 = InvestigationStateV1(
        investigation_id=inv.id,
        org_id=org_id,
        repository_id=repo_id,
        target_type="finding",
        query="Verify hardcoded secret leak",
        current_step=1,
        history=(ChatMessageV1(role=ModelRole.USER, content="Investigate potential AWS key"),),
        claims=(
            InvestigationClaimV1(
                claim_id="claim-01",
                statement="Credential found in auth.py is active",
                file_path="src/auth.py",
                line_start=45,
                line_end=45,
            ),
        ),
    )
    await svc.save_checkpoint(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-rec",
        fencing_token=fence,
        step_index=1,
        graph_node="investigate",
        state=state1,
        new_spent_micros=120_000,
    )

    # Step 2: verified finding
    state2 = state1.model_copy(
        update={
            "current_step": 2,
            "verdict": InvestigationVerdict.TRUE_POSITIVE.value,
            "verdict_confidence": 0.98,
            "summary": "Hardcoded production credential found with commit provenance.",
        }
    )
    await svc.save_checkpoint(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-rec",
        fencing_token=fence,
        step_index=2,
        graph_node="verdict",
        state=state2,
        new_spent_micros=80_000,
    )

    # Load latest checkpoint
    recovered = await svc.load_latest_checkpoint(org_id, inv.id)
    assert recovered is not None
    step_idx, rec_state = recovered
    assert step_idx == 2
    assert rec_state.verdict == InvestigationVerdict.TRUE_POSITIVE.value
    assert len(rec_state.claims) == 1
    assert rec_state.claims[0].claim_id == "claim-01"

    # Complete investigation
    await svc.complete_investigation(
        org_id=org_id,
        investigation_id=inv.id,
        worker_id="worker-rec",
        fencing_token=fence,
        verdict=InvestigationVerdict.TRUE_POSITIVE.value,
        verdict_confidence=0.98,
        summary="Confirmed real key",
    )

    # Terminal state protection
    with pytest.raises(InvestigationTerminatedError, match="terminal status"):
        await svc.acquire_lease(org_id, inv.id, worker_id="worker-new", lease_ttl_seconds=30)


@pytest.mark.asyncio
async def test_event_stream_replay(session: AsyncSession) -> None:
    svc = FencedCheckpointer(session)
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    inv = await svc.create_investigation(
        org_id,
        principal_id,
        InvestigationCreateRequestV1(
            repository_id=repo_id,
            query="Trace user input sanitization",
        ),
    )

    await svc.acquire_lease(org_id, inv.id, worker_id="worker-ev", lease_ttl_seconds=60)

    # Append custom tool and thinking events
    await svc.append_event(
        org_id=org_id,
        investigation_id=inv.id,
        step_index=1,
        event_type="tool_called",
        node_name="searcher",
        payload={"tool": "search_code", "query": "sanitizer"},
    )
    await svc.append_event(
        org_id=org_id,
        investigation_id=inv.id,
        step_index=1,
        event_type="tool_result",
        node_name="searcher",
        payload={"results_count": 3},
    )
    await svc.append_event(
        org_id=org_id,
        investigation_id=inv.id,
        step_index=2,
        event_type="claim_extracted",
        node_name="extractor",
        payload={"claim": "Sanitization exists in middleware"},
    )

    events_all = await svc.list_events(org_id, inv.id)
    assert len(events_all) == 3
    assert events_all[0].event_type == "tool_called"
    assert events_all[1].event_type == "tool_result"
    assert events_all[2].event_type == "claim_extracted"

    # Filter after step 1
    events_step2 = await svc.list_events(org_id, inv.id, after_step=1)
    assert len(events_step2) == 1
    assert events_step2[0].event_type == "claim_extracted"
