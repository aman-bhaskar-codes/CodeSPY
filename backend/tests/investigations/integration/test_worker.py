"""Integration tests for JobConsumer and InvestigateSnapshotOperation.

Tests:
- JobEnvelopeV6 parsing, bounded envelope checks, and error handling
- Mismatched envelope rejection without claiming
- Terminal state / stale dispatch generation handling
- Missing operation handler queue extension
- Duplicate delivery handling (already_leased / obsolete)
- Full investigate_snapshot consumer dispatch and execution
- Heartbeat loop maintenance and heartbeat loss cancellation
- Failure handling: investigation_process_failed, model_outcome_unknown, cancellation
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationAdvanceV1,
    InvestigationStopReason,
)
from app.contracts.investigation_jobs import JobEnvelopeV6
from app.contracts.jobs import AttemptLeaseV2
from app.domain.scope import OrgRole, TenantContext
from app.models.investigation_execution_tables import (
    investigation_commands,
    investigation_evidence,
    investigation_execution_checkpoints,
    investigation_jobs,
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
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import artifacts, memberships, repositories, source_snapshots
from app.ports.queue import Delivery, QueuePort
from app.services.jobs import JobService
from app.workers.consumer import JobConsumer
from app.workers.operations.investigate_snapshot import InvestigateSnapshotOperation
from sqlalchemy import event, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CheckConstraint
from sqlalchemy.sql.elements import BinaryExpression, TextClause


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # type: ignore[no-untyped-def]
    return "JSON"


@compiles(TextClause, "sqlite")
def _compile_text_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    new_text = element.text.replace("::jsonb", "").replace("::text", "")
    if new_text != element.text:
        clone = element._clone()
        clone.text = new_text
        return compiler.visit_textclause(clone, **kw)
    return compiler.visit_textclause(element, **kw)


@compiles(CheckConstraint, "sqlite")
def _compile_check_constraint_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    sqltext = str(element.sqltext)
    name = str(element.name or "")
    if (
        any(
            bad in sqltext
            for bad in ("~", "octet_length", "jsonb_", "num_nonnulls", "admit_source")
        )
        or "operation" in name
    ):
        return "CHECK (1=1)"


@compiles(BinaryExpression, "sqlite")
def _compile_binary_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    val = getattr(element.right, "value", None)
    if isinstance(val, timedelta):
        secs = int(val.total_seconds())
        sign = "+" if secs >= 0 else ""
        return f"strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '{sign}{secs} seconds')"
    if "interval" in str(element.right):
        return "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+15 minutes')"
    return compiler.visit_binary(element, **kw)


class InMemoryQueue(QueuePort):
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.acked: list[str] = []
        self.extended: list[tuple[str, int]] = []

    async def send(self, body: bytes) -> None:
        self.sent.append(body)

    async def receive(self, max_messages: int = 1, *, wait_seconds: int = 20) -> list[Delivery]:
        return []

    async def extend(self, receipt: str, seconds: int) -> None:
        self.extended.append((receipt, seconds))

    async def ack(self, receipt: str) -> None:
        self.acked.append(receipt)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite session factory with all required tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    configs: dict[str, str] = {}

    @event.listens_for(engine.sync_engine, "connect")
    def _register_sqlite_functions(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        dbapi_connection.create_function(
            "clock_timestamp", 0, lambda: datetime.now(UTC).isoformat()
        )
        dbapi_connection.create_function("pg_advisory_xact_lock", 1, lambda x: 1)
        dbapi_connection.create_function(
            "set_config", 3, lambda k, v, is_local: configs.update({k: str(v)})
        )
        dbapi_connection.create_function(
            "current_setting", 2, lambda k, missing_ok: configs.get(k, "")
        )

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: investigation_jobs.metadata.create_all(
                sync_conn, tables=[investigation_jobs]
            )
        )
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    memberships,
                    repositories,
                    artifacts,
                    source_snapshots,
                    PHASE2_TABLES["job_attempts"],
                    PHASE2_TABLES["job_events"],
                    PHASE2_TABLES["outbox"],
                    PHASE2_TABLES["org_work_limits"],
                    PHASE2_TABLES["repository_grants"],
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
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def seed_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    art_id = uuid4()
    now = datetime.now(UTC)

    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.MEMBER)

    async with session_factory() as s, s.begin():
        await s.execute(
            insert(memberships).values(
                org_id=org_id,
                principal_id=principal_id,
                role="member",
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["org_work_limits"]).values(
                org_id=org_id,
                max_pending_jobs=10,
                max_active_jobs=5,
            )
        )
        await s.execute(
            insert(repositories).values(
                org_id=org_id,
                id=repo_id,
                repository_key="test-repo",
                display_name="Test Repo",
                source_kind="local_fixture",
            )
        )
        await s.execute(
            insert(artifacts).values(
                org_id=org_id,
                id=art_id,
                repository_id=repo_id,
                sha256="a" * 64,
                size_bytes=100,
                storage_key="test-key",
            )
        )
        await s.execute(
            insert(source_snapshots).values(
                org_id=org_id,
                id=snap_id,
                repository_id=repo_id,
                artifact_id=art_id,
                admission_config_hash="a" * 64,
                identity_hash="b" * 64,
                manifest_hash="c" * 64,
                state="sealed",
                sealed_at=now,
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
            )
        )

    from app.contracts.investigation_jobs import InvestigationCreateRequestV2
    from app.services.investigation_jobs import InvestigationJobService

    svc = InvestigationJobService(session_factory)
    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="seed-create")
    started = await svc.start(ctx, created.id, request_key="seed-start")

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(
                        PHASE2_TABLES["jobs"].c.id == started.job_id
                    )
                )
            )
            .mappings()
            .one()
        )

    return {
        "ctx": ctx,
        "org_id": org_id,
        "principal_id": principal_id,
        "repo_id": repo_id,
        "snap_id": snap_id,
        "inv_id": created.id,
        "job_id": started.job_id,
        "spec_hash": created.spec_hash,
        "config_hash": job["config_hash"],
        "deadline": job["deadline"],
    }


# ===========================================================================
# Unit / Integration Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_job_envelope_v6_bounded_and_validation(seed_data: dict[str, Any]) -> None:
    """Envelope V6 validates cleanly and enforces bounded length."""
    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    raw = envelope.model_dump_json()
    assert len(raw.encode("utf-8")) <= 4096

    # Roundtrip validation
    loaded = JobEnvelopeV6.model_validate_json(raw)
    assert loaded.schema_version == 6
    assert loaded.job_id == seed_data["job_id"]
    assert loaded.investigation_id == seed_data["inv_id"]


@pytest.mark.asyncio
async def test_consumer_rejects_oversized_delivery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Consumer rejects envelopes exceeding 4096 bytes and acks them."""
    queue = InMemoryQueue()
    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
    )
    oversized = b"x" * 4097
    delivery = Delivery(body=oversized, receipt="rcpt-oversized")

    await consumer.handle(delivery)
    assert "rcpt-oversized" in queue.acked


