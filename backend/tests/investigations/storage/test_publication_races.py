"""Storage concurrency and race tests for report publication (Task I)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation import InvestigationDepth, InvestigationMode
from app.contracts.investigation_execution import (
    InvestigationReportDraftV1,
    InvestigationStopReason,
)
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.contracts.jobs import AttemptLeaseV2
from app.domain.errors import ScopeViolation
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
from sqlalchemy import event, insert, update
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
    assert summary.spec_hash is not None
    return ctx, lease, summary.id, summary.spec_hash


def _make_draft(inv_id: UUID, spec_hash: str) -> InvestigationReportDraftV1:
    d = {
        "investigation_id": inv_id,
        "spec_hash": spec_hash,
        "verdict": "benign",
        "verdict_confidence": 0.8,
        "summary": "No critical vulnerabilities found.",
        "claims": (),
        "verifications": (),
        "evidence_refs": (),
        "coverage_notes": "Scanned targeted files",
        "unknowns": "",
        "limitations": "",
        "settled_usage_micros": 20_000,
        "held_usage_micros": 0,
        "stop_reason": InvestigationStopReason.COMPLETED,
        "current_support_available": True,
        "draft_digest": "0" * 64,
    }
    draft = InvestigationReportDraftV1.model_validate(d)
    digest = draft.compute_draft_digest()
    return draft.model_copy(update={"draft_digest": digest})


@pytest.mark.asyncio
async def test_publication_ambiguous_commit_recovery(
    session_factory: async_sessionmaker[AsyncSession],
):
    """If a publication receipt is already committed, publish() returns it cleanly."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)

    finalizer = InvestigationFinalizer(session_factory)
    # First publication
    receipt_1 = await finalizer.publish(ctx, lease, draft)

    # Simulate ambiguous commit retry on new connection
    receipt_2 = await finalizer.publish(ctx, lease, draft)

    assert receipt_1.receipt_id == receipt_2.receipt_id
    assert receipt_1.draft_digest == receipt_2.draft_digest


@pytest.mark.asyncio
async def test_cancel_vs_publish_race(
    session_factory: async_sessionmaker[AsyncSession],
):
    """When job cancellation wins before publish commits, publish is aborted."""
    ctx, lease, inv_id, spec_hash = await _seed_running_job(session_factory)
    draft = _make_draft(inv_id, spec_hash)

    job_service = InvestigationJobService(session_factory)
    # Cancel wins
    await job_service.cancel(ctx, inv_id, request_key="req-cancel-race")

    finalizer = InvestigationFinalizer(session_factory)
    with pytest.raises(ScopeViolation, match=r"cancelling|cancelled"):
        await finalizer.publish(ctx, lease, draft)
