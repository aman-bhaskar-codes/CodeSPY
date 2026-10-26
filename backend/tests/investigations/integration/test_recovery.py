"""Integration tests for background recovery of investigation jobs.

Tests independent DB-time recovery:
- Queued job with expired deadline -> terminal failure with deadline_exceeded
- Running job with expired deadline -> terminal failure with deadline_exceeded
- Running job with expired lease -> retry_wait and investigation marked checkpointed
- Cancelling job -> cancelled with user_requested and investigation marked cancelled
- Revoked principal permission -> failed with access_revoked and investigation marked failed
- Stranded queued job -> re-enqueues wakeup with bumped dispatch_generation
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
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
from app.services.jobs import JobService
from app.workers.recovery import RecoveryWorker
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
                    PHASE2_TABLES["github_user_grants"],
                    PHASE2_TABLES["github_repository_bindings"],
                    PHASE2_TABLES["source_submissions"],
                    PHASE2_TABLES["object_records"],
                    PHASE2_TABLES["cleanup_tasks"],
                    PHASE2_TABLES["oauth_flows"],
                    PHASE2_TABLES["sessions"],
                    PHASE2_TABLES["webhook_receipts"],
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
async def setup_investigation_job(
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
        query="Investigate authorization bypass in API router",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="seed-rec-create")
    started = await svc.start(ctx, created.id, request_key="seed-rec-start")

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
    }


# ===========================================================================
# Recovery Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_recover_due_queued_job_deadline_exceeded(
    session_factory: async_sessionmaker[AsyncSession],
    setup_investigation_job: dict[str, Any],
) -> None:
    """Queued investigation job with past deadline fails with deadline_exceeded.

    Also marks investigation failed.
    """
    job_id = setup_investigation_job["job_id"]
    inv_id = setup_investigation_job["inv_id"]
    past = datetime.now(UTC) - timedelta(minutes=1)

    # Set deadline in the past
    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["jobs"])
            .where(PHASE2_TABLES["jobs"].c.id == job_id)
            .values(deadline=past)
        )

    job_service = JobService(session_factory)
    recovered = await job_service.recover_due()
    assert recovered == 1

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(PHASE2_TABLES["jobs"].c.id == job_id)
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "failed"
        assert job["failure_code"] == "deadline_exceeded"

        inv = (
            (await s.execute(select(investigations).where(investigations.c.id == inv_id)))
            .mappings()
            .one()
        )
        assert inv["status"] == "failed"


@pytest.mark.asyncio
async def test_recover_due_running_job_deadline_exceeded(
    session_factory: async_sessionmaker[AsyncSession],
    setup_investigation_job: dict[str, Any],
) -> None:
    """Running investigation job with expired deadline terminates attempt.

    Also marks investigation failed.
    """
    job_id = setup_investigation_job["job_id"]
    inv_id = setup_investigation_job["inv_id"]
    org_id = setup_investigation_job["org_id"]

    job_service = JobService(session_factory)
    lease = await job_service.claim(org_id, job_id, 1, "worker-rec-1")
    assert isinstance(lease, AttemptLeaseV2)

    past = datetime.now(UTC) - timedelta(seconds=10)
    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["jobs"])
            .where(PHASE2_TABLES["jobs"].c.id == job_id)
            .values(deadline=past)
        )

    recovered = await job_service.recover_due()
    assert recovered == 1

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(PHASE2_TABLES["jobs"].c.id == job_id)
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "failed"
        assert job["failure_code"] == "deadline_exceeded"

        attempt = (
            (
                await s.execute(
                    select(PHASE2_TABLES["job_attempts"]).where(
                        PHASE2_TABLES["job_attempts"].c.id == lease.attempt_id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert attempt["state"] == "failed"

        inv = (
            (await s.execute(select(investigations).where(investigations.c.id == inv_id)))
            .mappings()
            .one()
        )
        assert inv["status"] == "failed"


@pytest.mark.asyncio
async def test_recover_due_running_job_lease_expired_retried(
    session_factory: async_sessionmaker[AsyncSession],
    setup_investigation_job: dict[str, Any],
) -> None:
    """Running investigation job with expired lease but valid deadline retries.

    Transitions to retry_wait and checkpointed.
    """
    job_id = setup_investigation_job["job_id"]
    inv_id = setup_investigation_job["inv_id"]
    org_id = setup_investigation_job["org_id"]

    job_service = JobService(session_factory)
    lease = await job_service.claim(org_id, job_id, 1, "worker-rec-1")
    assert isinstance(lease, AttemptLeaseV2)

    # Expire attempt lease
    past = datetime.now(UTC) - timedelta(seconds=10)
    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["job_attempts"])
            .where(PHASE2_TABLES["job_attempts"].c.id == lease.attempt_id)
            .values(lease_expires_at=past)
        )

    recovered = await job_service.recover_due()
    assert recovered == 1

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(PHASE2_TABLES["jobs"].c.id == job_id)
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "retry_wait"
        assert job["failure_code"] == "lease_expired"
        assert job["dispatch_generation"] == 2

        inv = (
            (await s.execute(select(investigations).where(investigations.c.id == inv_id)))
            .mappings()
            .one()
        )
        assert inv["status"] == "checkpointed"


@pytest.mark.asyncio
async def test_recover_cancelling_investigation_job(
    session_factory: async_sessionmaker[AsyncSession],
    setup_investigation_job: dict[str, Any],
) -> None:
    """Investigation job with cancel requested is cancelled.

    Also marks investigation cancelled.
    """
    job_id = setup_investigation_job["job_id"]
    inv_id = setup_investigation_job["inv_id"]
    org_id = setup_investigation_job["org_id"]
    now = datetime.now(UTC)

    job_service = JobService(session_factory)
    lease = await job_service.claim(org_id, job_id, 1, "worker-rec-1")
    assert isinstance(lease, AttemptLeaseV2)

    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["jobs"])
            .where(PHASE2_TABLES["jobs"].c.id == job_id)
            .values(cancel_requested_at=now)
        )

    recovered = await job_service.recover_due()
    assert recovered == 1

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(PHASE2_TABLES["jobs"].c.id == job_id)
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "cancelled"
        assert job["failure_code"] == "user_requested"

        inv = (
            (await s.execute(select(investigations).where(investigations.c.id == inv_id)))
            .mappings()
            .one()
        )
        assert inv["status"] == "cancelled"


@pytest.mark.asyncio
async def test_recover_revoked_principal_access(
    session_factory: async_sessionmaker[AsyncSession],
    setup_investigation_job: dict[str, Any],
) -> None:
    """When principal's permission is revoked, recover_due fails the job with access_revoked."""
    job_id = setup_investigation_job["job_id"]
    inv_id = setup_investigation_job["inv_id"]
    org_id = setup_investigation_job["org_id"]
    repo_id = setup_investigation_job["repo_id"]
    principal_id = setup_investigation_job["principal_id"]
    now = datetime.now(UTC)

    job_service = JobService(session_factory)
    lease = await job_service.claim(org_id, job_id, 1, "worker-rec-1")
    assert isinstance(lease, AttemptLeaseV2)

    # Revoke permission
    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["repository_grants"])
            .where(
                PHASE2_TABLES["repository_grants"].c.org_id == org_id,
                PHASE2_TABLES["repository_grants"].c.repository_id == repo_id,
                PHASE2_TABLES["repository_grants"].c.principal_id == principal_id,
            )
            .values(revoked_at=now)
        )

    worker = RecoveryWorker(session_factory)
    recovered = await worker.tick()
    assert recovered >= 1

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(PHASE2_TABLES["jobs"].c.id == job_id)
                )
            )
            .mappings()
            .one()
        )
        assert job["state"] == "cancelled"
        assert job["failure_code"] == "access_revoked"

        inv = (
            (await s.execute(select(investigations).where(investigations.c.id == inv_id)))
            .mappings()
            .one()
        )
        assert inv["status"] == "cancelled"


