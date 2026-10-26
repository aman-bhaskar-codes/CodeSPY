"""Storage tests for investigation execution schema, checkpoints, and repositories.

Tests immutable checkpoints, sequence validation, replay conflicts,
spec persistence, report publication receipts, and evidence observations.
"""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation_execution import (
    InvestigationEvidenceV1,
    InvestigationExecutionStateV1,
    InvestigationNodeKind,
    InvestigationReportV1,
    InvestigationScopeV1,
    InvestigationSpecV1,
    InvestigationStopReason,
)
from app.contracts.jobs import AttemptLeaseV2
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.investigation_execution_tables import (
    investigation_commands,
    investigation_evidence,
    investigation_execution_checkpoints,
    investigation_publication_receipts,
    investigation_reports,
    investigation_specs,
    investigation_step_receipts,
    model_call_results,
)
from app.models.investigation_tables import (
    investigation_checkpoints,
    investigation_events,
    investigations,
)
from app.models.phase2_tables import md
from app.repositories.investigation_execution_repository import (
    SqlInvestigationCommandRepository,
    SqlInvestigationEvidenceRepository,
    SqlInvestigationExecutionCheckpointRepository,
    SqlInvestigationReportRepository,
    SqlInvestigationSpecRepository,
    SqlInvestigationStepReceiptRepository,
)
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with all investigation execution tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    investigations,
                    investigation_checkpoints,
                    investigation_events,
                    investigation_specs,
                    investigation_execution_checkpoints,
                    investigation_step_receipts,
                    model_call_results,
                    investigation_evidence,
                    investigation_reports,
                    investigation_publication_receipts,
                    investigation_commands,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _make_scope(org_id: UUID, inv_id: UUID, spec_hash: str) -> InvestigationScopeV1:
    return InvestigationScopeV1(
        org_id=org_id,
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        investigation_id=inv_id,
        spec_hash=spec_hash,
    )


def _make_state(scope: InvestigationScopeV1, ordinal: int = 0) -> InvestigationExecutionStateV1:
    return InvestigationExecutionStateV1(
        investigation_id=scope.investigation_id,
        scope_hash=hashlib.sha256(canonical_bytes(scope.model_dump())).hexdigest(),
        spec_hash=scope.spec_hash,
        execution_revision=1,
        next_node_kind=InvestigationNodeKind.INVESTIGATE,
        next_role="investigator",
        next_ordinal=ordinal,
        checkpoint_sequence=ordinal,
    )


def _make_lease(org_id: UUID, fence: int = 1) -> AttemptLeaseV2:
    now = datetime.now(UTC)
    return AttemptLeaseV2(
        org_id=org_id,
        job_id=uuid4(),
        attempt_id=uuid4(),
        fence=fence,
        owner="worker-1",
        lease_expires_at=now + timedelta(minutes=5),
        heartbeat_at=now,
    )


# ---------------------------------------------------------------------------
# Checkpoint Repository Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_append_and_load_latest(session: AsyncSession) -> None:
    repo = SqlInvestigationExecutionCheckpointRepository()
    org_id = uuid4()
    inv_id = uuid4()
    spec_hash = hashlib.sha256(b"spec").hexdigest()
    scope = _make_scope(org_id, inv_id, spec_hash)
    lease = _make_lease(org_id)

    # Initial append: sequence 0 (expected predecessor -1)
    state0 = _make_state(scope, ordinal=0)
    cp0 = await repo.append(session, lease, expected_previous_sequence=-1, state=state0)
    assert cp0.checkpoint_sequence == 0
    assert cp0.state_digest == state0.compute_state_digest()

    # Load latest should return cp0
    latest = await repo.load_latest(session, scope)
    assert latest is not None
    assert latest.checkpoint_sequence == 0
    assert latest.state_digest == cp0.state_digest

    # Second append: sequence 1 (expected predecessor 0)
    state1 = _make_state(scope, ordinal=1)
    cp1 = await repo.append(session, lease, expected_previous_sequence=0, state=state1)
    assert cp1.checkpoint_sequence == 1

    # Load latest now returns cp1
    latest2 = await repo.load_latest(session, scope)
    assert latest2 is not None
    assert latest2.checkpoint_sequence == 1


@pytest.mark.asyncio
async def test_checkpoint_idempotent_replay(session: AsyncSession) -> None:
    repo = SqlInvestigationExecutionCheckpointRepository()
    org_id = uuid4()
    inv_id = uuid4()
    spec_hash = hashlib.sha256(b"spec").hexdigest()
    scope = _make_scope(org_id, inv_id, spec_hash)
    lease = _make_lease(org_id)

    state0 = _make_state(scope, ordinal=0)
    cp0 = await repo.append(session, lease, expected_previous_sequence=-1, state=state0)

    # Re-appending identical state at sequence 0 returns existing checkpoint
    cp0_replay = await repo.append(session, lease, expected_previous_sequence=-1, state=state0)
    assert cp0_replay.checkpoint_sequence == 0
    assert cp0_replay.state_digest == cp0.state_digest


