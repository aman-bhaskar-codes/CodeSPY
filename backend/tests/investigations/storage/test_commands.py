"""Storage and concurrency tests for investigation commands and lifecycle.

Tests atomic create, start, cancel, resume, and retry commands,
idempotent replay, conflicting request keys, one-active-job constraints,
and lifecycle state machine transitions.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation import (
    InvestigationDepth,
    InvestigationMode,
)
from app.contracts.investigation_jobs import (
    InvestigationCreateRequestV2,
    InvestigationResumeRequestV1,
    InvestigationRetryRequestV1,
)
from app.domain.errors import DuplicateConflict, InvalidTransition, ScopeViolation
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
from sqlalchemy import event, insert
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
    cleaned = element.text.replace("::jsonb", "").replace("::text", "")
    if cleaned != element.text:
        element = element._clone()
        element.text = cleaned
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
        seconds = int(val.total_seconds())
        sign = "+" if seconds >= 0 else ""
        return f"strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '{sign}{seconds} seconds')"
    if "interval" in str(element.right):
        return "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+15 minutes')"
    return compiler.visit_binary(element, **kw)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite session factory with all required tables."""
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


# ---------------------------------------------------------------------------
# Create Command Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_investigation_success(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
        mode=InvestigationMode.SHORT_PATH,
        depth=InvestigationDepth.STANDARD,
        max_steps=10,
        budget_limit_micros=2_000_000,
    )
    summary = await svc.create(ctx, req, request_key="key-create-1")
    assert summary is not None
    assert summary.status == "pending"
    assert summary.repository_id == repo_id
    assert summary.snapshot_id == snap_id
    assert summary.spec_hash is not None
    assert summary.current_job_id is None


@pytest.mark.asyncio
async def test_create_idempotent_replay(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    sum1 = await svc.create(ctx, req, request_key="key-create-rep")
    sum2 = await svc.create(ctx, req, request_key="key-create-rep")
    assert sum1.id == sum2.id
    assert sum1.spec_hash == sum2.spec_hash


@pytest.mark.asyncio
async def test_create_conflicting_payload_fails(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req1 = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    req2 = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Different query entirely",
        target_type="repository",
    )
    await svc.create(ctx, req1, request_key="key-conflict")
    with pytest.raises(DuplicateConflict):
        await svc.create(ctx, req2, request_key="key-conflict")


@pytest.mark.asyncio
async def test_create_different_actor_fails(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    await svc.create(ctx, req, request_key="key-actor")

    other_ctx = TenantContext(org_id=ctx.org_id, principal_id=uuid4(), role=OrgRole.MEMBER)
    # Grant other actor write permission on repo
    async with session_factory() as s, s.begin():
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=repo_id,
                principal_id=other_ctx.principal_id,
                permission="write",
            )
        )

    with pytest.raises(DuplicateConflict):
        await svc.create(other_ctx, req, request_key="key-actor")


# ---------------------------------------------------------------------------
# Start Command Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_investigation_success(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="key-create-for-start")

    start_resp = await svc.start(ctx, created.id, request_key="key-start-1")
    assert start_resp is not None
    assert start_resp.investigation_id == created.id
    assert start_resp.job_id is not None
    assert "events" in start_resp.events_url


@pytest.mark.asyncio
async def test_start_idempotent_replay(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="key-start-rep-cr")

    r1 = await svc.start(ctx, created.id, request_key="key-start-rep")
    r2 = await svc.start(ctx, created.id, request_key="key-start-rep")
    assert r1.job_id == r2.job_id


@pytest.mark.asyncio
async def test_start_two_different_keys_same_investigation_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="key-start-double-cr")

    await svc.start(ctx, created.id, request_key="key-start-k1")
    with pytest.raises(DuplicateConflict, match="active job"):
        await svc.start(ctx, created.id, request_key="key-start-k2")


# ---------------------------------------------------------------------------
# Cancel Command Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_investigation_queued(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="key-canc-cr")
    await svc.start(ctx, created.id, request_key="key-canc-st")

    cancelled = await svc.cancel(ctx, created.id, request_key="key-canc-1")
    assert cancelled.investigation_id == created.id
    assert cancelled.already_terminal is False

    # Second cancel is idempotent and indicates already_terminal
    canc2 = await svc.cancel(ctx, created.id, request_key="key-canc-2")
    assert canc2.already_terminal is True


# ---------------------------------------------------------------------------
# Resume and Retry Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_rejected_if_not_awaiting_decision(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="key-resume-cr")

    # In pending state, resume should be rejected
    resume_req = InvestigationResumeRequestV1(
        expected_failed_job_id=uuid4(),
        expected_spec_hash=created.spec_hash or "a" * 64,
    )
    with pytest.raises((InvalidTransition, ScopeViolation), match="awaiting_human_decision"):
        await svc.resume(ctx, created.id, resume_req, request_key="key-res-1")


@pytest.mark.asyncio
async def test_retry_creates_new_pending_investigation(
    session_factory: async_sessionmaker[AsyncSession],
    ctx_and_repo: tuple[TenantContext, UUID, UUID],
) -> None:
    ctx, repo_id, snap_id = ctx_and_repo
    svc = InvestigationJobService(session_factory)

    req = InvestigationCreateRequestV2(
        repository_id=repo_id,
        snapshot_id=snap_id,
        query="Verify auth flow security",
        target_type="repository",
    )
    created = await svc.create(ctx, req, request_key="key-retry-cr")
    await svc.start(ctx, created.id, request_key="key-retry-st")
    await svc.cancel(ctx, created.id, request_key="key-retry-ca")

    retry_req = InvestigationRetryRequestV1(max_steps=15, budget_limit_micros=3_000_000)
    retried = await svc.retry(ctx, created.id, retry_req, request_key="key-retry-cmd")
    assert retried is not None
    assert retried.id != created.id
    assert retried.retry_of_id == created.id
    assert retried.status == "pending"
    assert retried.max_steps == 15
    assert retried.budget_limit_micros == 3_000_000


async def test_retry_rejects_another_current_writer(session_factory, ctx_and_repo):
    ctx, repo, snap = ctx_and_repo
    svc = InvestigationJobService(session_factory)
    created = await svc.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=repo, snapshot_id=snap, query="Check source", target_type="repository"
        ),
        request_key="original-create",
    )
    await svc.start(ctx, created.id, request_key="original-start")
    await svc.cancel(ctx, created.id, request_key="original-cancel")
    other = TenantContext(org_id=ctx.org_id, principal_id=uuid4(), role=OrgRole.MEMBER)
    async with session_factory() as s, s.begin():
        await s.execute(
            insert(memberships).values(
                org_id=ctx.org_id, principal_id=other.principal_id, role="member"
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=repo,
                principal_id=other.principal_id,
                permission="write",
            )
        )
    with pytest.raises(ScopeViolation, match="original requesting principal"):
        await svc.retry(other, created.id, InvestigationRetryRequestV1(), request_key="other-retry")