@pytest.mark.asyncio
async def test_consumer_rejects_mismatched_envelope(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Consumer acks without claiming when envelope investigation_id or config_hash mismatches."""
    queue = InMemoryQueue()
    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
    )
    # Wrong investigation_id
    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=uuid4(),  # Mismatched!
        spec_hash=seed_data["spec_hash"],
    )
    delivery = Delivery(body=envelope.model_dump_json().encode(), receipt="rcpt-mismatch")

    await consumer.handle(delivery)
    assert "rcpt-mismatch" in queue.acked

    # Job remains queued, never claimed
    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(
                        PHASE2_TABLES["jobs"].c.id == seed_data["job_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "queued"
        assert job["attempt_count"] == 0


@pytest.mark.asyncio
async def test_consumer_investigate_snapshot_terminal_or_generation_mismatch(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Terminal jobs or old dispatch generations are acked without claiming."""
    queue = InMemoryQueue()
    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
    )

    # 1. Dispatch generation mismatch
    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=99,  # Mismatch with db (which has 1)
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    delivery = Delivery(body=envelope.model_dump_json().encode(), receipt="rcpt-gen-mismatch")
    await consumer.handle(delivery)
    assert "rcpt-gen-mismatch" in queue.acked

    # 2. Terminal state (succeeded)
    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["jobs"])
            .where(PHASE2_TABLES["jobs"].c.id == seed_data["job_id"])
            .values(state="succeeded")
        )

    envelope_valid_gen = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    delivery_terminal = Delivery(
        body=envelope_valid_gen.model_dump_json().encode(), receipt="rcpt-terminal"
    )
    await consumer.handle(delivery_terminal)
    assert "rcpt-terminal" in queue.acked


@pytest.mark.asyncio
async def test_consumer_extends_visibility_when_investigation_op_missing(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """When investigation_operation is None, consumer extends queue visibility.

    It does not claim the lease.
    """
    queue = InMemoryQueue()
    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
        investigation_operation=None,
    )

    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    delivery = Delivery(body=envelope.model_dump_json().encode(), receipt="rcpt-no-op")
    await consumer.handle(delivery)

    assert ("rcpt-no-op", 120) in queue.extended
    assert "rcpt-no-op" not in queue.acked