@pytest.mark.asyncio
async def test_checkpoint_replay_conflict_rejected(session: AsyncSession) -> None:
    repo = SqlInvestigationExecutionCheckpointRepository()
    org_id = uuid4()
    inv_id = uuid4()
    spec_hash = hashlib.sha256(b"spec").hexdigest()
    scope = _make_scope(org_id, inv_id, spec_hash)
    lease = _make_lease(org_id)

    state0 = _make_state(scope, ordinal=0)
    await repo.append(session, lease, expected_previous_sequence=-1, state=state0)

    # Different state at the same sequence raises DuplicateConflict
    state0_diff = _make_state(scope, ordinal=99)
    with pytest.raises(DuplicateConflict, match="conflict"):
        await repo.append(session, lease, expected_previous_sequence=-1, state=state0_diff)


@pytest.mark.asyncio
async def test_checkpoint_missing_predecessor_rejected(session: AsyncSession) -> None:
    repo = SqlInvestigationExecutionCheckpointRepository()
    org_id = uuid4()
    inv_id = uuid4()
    spec_hash = hashlib.sha256(b"spec").hexdigest()
    scope = _make_scope(org_id, inv_id, spec_hash)
    lease = _make_lease(org_id)

    state5 = _make_state(scope, ordinal=5)
    # Sequence gap: expecting previous sequence 4 when table is empty
    with pytest.raises(ScopeViolation, match="not found"):
        await repo.append(session, lease, expected_previous_sequence=4, state=state5)


# ---------------------------------------------------------------------------
# Spec Repository Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spec_save_and_get(session: AsyncSession) -> None:
    repo = SqlInvestigationSpecRepository()
    org_id = uuid4()
    spec = InvestigationSpecV1(
        org_id=org_id,
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        target_type="repository",
        query="Verify auth module security",
        mode="short_path",
        depth="standard",
        disclosure_policy_digest=hashlib.sha256(b"disclosure").hexdigest(),
        tool_policy_revision="v1",
        model_route_revision="v1",
        model_profile_revision="v1",
        privacy_policy_revision="v1",
    )

    saved = await repo.save_spec(session, spec)
    spec_hash = saved.compute_spec_hash()

    retrieved = await repo.get_spec(session, org_id, spec_hash)
    assert retrieved is not None
    assert retrieved.compute_spec_hash() == spec_hash
    assert retrieved.query == spec.query


@pytest.mark.asyncio
async def test_spec_save_idempotent(session: AsyncSession) -> None:
    repo = SqlInvestigationSpecRepository()
    org_id = uuid4()
    spec = InvestigationSpecV1(
        org_id=org_id,
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        target_type="repository",
        query="Verify auth module security",
        mode="short_path",
        depth="standard",
        disclosure_policy_digest=hashlib.sha256(b"disclosure").hexdigest(),
        tool_policy_revision="v1",
        model_route_revision="v1",
        model_profile_revision="v1",
        privacy_policy_revision="v1",
    )

    s1 = await repo.save_spec(session, spec)
    s2 = await repo.save_spec(session, spec)
    assert s1.compute_spec_hash() == s2.compute_spec_hash()


# ---------------------------------------------------------------------------
# Command Repository Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_record_and_replay(session: AsyncSession) -> None:
    repo = SqlInvestigationCommandRepository()
    org_id = uuid4()
    key = "create-inv-1"
    digest = hashlib.sha256(b"req1").hexdigest()
    principal_id = uuid4()
    inv_id = uuid4()
    response = {"status": "created", "investigation_id": str(inv_id)}

    # Initial replay is None
    assert await repo.replay(session, org_id, principal_id, "create", key, digest) is None

    # Record
    await repo.record(
        session,
        org_id=org_id,
        command_kind="create",
        idempotency_key=key,
        request_digest=digest,
        principal_id=principal_id,
        investigation_id=inv_id,
        job_id=None,
        status_code=201,
        response_json=response,
    )

    # Replay returns cached response
    replayed = await repo.replay(session, org_id, principal_id, "create", key, digest)
    assert replayed == response

    # Conflicting digest raises DuplicateConflict
    with pytest.raises(DuplicateConflict, match="conflicts"):
        await repo.replay(
            session, org_id, principal_id, "create", key, hashlib.sha256(b"different").hexdigest()
        )


