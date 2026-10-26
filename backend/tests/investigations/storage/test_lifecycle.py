"""Storage and lifecycle tests for investigations and jobs.

Tests state machine transitions:
- pending -> queued -> running -> completed / failed / cancelled
- transient retries (retry_wait) vs terminal failure
- model_outcome_unknown -> awaiting_human_decision
- resume from awaiting_human_decision
- cancel when running / queued
- terminal mappings consistency across job and investigation
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation_jobs import (
    InvestigationCreateRequestV2,
    InvestigationResumeRequestV1,
)
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
from app.services.investigation_jobs import InvestigationJobService
from app.services.jobs import JobService
from sqlalchemy import event, insert, select
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
async def ctx_and_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[TenantContext, UUID, UUID]:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    art_id = uuid4()
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
                sealed_at=datetime.now(UTC),
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

    return ctx, repo_id, snap_id


@pytest.mark.asyncio
async def test_lifecycle_claim_running_investigation(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    inv_svc = InvestigationJobService(session_factory)
    job_svc = JobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await inv_svc.create(ctx, req, request_key="key-lc-claim-cr")
    started = await inv_svc.start(ctx, created.id, request_key="key-lc-claim-st")
    assert started.job_id is not None

    # Worker claims the job
    worker_id = "worker-lifecycle-1"
    lease = await job_svc.claim(ctx.org_id, started.job_id, generation=1, owner=worker_id)
    assert isinstance(lease, AttemptLeaseV2)
    assert lease.job_id == started.job_id

    # Verify investigation updated to running
    async with session_factory() as s:
        inv = (
            (
                await s.execute(
                    select(investigations).where(
                        investigations.c.org_id == ctx.org_id,
                        investigations.c.id == created.id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert inv["status"] == "running"
        assert inv["worker_id"] == worker_id
        assert inv["current_job_id"] == started.job_id


@pytest.mark.asyncio
async def test_lifecycle_fail_attempt_retryable(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    inv_svc = InvestigationJobService(session_factory)
    job_svc = JobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await inv_svc.create(ctx, req, request_key="key-lc-retry-cr")
    started = await inv_svc.start(ctx, created.id, request_key="key-lc-retry-st")

    lease = await job_svc.claim(ctx.org_id, started.job_id, generation=1, owner="worker-retry-1")
    assert isinstance(lease, AttemptLeaseV2)

    # Fail transiently (transient=True, attempt 1 < 3)
    await job_svc.fail_attempt(lease, "provider_unavailable", transient=True)

    job_rec = await job_svc.get(ctx, started.job_id)
    assert job_rec.state == "retry_wait"
    assert job_rec.attempt_count == 1

    # Investigation remains non-terminal (checkpointed or running)
    async with session_factory() as s:
        inv = (
            (
                await s.execute(
                    select(investigations).where(
                        investigations.c.org_id == ctx.org_id,
                        investigations.c.id == created.id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert inv["status"] in ("checkpointed", "running", "pending")


@pytest.mark.asyncio
async def test_lifecycle_fail_attempt_model_uncertain_awaits_decision(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    inv_svc = InvestigationJobService(session_factory)
    job_svc = JobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await inv_svc.create(ctx, req, request_key="key-lc-model-cr")
    started = await inv_svc.start(ctx, created.id, request_key="key-lc-model-st")

    lease = await job_svc.claim(ctx.org_id, started.job_id, generation=1, owner="worker-model-1")
    assert isinstance(lease, AttemptLeaseV2)

    # Fail with model_outcome_unknown (non-transient)
    await job_svc.fail_attempt(lease, "model_outcome_unknown", transient=False)

    job_rec = await job_svc.get(ctx, started.job_id)
    assert job_rec.state == "failed"
    assert job_rec.failure_code == "model_outcome_unknown"

    # Investigation transitions to awaiting_human_decision per section 10
    async with session_factory() as s:
        inv = (
            (
                await s.execute(
                    select(investigations).where(
                        investigations.c.org_id == ctx.org_id,
                        investigations.c.id == created.id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert inv["status"] == "awaiting_human_decision"


@pytest.mark.asyncio
async def test_lifecycle_resume_from_awaiting_human_decision(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    inv_svc = InvestigationJobService(session_factory)
    job_svc = JobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await inv_svc.create(ctx, req, request_key="key-lc-res-cr")
    assert created.spec_hash is not None
    started = await inv_svc.start(ctx, created.id, request_key="key-lc-res-st")

    lease = await job_svc.claim(ctx.org_id, started.job_id, generation=1, owner="worker-res-1")
    assert isinstance(lease, AttemptLeaseV2)
    await job_svc.fail_attempt(lease, "model_outcome_unknown", transient=False)

    # Resume with expected failed job id and spec hash
    resume_req = InvestigationResumeRequestV1(
        expected_failed_job_id=started.job_id,
        expected_spec_hash=created.spec_hash,
    )
    resumed = await inv_svc.resume(ctx, created.id, resume_req, request_key="key-lc-res-cmd")

    assert resumed.investigation_id == created.id
    assert resumed.job_id != started.job_id
    assert resumed.status_url is not None


@pytest.mark.asyncio
async def test_lifecycle_cancel_running_job(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    inv_svc = InvestigationJobService(session_factory)
    job_svc = JobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await inv_svc.create(ctx, req, request_key="key-lc-canc-cr")
    started = await inv_svc.start(ctx, created.id, request_key="key-lc-canc-st")

    lease = await job_svc.claim(ctx.org_id, started.job_id, generation=1, owner="worker-canc-1")
    assert isinstance(lease, AttemptLeaseV2)

    # Cancel while running: job becomes cancelling
    cancelled = await inv_svc.cancel(ctx, created.id, request_key="key-lc-canc-cmd")
    assert cancelled.already_terminal is False

    job_rec = await job_svc.get(ctx, started.job_id)
    assert job_rec.state == "cancelling"

    # Broker / recovery / finalizer closes the cancelling job
    await job_svc.cancel(ctx, started.job_id)

    job_rec_final = await job_svc.get(ctx, started.job_id)
    assert job_rec_final.state == "cancelled"

    async with session_factory() as s:
        inv = (
            (
                await s.execute(
                    select(investigations).where(
                        investigations.c.org_id == ctx.org_id,
                        investigations.c.id == created.id,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert inv["status"] == "cancelled"
