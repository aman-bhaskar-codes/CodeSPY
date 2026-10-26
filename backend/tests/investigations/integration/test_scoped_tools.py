"""Integration tests for real scoped bounded investigation tools.

Verifies:
- Real source file resolution and two-stage isolated source read
- Recording of immutable evidence receipts in investigation_evidence table
- Missing file returning UNAVAILABLE
- Missing membership or revoked grant returning DENIED
- Sensitive path patterns returning DENIED with sensitive_path_exclusion
- Safe payload truncation at 32 KiB with PARTIAL status
- Unindexed repository returning index_unavailable for search_code
- Memory retrieval from repo_memories table
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from app.contracts.investigation_execution import (
    MAX_SAFE_RETURNED_BYTES,
    InvestigationExecutionContextV1,
    InvestigationLimitsV1,
    InvestigationNodeKind,
    InvestigationScopeV1,
    InvestigationToolName,
    InvestigationToolRequestV1,
    InvestigationToolStatus,
)
from app.domain.scope import OrgRole
from app.models.investigation_execution_tables import (
    investigation_evidence,
    investigation_specs,
)
from app.models.investigation_tables import investigations
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import artifacts, memberships, repositories, source_files, source_snapshots
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderPort
from app.services.investigation_tools import InvestigationToolService
from sqlalchemy import CheckConstraint, event, insert, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import BinaryExpression, TextClause

# ---------------------------------------------------------------------------
# SQLite Compatibility Compilers
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@compiles(TextClause, "sqlite")
def _compile_text_sqlite(element: Any, compiler: Any, **kw: Any) -> Any:
    new_text = element.text.replace("::jsonb", "").replace("::text", "")
    if new_text != element.text:
        clone = element._clone()
        clone.text = new_text
        return compiler.visit_textclause(clone, **kw)
    return compiler.visit_textclause(element, **kw)


@compiles(CheckConstraint, "sqlite")
def _compile_check_constraint_sqlite(element: Any, compiler: Any, **kw: Any) -> Any:
    sqltext = str(element.sqltext)
    name = str(element.name or "")
    if (
        any(
            bad in sqltext
            for bad in ("~", "octet_length", "jsonb_", "num_nonnulls", "admit_source", "position(")
        )
        or "operation" in name
    ):
        return "CHECK (1=1)"
    return compiler.visit_check_constraint(element, **kw)


@compiles(BinaryExpression, "sqlite")
def _compile_binary_sqlite(element: Any, compiler: Any, **kw: Any) -> Any:
    val = getattr(element.right, "value", None)
    if isinstance(val, timedelta):
        secs = int(val.total_seconds())
        sign = "+" if secs >= 0 else ""
        return f"strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '{sign}{secs} seconds')"
    if "interval" in str(element.right):
        return "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+15 minutes')"
    return compiler.visit_binary(element, **kw)


# ---------------------------------------------------------------------------
# Test Fixtures & Harness
# ---------------------------------------------------------------------------


class MockSourceReader(SourceReaderPort):
    """In-memory source reader providing predetermined file contents."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}

    async def read(self, request: AdmittedSourceReadV1) -> bytes:
        if request.path in self.files:
            return self.files[request.path]
        raise ValueError(f"File not in mock reader: {request.path}")


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite session factory for scoped tools integration tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    configs: dict[str, str] = {}

    @event.listens_for(engine.sync_engine, "connect")
    def _register_sqlite_functions(dbapi_connection: Any, connection_record: Any) -> None:
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

    source_files.c.path.type.collation = None  # type: ignore[attr-defined]

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    memberships,
                    repositories,
                    artifacts,
                    PHASE2_TABLES["object_records"],
                    source_snapshots,
                    source_files,
                    PHASE2_TABLES["repository_grants"],
                    investigations,
                    investigation_specs,
                    investigation_evidence,
                    repo_memories,
                ],
            )
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def test_env(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    snapshot_id = uuid4()
    investigation_id = uuid4()
    now = datetime.now(UTC)

    # Sample source file content
    auth_py_content = (
        b"import os\n"
        b"import secrets\n"
        b"\n"
        b"def hash_password(password: str) -> str:\n"
        b"    salt = secrets.token_hex(16)\n"
        b"    return f'{salt}${password}'\n"
        b"\n"
        b"def verify_password(password: str, hashed: str) -> bool:\n"
        b"    salt, pw = hashed.split('$')\n"
        b"    return pw == password\n"
    )
    auth_py_sha = hashlib.sha256(auth_py_content).hexdigest()

    async with session_factory() as session, session.begin():
        await session.execute(
            insert(memberships).values(
                org_id=org_id,
                principal_id=principal_id,
                role=OrgRole.OWNER.value,
                created_at=now,
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
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
            )
        )
        artifact_id = uuid4()
        await session.execute(
            insert(artifacts).values(
                org_id=org_id,
                id=artifact_id,
                repository_id=repo_id,
                sha256="a" * 64,
                size_bytes=100,
                storage_key="test-key",
                created_at=now,
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["object_records"]).values(
                org_id=org_id,
                id=uuid4(),
                repository_id=repo_id,
                key="test-key",
                sha256="a" * 64,
                size_bytes=100,
                kind="source",
                state="available",
                created_at=now,
            )
        )
        await session.execute(
            insert(source_snapshots).values(
                org_id=org_id,
                repository_id=repo_id,
                id=snapshot_id,
                artifact_id=artifact_id,
                state="sealed",
                source_revision="a" * 40,
                admission_config_hash="b" * 64,
                identity_hash="c" * 64,
                manifest_hash="d" * 64,
                sealed_at=now,
                created_at=now,
            )
        )
        await session.execute(
            insert(source_files).values(
                org_id=org_id,
                snapshot_id=snapshot_id,
                id=uuid4(),
                path="src/auth.py",
                sha256=auth_py_sha,
                size_bytes=len(auth_py_content),
                language="python",
                created_at=now,
            )
        )
        await session.execute(
            insert(investigations).values(
                org_id=org_id,
                id=investigation_id,
                repository_id=repo_id,
                snapshot_id=snapshot_id,
                principal_id=principal_id,
                target_type="repository",
                query="Test Investigation",
                status="running",
                budget_limit_micros=5_000_000,
                created_at=now,
            )
        )

    reader = MockSourceReader(files={"src/auth.py": auth_py_content})

    ctx = InvestigationExecutionContextV1(
        scope=InvestigationScopeV1(
            org_id=org_id,
            repository_id=repo_id,
            snapshot_id=snapshot_id,
            principal_id=principal_id,
            investigation_id=investigation_id,
            spec_hash="0" * 64,
        ),
        logical_node_id=uuid4(),
        node_kind=InvestigationNodeKind.INVESTIGATE,
        role="investigator",
        logical_ordinal=1,
        attempt_job_id=uuid4(),
        attempt_id=uuid4(),
        attempt_fence=1,
        deadline=now + timedelta(minutes=15),
        limits=InvestigationLimitsV1(),
    )

    return {
        "org_id": org_id,
        "repo_id": repo_id,
        "principal_id": principal_id,
        "snapshot_id": snapshot_id,
        "investigation_id": investigation_id,
        "reader": reader,
        "context": ctx,
        "auth_py_content": auth_py_content,
        "auth_py_sha": auth_py_sha,
    }


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestScopedToolsIntegration:
    """Integration test suite for InvestigationToolService with real database."""

    @pytest.mark.asyncio
    async def test_read_file_range_success_records_evidence(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        service = InvestigationToolService(
            session_factory=session_factory,
            source_reader=test_env["reader"],
        )
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.READ_FILE_RANGE,
            arguments={
                "file_path": "src/auth.py",
                "start_line": 4,
                "end_line": 6,
            },
        )
        res = await service.execute(req, ctx)

        assert res.status == InvestigationToolStatus.OK
        assert len(res.evidence_refs) == 1
        assert "4: def hash_password" in res.payload
        assert "5:     salt = secrets.token_hex(16)" in res.payload
        assert "6:     return f'{salt}${password}'" in res.payload
        assert res.result_digest != ""

        # Verify evidence record in database
        evidence_id = res.evidence_refs[0]
        async with session_factory() as session:
            stmt = select(investigation_evidence).where(
                investigation_evidence.c.org_id == ctx.scope.org_id,
                investigation_evidence.c.id == evidence_id,
            )
            row = (await session.execute(stmt)).mappings().one_or_none()
            assert row is not None
            assert row["source_path"] == "src/auth.py"
            assert row["file_sha256"] == test_env["auth_py_sha"]
            assert row["original_start_line"] == 4
            assert row["original_end_line"] == 6
            assert row["safe_start_line"] == 4
            assert row["safe_end_line"] == 6
            assert row["observation_type"] == "source_read"

    @pytest.mark.asyncio
    async def test_read_file_range_missing_file_unavailable(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        service = InvestigationToolService(
            session_factory=session_factory,
            source_reader=test_env["reader"],
        )
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.READ_FILE_RANGE,
            arguments={
                "file_path": "src/nonexistent.py",
                "start_line": 1,
                "end_line": 10,
            },
        )
        res = await service.execute(req, ctx)

        assert res.status == InvestigationToolStatus.UNAVAILABLE
        assert "file_not_found_in_snapshot" in res.omission_reason
        assert res.payload == ""

    @pytest.mark.asyncio
    async def test_read_file_range_non_member_denied(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        service = InvestigationToolService(
            session_factory=session_factory,
            source_reader=test_env["reader"],
        )
        ctx = test_env["context"]
        # Use an unknown principal
        other_ctx = ctx.model_copy(
            update={"scope": ctx.scope.model_copy(update={"principal_id": uuid4()})}
        )

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.READ_FILE_RANGE,
            arguments={
                "file_path": "src/auth.py",
                "start_line": 1,
                "end_line": 5,
            },
        )
        res = await service.execute(req, other_ctx)

        assert res.status == InvestigationToolStatus.DENIED
        assert "membership_denied" in res.omission_reason

    @pytest.mark.asyncio
    async def test_read_file_range_revoked_grant_denied(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        # Revoke the repository grant
        async with session_factory() as session, session.begin():
            grants = PHASE2_TABLES["repository_grants"]
            await session.execute(
                grants.update()
                .where(
                    grants.c.org_id == test_env["org_id"],
                    grants.c.repository_id == test_env["repo_id"],
                    grants.c.principal_id == test_env["principal_id"],
                )
                .values(revoked_at=datetime.now(UTC))
            )

        service = InvestigationToolService(
            session_factory=session_factory,
            source_reader=test_env["reader"],
        )
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.READ_FILE_RANGE,
            arguments={
                "file_path": "src/auth.py",
                "start_line": 1,
                "end_line": 5,
            },
        )
        res = await service.execute(req, ctx)

        assert res.status == InvestigationToolStatus.DENIED
        assert "repository_grant_denied" in res.omission_reason

    @pytest.mark.asyncio
    async def test_read_file_range_sensitive_path_exclusion(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        service = InvestigationToolService(
            session_factory=session_factory,
            source_reader=test_env["reader"],
        )
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.READ_FILE_RANGE,
            arguments={
                "file_path": ".env.production",
                "start_line": 1,
                "end_line": 5,
            },
        )
        res = await service.execute(req, ctx)

        assert res.status == InvestigationToolStatus.DENIED
        assert "sensitive_path_exclusion" in res.omission_reason

    @pytest.mark.asyncio
    async def test_read_file_range_payload_bounding_to_32kib(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        # Create a file whose 100 lines exceed 32 KiB
        large_lines = [f"line_{i:04d}_" + ("X" * 400) for i in range(100)]
        large_content = "\n".join(large_lines).encode("utf-8")
        large_sha = hashlib.sha256(large_content).hexdigest()

        async with session_factory() as session, session.begin():
            await session.execute(
                insert(source_files).values(
                    org_id=test_env["org_id"],
                    snapshot_id=test_env["snapshot_id"],
                    id=uuid4(),
                    path="src/large_file.py",
                    sha256=large_sha,
                    size_bytes=len(large_content),
                    language="python",
                    created_at=datetime.now(UTC),
                )
            )

        reader = MockSourceReader(files={"src/large_file.py": large_content})
        service = InvestigationToolService(
            session_factory=session_factory,
            source_reader=reader,
        )
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.READ_FILE_RANGE,
            arguments={
                "file_path": "src/large_file.py",
                "start_line": 1,
                "end_line": 100,
            },
        )
        res = await service.execute(req, ctx)

        assert res.status == InvestigationToolStatus.PARTIAL
        assert len(res.payload.encode("utf-8")) <= MAX_SAFE_RETURNED_BYTES
        assert "truncated" in res.omission_reason.lower()
        assert len(res.evidence_refs) == 1

    @pytest.mark.asyncio
    async def test_search_code_index_unavailable(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        service = InvestigationToolService(session_factory=session_factory)
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.SEARCH_CODE,
            arguments={"query": "def hash_password"},
        )
        res = await service.execute(req, ctx)

        # Unindexed repository must explicitly return UNAVAILABLE, never fake empty success!
        assert res.status == InvestigationToolStatus.UNAVAILABLE
        assert "index_unavailable" in res.omission_reason

    @pytest.mark.asyncio
    async def test_check_memory_with_real_database_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        test_env: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC)
        async with session_factory() as session, session.begin():
            await session.execute(
                insert(repo_memories).values(
                    org_id=test_env["org_id"],
                    repository_id=test_env["repo_id"],
                    id=uuid4(),
                    snapshot_id=test_env["snapshot_id"],
                    memory_kind="architecture_fact",
                    topic="Authentication security",
                    summary="Passwords must use salted hashing and minimum 12 rounds",
                    evidence_pointers=[],
                    state="accepted",
                    reviewer_id=test_env["principal_id"],
                    accepted_at=now,
                    expires_at=now + timedelta(days=90),
                    created_at=now,
                )
            )

        from app.contracts.context_manifest import MemoryContextSettingsV1

        service = InvestigationToolService(
            session_factory=session_factory,
            memory_context=MemoryContextSettingsV1(mode="reviewed"),
        )
        ctx = test_env["context"]

        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.CHECK_MEMORY,
            arguments={"query": "Authentication security"},
        )
        res = await service.execute(req, ctx)

        assert res.status == InvestigationToolStatus.OK
        assert "Authentication security" in res.payload
        assert "salted hashing" in res.payload