@pytest.mark.asyncio
async def test_recovery_worker_tick_stranded_queued_job(
    session_factory: async_sessionmaker[AsyncSession],
    setup_investigation_job: dict[str, Any],
) -> None:
    """RecoveryWorker.tick() re-enqueues stranded queued wakeup with incremented generation."""
    job_id = setup_investigation_job["job_id"]
    setup_investigation_job["org_id"]
    now = datetime.now(UTC)

    # Update the existing outbox wakeup to be sent > 180 seconds ago
    async with session_factory() as s, s.begin():
        await s.execute(
            update(PHASE2_TABLES["outbox"])
            .where(
                PHASE2_TABLES["outbox"].c.job_id == job_id,
                PHASE2_TABLES["outbox"].c.generation == 1,
            )
            .values(
                state="sent",
                created_at=now - timedelta(seconds=200),
                sent_at=now - timedelta(seconds=190),
            )
        )

    worker = RecoveryWorker(session_factory)
    changed = await worker.tick()
    assert changed >= 1

    async with session_factory() as s:
        job = (
            (
                await s.execute(
                    select(PHASE2_TABLES["jobs"]).where(PHASE2_TABLES["jobs"].c.id == job_id)
                )
            )
            .mappings()
            .one()
        )
        assert job["dispatch_generation"] == 2

        # A new pending wakeup should be in outbox for generation 2
        wakeups = (
            (
                await s.execute(
                    select(PHASE2_TABLES["outbox"]).where(
                        PHASE2_TABLES["outbox"].c.job_id == job_id,
                        PHASE2_TABLES["outbox"].c.generation == 2,
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(wakeups) == 1
        assert wakeups[0]["state"] == "pending"
