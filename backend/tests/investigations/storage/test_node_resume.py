"""Storage and resume tests for investigation execution nodes.

Verifies:
- SqlInvestigationJobAuthority: ordering, DB-time lease/deadline checks, fencing, cancellation
- SqlInvestigationExecutionCheckpointRepository: immutable sequential progression,
  idempotency, conflict detection
- InvestigationExecutorService integration with SQL storage: crash-and-resume consistency
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationExecutionContextV1,
    InvestigationExecutionStateV1,
    InvestigationLimitsV1,
    InvestigationNodeKind,
    InvestigationScopeV1,
    InvestigationSpecV1,
    compute_node_id,
)
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model import (
    ModelResponseV1,
    TokenUsageV1,
    ToolCallV1,
)
from app.domain.errors import DuplicateConflict
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
from app.models.phase2_tables import PHASE2_TABLES, attempts, md
from app.models.tables import artifacts, memberships, repositories, source_snapshots
from app.ports.investigation_execution import InvestigationToolPort
from app.repositories.investigation_execution_repository import (
    SqlInvestigationExecutionCheckpointRepository,
    SqlInvestigationSpecRepository,
    SqlInvestigationStepReceiptRepository,
)
from app.services.investigation_checkpointer import StaleWorkerFencingError
from app.services.investigation_executor import (
    InvestigationExecutorService,
    SqlInvestigationJobAuthority,
)
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


def _make_response(
    content: str,
    *,
    tool_calls: tuple[ToolCallV1, ...] = (),
    charge_micros: int = 1000,
) -> ModelResponseV1:
    return ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="synthetic",
        model_id="gpt-4o",
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=charge_micros,
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class DummyModelGateway:
    def __init__(self, responses: list[ModelResponseV1] | None = None) -> None:
        self.responses = list(responses or [])
        self.call_count: int = 0

    async def execute_call(self, **kwargs: Any) -> ModelResponseV1:
        self.call_count += 1
        if self.responses:
            return self.responses.pop(0)
        return _make_response(
            '{"verdict": "benign", "confidence": 0.9, "summary": "Safe", "claims": []}'
        )


class DummyTools(InvestigationToolPort):
    async def execute(self, request: Any, context: Any) -> Any:
        from app.contracts.investigation_execution import (
            InvestigationToolResultV1,
            InvestigationToolStatus,
        )

        return InvestigationToolResultV1(status=InvestigationToolStatus.OK, payload="dummy tool ok")


async def _seed_investigation_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_state: str = "running",
    lease_expires_delta: timedelta = timedelta(minutes=5),
    fence: int = 1,
) -> tuple[UUID, UUID, UUID, UUID, UUID, AttemptLeaseV2, InvestigationSpecV1]:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    job_id = uuid4()
    attempt_id = uuid4()
    inv_id = uuid4()

    spec = InvestigationSpecV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
        target_type="repository",
        query="Audit security findings",
        mode="short_path",
        depth="standard",
        limits=InvestigationLimitsV1(max_steps=5, budget_limit_micros=1_000_000),
        disclosure_policy_digest="0" * 64,
        tool_policy_revision="v1",
        model_route_revision="v1",
        model_profile_revision="v1",
        runtime_revision=1,
        privacy_policy_revision="v1",
    )
    spec_hash = spec.compute_spec_hash()

    now = datetime.now(UTC)
    lease_expires_at = now + lease_expires_delta
    deadline = now + timedelta(minutes=15)

    async with factory() as s, s.begin():
        # memberships
        await s.execute(
            insert(memberships).values(org_id=org_id, principal_id=principal_id, role="member")
        )
        # repositories
        await s.execute(
            insert(repositories).values(
                org_id=org_id,
                id=repo_id,
                repository_key="repo",
                display_name="repo",
                source_kind="git",
            )
        )
        # repository grants (write=True)
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=org_id, repository_id=repo_id, principal_id=principal_id, permission="write"
            )
        )
        art_id = uuid4()
        # artifacts
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
        # source_snapshots
        await s.execute(
            insert(source_snapshots).values(
                org_id=org_id,
                repository_id=repo_id,
                id=snap_id,
                artifact_id=art_id,
                admission_config_hash="a" * 64,
                identity_hash="b" * 64,
                manifest_hash="c" * 64,
                state="sealed",
                sealed_at=now,
                source_revision="a" * 40,
            )
        )
        # jobs (via investigation_jobs projection)
        await s.execute(
            insert(investigation_jobs).values(
                org_id=org_id,
                id=job_id,
                repository_id=repo_id,
                principal_id=principal_id,
                operation="investigate_snapshot",
                state=job_state,
                fence=fence,
                deadline=deadline,
                attempt_count=1,
                max_attempts=3,
                config_hash="0" * 64,
                request_key=f"req-{job_id}",
                request_hash="0" * 64,
                investigation_id=inv_id,
                spec_hash=spec_hash,
                created_at=now,
            )
        )
        # attempts
        await s.execute(
            insert(attempts).values(
                org_id=org_id,
                job_id=job_id,
                id=attempt_id,
                fence=fence,
                owner="worker-1",
                state="active",
                lease_expires_at=lease_expires_at,
                heartbeat_at=now,
                started_at=now,
            )
        )
        # investigations
        await s.execute(
            insert(investigations).values(
                org_id=org_id,
                id=inv_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                principal_id=principal_id,
                target_type="repository",
                query="Audit security findings",
                mode="short_path",
                depth="standard",
                budget_limit_micros=1_000_000,
                max_steps=5,
                status="running",
                current_job_id=job_id,
                spec_hash=spec_hash,
                execution_revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        # investigation_specs
        await s.execute(
            insert(investigation_specs).values(
                org_id=org_id,
                id=uuid4(),
                spec_hash=spec_hash,
                repository_id=repo_id,
                snapshot_id=snap_id,
                principal_id=principal_id,
                target_type="repository",
                query=spec.query,
                mode=spec.mode,
                depth=spec.depth,
                limits_json=spec.limits.model_dump(),
                disclosure_policy_digest=spec.disclosure_policy_digest,
                tool_policy_revision=spec.tool_policy_revision,
                model_route_revision=spec.model_route_revision,
                model_profile_revision=spec.model_profile_revision,
                runtime_revision=spec.runtime_revision,
                privacy_policy_revision=spec.privacy_policy_revision,
                created_at=now,
            )
        )

    lease = AttemptLeaseV2(
        org_id=org_id,
        job_id=job_id,
        attempt_id=attempt_id,
        fence=fence,
        owner="worker-1",
        lease_expires_at=lease_expires_at,
        heartbeat_at=now,
    )
    return org_id, repo_id, snap_id, principal_id, inv_id, lease, spec


@pytest.mark.asyncio
async def test_job_authority_validates_active_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _org_id, _repo_id, _snap_id, _principal_id, inv_id, lease, spec = await _seed_investigation_job(
        session_factory
    )
    authority = SqlInvestigationJobAuthority()

    async with session_factory() as session:
        auth = await authority.assert_current(session, lease, lock=False)
        assert auth.is_cancelled is False
        assert auth.is_expired is False
        assert auth.scope.investigation_id == inv_id
        assert auth.scope.spec_hash == spec.compute_spec_hash()


@pytest.mark.asyncio
async def test_job_authority_rejects_fencing_mismatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    *_, lease, _ = await _seed_investigation_job(session_factory)
    stale_lease = lease.model_copy(update={"fence": lease.fence + 1})
    authority = SqlInvestigationJobAuthority()

    async with session_factory() as session:
        with pytest.raises(StaleWorkerFencingError, match="fencing token mismatch"):
            await authority.assert_current(session, stale_lease, lock=False)


@pytest.mark.asyncio
async def test_job_authority_detects_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    *_, lease, _ = await _seed_investigation_job(session_factory, job_state="cancelling")
    authority = SqlInvestigationJobAuthority()

    async with session_factory() as session:
        auth = await authority.assert_current(session, lease, lock=False)
        assert auth.is_cancelled is True


@pytest.mark.asyncio
async def test_checkpoint_repo_append_and_replay_sqlite(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _org_id, _repo_id, _snap_id, _principal_id, inv_id, lease, spec = await _seed_investigation_job(
        session_factory
    )
    repo = SqlInvestigationExecutionCheckpointRepository()

    state = InvestigationExecutionStateV1(
        investigation_id=inv_id,
        scope_hash=hashlib.sha256(b"scope").hexdigest(),
        spec_hash=spec.compute_spec_hash(),
        execution_revision=1,
        next_node_kind=InvestigationNodeKind.INVESTIGATE,
        next_role="investigator",
        next_ordinal=1,
        checkpoint_sequence=1,
    )

    async with session_factory() as session, session.begin():
        cp1 = await repo.append(session, lease, expected_previous_sequence=0, state=state)
        assert cp1.checkpoint_sequence == 1

        # Idempotent replay of same state at sequence 1
        cp1_replay = await repo.append(session, lease, expected_previous_sequence=0, state=state)
        assert cp1_replay.checkpoint_sequence == 1
        assert cp1_replay.state_digest == cp1.state_digest

        # Conflicting state at sequence 1 should raise DuplicateConflict
        conflicting_state = state.model_copy(update={"next_ordinal": 99})
        with pytest.raises(DuplicateConflict):
            await repo.append(session, lease, expected_previous_sequence=0, state=conflicting_state)


@pytest.mark.asyncio
async def test_executor_sql_crash_and_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, repo_id, snap_id, principal_id, inv_id, lease, spec = await _seed_investigation_job(
        session_factory
    )

    scope = InvestigationScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
        investigation_id=inv_id,
        spec_hash=spec.compute_spec_hash(),
        execution_revision=1,
    )
    node_id = compute_node_id(
        org_id, inv_id, spec.compute_spec_hash(), InvestigationNodeKind.GATHER, "investigator", 0
    )
    ctx = InvestigationExecutionContextV1(
        scope=scope,
        logical_node_id=node_id,
        node_kind=InvestigationNodeKind.GATHER,
        role="investigator",
        logical_ordinal=0,
        attempt_job_id=lease.job_id,
        attempt_id=lease.attempt_id,
        attempt_fence=lease.fence,
        deadline=datetime.now(UTC) + timedelta(minutes=15),
        limits=spec.limits,
    )

    gateway1 = DummyModelGateway(
        [
            _make_response(
                content=json.dumps(
                    {"verdict": "benign", "confidence": 0.9, "summary": "Safe", "claims": []}
                ),
                charge_micros=1500,
            )
        ]
    )
    tools = DummyTools()
    authority = SqlInvestigationJobAuthority()
    checkpoint_repo = SqlInvestigationExecutionCheckpointRepository()
    receipt_repo = SqlInvestigationStepReceiptRepository()
    spec_repo = SqlInvestigationSpecRepository()

    from app.services.investigation_journal import InMemoryExecutionJournal

    journal = InMemoryExecutionJournal()
    executor1 = InvestigationExecutorService(
        gateway=gateway1,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=receipt_repo,
        session_factory=session_factory,
        spec_repo=spec_repo,
        journal=journal,
    )

    # 1. GATHER -> INVESTIGATE (checkpoint 1 written to SQLite)
    adv1 = await executor1.advance(ctx)
    assert adv1.status == InvestigationAdvanceStatus.PROGRESS
    assert adv1.checkpoint_sequence == 1

    # 2. INVESTIGATE -> VERIFY (checkpoint 2 written to SQLite)
    adv2 = await executor1.advance(ctx)
    assert adv2.status == InvestigationAdvanceStatus.PROGRESS
    assert adv2.checkpoint_sequence == 2

    assert gateway1.call_count == 1

    # --- SIMULATE CRASH & RESTART ---
    # Create brand new executor instance with fresh gateway
    gateway2 = DummyModelGateway()
    executor2 = InvestigationExecutorService(
        gateway=gateway2,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=receipt_repo,
        session_factory=session_factory,
        spec_repo=spec_repo,
        journal=journal,
    )

    # 3. Resume from checkpoint 2 -> advances to VERIFY -> finishes to READY_TO_PUBLISH
    adv3 = await executor2.advance(ctx)
    assert adv3.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
    assert adv3.checkpoint_sequence == 3

    # No new model calls on gateway2!
    assert gateway2.call_count == 0