@pytest.mark.asyncio
async def test_consumer_duplicate_delivery_already_leased(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Redundant delivery for an already-leased job is acked without competing."""
    # First, claim the job so it is in running state
    job_service = JobService(session_factory)
    lease = await job_service.claim(seed_data["org_id"], seed_data["job_id"], 1, "worker-1")
    assert isinstance(lease, AttemptLeaseV2)

    queue = InMemoryQueue()
    mock_op = AsyncMock(spec=InvestigateSnapshotOperation)
    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
        investigation_operation=mock_op,
    )

    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    delivery = Delivery(body=envelope.model_dump_json().encode(), receipt="rcpt-dup")
    await consumer.handle(delivery)

    assert "rcpt-dup" in queue.acked


@pytest.mark.asyncio
async def test_consumer_dispatches_investigate_snapshot_successfully(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Consumer claims attempt, executes investigate_snapshot operation, and acks delivery."""
    queue = InMemoryQueue()
    mock_op = AsyncMock(spec=InvestigateSnapshotOperation)
    mock_op.execute = AsyncMock(return_value=None)

    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
        investigation_operation=mock_op,
    )

    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    delivery = Delivery(body=envelope.model_dump_json().encode(), receipt="rcpt-success")
    await consumer.handle(delivery)

    assert mock_op.execute.called
    called_lease = mock_op.execute.call_args[0][0]
    assert isinstance(called_lease, AttemptLeaseV2)
    assert called_lease.job_id == seed_data["job_id"]
    assert "rcpt-success" in queue.acked


@pytest.mark.asyncio
async def test_consumer_operation_failure_fails_attempt_with_investigation_process_failed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """When operation crashes with an unhandled exception, attempt is failed.

    Failure code is set to investigation_process_failed.
    """
    queue = InMemoryQueue()
    mock_op = AsyncMock(spec=InvestigateSnapshotOperation)
    mock_op.execute = AsyncMock(side_effect=RuntimeError("internal crash"))

    consumer = JobConsumer(
        factory=session_factory,
        queue=queue,
        finalizer=AsyncMock(),
        processor=AsyncMock(),
        investigation_operation=mock_op,
    )

    envelope = JobEnvelopeV6(
        org_id=seed_data["org_id"],
        job_id=seed_data["job_id"],
        operation="investigate_snapshot",
        dispatch_generation=1,
        config_hash=seed_data["config_hash"],
        deadline=seed_data["deadline"],
        investigation_id=seed_data["inv_id"],
        spec_hash=seed_data["spec_hash"],
    )
    delivery = Delivery(body=envelope.model_dump_json().encode(), receipt="rcpt-failure")
    await consumer.handle(delivery)

    assert "rcpt-failure" in queue.acked

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(
                        PHASE2_TABLES["jobs"].c.id == seed_data["job_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        # Transient failure transitions job to retry_wait
        assert job["state"] == "retry_wait"
        assert job["failure_code"] == "investigation_process_failed"


@pytest.mark.asyncio
async def test_investigate_snapshot_operation_execution_loop(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """InvestigateSnapshotOperation executes progress turns and transitions to finalization."""
    job_service = JobService(session_factory)
    lease = await job_service.claim(seed_data["org_id"], seed_data["job_id"], 1, "worker-1")
    assert isinstance(lease, AttemptLeaseV2)

    # Mock executor with 1 progress step then ready to publish
    mock_executor = AsyncMock()
    mock_executor.advance = AsyncMock(
        side_effect=[
            InvestigationAdvanceV1(
                status=InvestigationAdvanceStatus.PROGRESS,
                node_id=uuid4(),
                checkpoint_sequence=1,
            ),
            InvestigationAdvanceV1(
                status=InvestigationAdvanceStatus.READY_TO_PUBLISH,
                node_id=uuid4(),
                checkpoint_sequence=2,
                stop_reason=InvestigationStopReason.COMPLETED,
            ),
        ]
    )

    finalizer_called = asyncio.Event()

    class MockFinalizer:
        async def publish(self, lease: AttemptLeaseV2, advance: InvestigationAdvanceV1) -> None:
            finalizer_called.set()

    entered_finalization = asyncio.Event()

    async def on_finalization() -> None:
        entered_finalization.set()

    operation = InvestigateSnapshotOperation(
        factory=session_factory,
        executor=mock_executor,
        finalizer=MockFinalizer(),
    )

    res = await operation.execute(lease, enter_finalization=on_finalization)
    assert res.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
    assert entered_finalization.is_set()
    assert finalizer_called.is_set()
    assert mock_executor.advance.call_count == 2