# ---------------------------------------------------------------------------
# Evidence Repository Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_record_and_list(session: AsyncSession) -> None:
    repo = SqlInvestigationEvidenceRepository()
    org_id = uuid4()
    inv_id = uuid4()
    snap_id = uuid4()

    ev1 = InvestigationEvidenceV1(
        id=uuid4(),
        org_id=org_id,
        investigation_id=inv_id,
        snapshot_id=snap_id,
        source_path="app/auth.py",
        file_sha256=hashlib.sha256(b"file").hexdigest(),
        original_start_line=10,
        original_end_line=25,
        safe_start_line=10,
        safe_end_line=25,
        excerpt_sha256=hashlib.sha256(b"excerpt").hexdigest(),
        observation_type="source_read",
        coverage_summary="read 15 lines",
    )

    recorded_id = await repo.record_evidence(session, ev1)
    assert recorded_id == ev1.id

    evidence_list = await repo.list_evidence(session, org_id, inv_id)
    assert len(evidence_list) == 1
    assert evidence_list[0].source_path == "app/auth.py"
    assert evidence_list[0].safe_end_line == 25


# ---------------------------------------------------------------------------
# Step Receipt Repository Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_receipt_record_and_get(session: AsyncSession) -> None:
    repo = SqlInvestigationStepReceiptRepository()
    org_id = uuid4()
    inv_id = uuid4()
    node_id = uuid4()

    await repo.record_receipt(
        session,
        org_id=org_id,
        investigation_id=inv_id,
        execution_revision=1,
        node_id=node_id,
        node_kind="investigate",
        role="investigator",
        logical_ordinal=0,
        input_digest=hashlib.sha256(b"input").hexdigest(),
        state="completed",
        attempt_id=uuid4(),
        result_digest=hashlib.sha256(b"result").hexdigest(),
    )

    receipt = await repo.get_receipt(session, org_id, inv_id, 1, node_id)
    assert receipt is not None
    assert receipt["state"] == "completed"
    assert receipt["role"] == "investigator"


# ---------------------------------------------------------------------------
# Report & Publication Repository Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_publish_and_get(session: AsyncSession) -> None:
    repo = SqlInvestigationReportRepository()
    org_id = uuid4()
    inv_id = uuid4()
    job_id = uuid4()
    attempt_id = uuid4()
    spec_hash = hashlib.sha256(b"spec").hexdigest()
    now = datetime.now(UTC)

    # Insert prerequisite investigation row
    await session.execute(
        insert(investigations).values(
            org_id=org_id,
            id=inv_id,
            repository_id=uuid4(),
            principal_id=uuid4(),
            target_type="repository",
            query="test report publication",
            status="running",
            budget_limit_micros=1_000_000,
        )
    )

    report = InvestigationReportV1(
        report_id=uuid4(),
        investigation_id=inv_id,
        spec_hash=spec_hash,
        runtime_revision=1,
        verdict="true_positive",
        verdict_confidence=0.95,
        summary="Security flaw verified in auth flow",
        claims=(),
        verifications=(),
        evidence_refs=(),
        stop_reason=InvestigationStopReason.COMPLETED,
        report_digest=hashlib.sha256(b"rep1").hexdigest(),
        created_at=now,
    )

    draft_digest = hashlib.sha256(b"draft").hexdigest()
    receipt = await repo.publish_report(
        session,
        report=report,
        job_id=job_id,
        attempt_id=attempt_id,
        fencing_token=1,
        draft_digest=draft_digest,
    )
    assert receipt is not None
    assert receipt.investigation_id == inv_id
    assert receipt.job_id == job_id
    assert receipt.attempt_fence == 1
    assert receipt.report_id == report.report_id

    # Retrieve report by id
    fetched = await repo.get_report(session, org_id, report.report_id)
    assert fetched is not None
    assert fetched.verdict == "true_positive"
    assert fetched.summary == report.summary

    # Retrieve report by job
    by_job = await repo.get_report_by_job(session, org_id, job_id)
    assert by_job is not None
    assert by_job.report_id == report.report_id

    # Retrieve publication receipt
    pub_rcpt = await repo.get_publication_receipt(session, org_id, job_id)
    assert pub_rcpt is not None
    assert pub_rcpt.receipt_id == receipt.receipt_id
    assert pub_rcpt.report_digest == receipt.report_digest

    # Idempotent replay of same publication
    replay = await repo.publish_report(
        session,
        report=report,
        job_id=job_id,
        attempt_id=attempt_id,
        fencing_token=1,
        draft_digest=draft_digest,
    )
    assert replay.receipt_id == receipt.receipt_id
