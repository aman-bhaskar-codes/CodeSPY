"""Storage tests for atomic report publication and commit recovery (Task I).

Validates that publication:
1. Atomically commits report, publication receipt, jobs state=succeeded,
   attempts state=succeeded, and investigations status=completed.
2. Idempotently replays on exact matching draft digest.
3. Detects and rejects conflicting draft digest for the same job.
4. Fails safely on expired leases, dead-job fences, or cancelling states.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation import InvestigationDepth, InvestigationMode
from app.contracts.investigation_execution import (
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationReportDraftV1,
    InvestigationStopReason,
)
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.contracts.jobs import AttemptLeaseV2
from app.domain.errors import DuplicateConflict, ScopeViolation
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
from app.services.investigation_jobs import InvestigationJobService
from app.workers.investigation_finalizer import InvestigationFinalizer
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


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _register_sqlite_functions(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        dbapi_connection.create_function(
            "clock_timestamp", 0, lambda: datetime.now(UTC).isoformat()
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


async def _seed_running_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[TenantContext, AttemptLeaseV2, UUID, str]:
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    snap_id = uuid4()

    art_id = uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(memberships).values(
                org_id=org_id,
                principal_id=principal_id,
                role="member",
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["org_work_limits"]).values(
                org_id=org_id,
                max_pending_jobs=10,
                max_active_jobs=5,
            )
        )
        await session.execute(
            insert(repositories).values(
                org_id=org_id,
                id=repo_id,
                repository_key="test-repo",
                display_name="Test Repo",
                source_kind="local_fixture",
            )
        )
        await session.execute(
            insert(artifacts).values(
                org_id=org_id,
                id=art_id,
                repository_id=repo_id,
                sha256="a" * 64,
                size_bytes=100,
                storage_key="test-key",
            )
        )
        await session.execute(
            insert(source_snapshots).values(
                org_id=org_id,
                id=snap_id,
                repository_id=repo_id,
                artifact_id=art_id,
                admission_config_hash="a" * 64,
                identity_hash="b" * 64,
                manifest_hash="c" * 64,
                state="sealed",
                sealed_at=datetime.now(UTC),
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
            )
        )

    ctx = TenantContext(
        org_id=org_id,
        principal_id=principal_id,
        role=OrgRole.MEMBER,
    )

    job_service = InvestigationJobService(session_factory)
    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        target_type="repository",
        query="Audit security posture",
        mode=InvestigationMode.SHORT_PATH,
        depth=InvestigationDepth.STANDARD,
    )
    summary = await job_service.create(ctx, req, request_key="req-create-1")
    start_resp = await job_service.start(ctx, summary.id, request_key="req-start-1")

    # Claim job into running attempt
    job_id = start_resp.job_id
    attempt_id = uuid4()
    now_utc = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            update(investigation_jobs)
            .where(investigation_jobs.c.org_id == org_id, investigation_jobs.c.id == job_id)
            .values(state="running", fence=1, current_attempt_id=attempt_id)
        )
        await session.execute(
            insert(PHASE2_TABLES["job_attempts"]).values(
                org_id=org_id,
                job_id=job_id,
                id=attempt_id,
                owner="worker-1",
                state="active",
                started_at=now_utc,
                heartbeat_at=now_utc,
                lease_expires_at=now_utc + timedelta(minutes=15),
                fence=1,
            )
        )
        await session.commit()

    lease = AttemptLeaseV2(
        org_id=org_id,
        job_id=job_id,
        attempt_id=attempt_id,
        fence=1,
        owner="worker-1",
        lease_expires_at=now_utc + timedelta(minutes=15),
        heartbeat_at=now_utc,
    )
    return ctx, lease, summary.id, summary.spec_hash


def _make_draft(inv_id: UUID, spec_hash: str) -> InvestigationReportDraftV1:
    d = {
        "investigation_id": inv_id,
        "spec_hash": spec_hash,
        "verdict": "vulnerable",
        "verdict_confidence": 0.9,
        "summary": "Hardcoded secret confirmed in configuration.",
        "claims": (
            InvestigationClaimV2(
                claim_id=uuid4(),
                statement="Secret key hardcoded",
                kind=InvestigationClaimKind.SOURCE_OBSERVATION,
                suggested_file="config.py",
                suggested_line_start=1,
                suggested_line_end=1,
                suggested_quote="SECRET_KEY = 'insecure'",
            ),
        ),
        "verifications": (),
        "evidence_refs": (),
        "coverage_notes": "Single file reviewed",
        "unknowns": "",
        "limitations": "",
        "settled_usage_micros": 50_000,
        "held_usage_micros": 0,
        "stop_reason": InvestigationStopReason.COMPLETED,
        "current_support_available": True,
        "draft_digest": "0" * 64,
    }
    draft = InvestigationReportDraftV1.model_validate(d)
    digest = draft.compute_draft_digest()
    return draft.model_copy(update={"draft_digest": digest})


@pytest.mark.asyncio
async def test_publish_report_success(session_factory: async_sessionmaker[AsyncSession]):
    """Atomic publication writes report, receipt, transitions job/attempt/investigation."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)

    finalizer = InvestigationFinalizer(session_factory)
    receipt = await finalizer.publish(ctx, lease, draft)

    assert receipt.investigation_id == inv_id
    assert receipt.job_id == lease.job_id
    assert receipt.attempt_id == lease.attempt_id
    assert receipt.attempt_fence == lease.fence
    assert receipt.draft_digest == draft.draft_digest

    # Verify rows in DB
    async with session_factory() as session:
        job_row = (
            (
                await session.execute(
                    select(investigation_jobs).where(
                        investigation_jobs.c.org_id == lease.org_id,
                        investigation_jobs.c.id == lease.job_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert job_row["state"] == "succeeded"

        attempt_row = (
            (
                await session.execute(
                    select(PHASE2_TABLES["job_attempts"]).where(
                        PHASE2_TABLES["job_attempts"].c.org_id == lease.org_id,
                        PHASE2_TABLES["job_attempts"].c.id == lease.attempt_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert attempt_row["state"] == "succeeded"

        inv_row = (
            (
                await session.execute(
                    select(investigations).where(
                        investigations.c.org_id == lease.org_id,
                        investigations.c.id == inv_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert inv_row["status"] == "completed"
        assert (
            inv_row["verdict"] == "indeterminate"
        )  # Unsupported vocabulary never becomes a conclusive verdict.
        assert inv_row["final_report_id"] == receipt.report_id


@pytest.mark.asyncio
async def test_publish_report_exact_replay(session_factory: async_sessionmaker[AsyncSession]):
    """Publishing identical draft returns existing publication receipt."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)

    finalizer = InvestigationFinalizer(session_factory)
    receipt_1 = await finalizer.publish(ctx, lease, draft)
    receipt_2 = await finalizer.publish(ctx, lease, draft)

    assert receipt_1.receipt_id == receipt_2.receipt_id
    assert receipt_1.report_id == receipt_2.report_id


@pytest.mark.asyncio
async def test_publish_report_conflict(session_factory: async_sessionmaker[AsyncSession]):
    """Publishing a conflicting draft digest for the same job raises DuplicateConflict."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft_1 = _make_draft(inv_id, spec_hash)

    # Different summary -> different draft digest
    draft_dict = draft_1.model_dump()
    draft_dict["summary"] = "Completely different summary statement."
    draft_2_proto = InvestigationReportDraftV1.model_validate(draft_dict)
    draft_2 = draft_2_proto.model_copy(
        update={"draft_digest": draft_2_proto.compute_draft_digest()}
    )

    finalizer = InvestigationFinalizer(session_factory)
    await finalizer.publish(ctx, lease, draft_1)

    with pytest.raises(DuplicateConflict, match="conflict"):
        await finalizer.publish(ctx, lease, draft_2)


@pytest.mark.asyncio
async def test_publish_report_expired_lease_rejected(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Expired attempt lease rejects publication with ScopeViolation."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)

    # Expire attempt lease in DB
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        await session.execute(
            update(PHASE2_TABLES["job_attempts"])
            .where(
                PHASE2_TABLES["job_attempts"].c.org_id == lease.org_id,
                PHASE2_TABLES["job_attempts"].c.id == lease.attempt_id,
            )
            .values(lease_expires_at=past)
        )
        await session.commit()

    finalizer = InvestigationFinalizer(session_factory)
    with pytest.raises(ScopeViolation, match="lease expired"):
        await finalizer.publish(ctx, lease, draft)


@pytest.mark.asyncio
async def test_publish_report_cancelling_job_rejected(
    session_factory: async_sessionmaker[AsyncSession],
):
    """A cancelling or cancelled job cannot publish a success report."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)

    # Mark job cancelling
    async with session_factory() as session:
        await session.execute(
            update(investigation_jobs)
            .where(
                investigation_jobs.c.org_id == lease.org_id,
                investigation_jobs.c.id == lease.job_id,
            )
            .values(state="cancelling")
        )
        await session.commit()

    finalizer = InvestigationFinalizer(session_factory)
    with pytest.raises(ScopeViolation, match="cancelling"):
        await finalizer.publish(ctx, lease, draft)


async def test_replay_rejects_a_different_attempt_fence(session_factory):
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)
    finalizer = InvestigationFinalizer(session_factory)
    await finalizer.publish(ctx, lease, draft)
    stale = lease.model_copy(update={"fence": lease.fence + 1})
    with pytest.raises(DuplicateConflict):
        await finalizer.publish(ctx, stale, draft)


async def test_revoked_writer_cannot_publish(session_factory):
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    async with session_factory() as session, session.begin():
        await session.execute(
            PHASE2_TABLES["repository_grants"]
            .delete()
            .where(PHASE2_TABLES["repository_grants"].c.principal_id == ctx.principal_id)
        )
    with pytest.raises(ScopeViolation):
        await InvestigationFinalizer(session_factory).publish(
            ctx, lease, _make_draft(inv_id, spec_hash)
        )


async def test_cumulative_work_timeout_rolls_back_report(session_factory, monkeypatch):
    import asyncio

    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    finalizer = InvestigationFinalizer(session_factory, budget_seconds=0.03)
    original = finalizer._atomic_publish

    async def slow(session, current, draft):
        result = await original(session, current, draft)
        # Individually short awaits collectively exceed the aggregate deadline.
        for _ in range(6):
            await asyncio.sleep(0.01)
        return result

    monkeypatch.setattr(finalizer, "_atomic_publish", slow)
    with pytest.raises(TimeoutError):
        await finalizer.publish(ctx, lease, _make_draft(inv_id, spec_hash))
    async with session_factory() as session:
        assert (await session.execute(select(investigation_reports))).first() is None
        row = (
            (
                await session.execute(
                    select(investigation_jobs).where(investigation_jobs.c.id == lease.job_id)
                )
            )
            .mappings()
            .one()
        )
        assert row["state"] == "running"
