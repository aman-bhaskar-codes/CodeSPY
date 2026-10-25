"""SQLite API transport checks with simulated ranking; not publication qualification."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.chunking import ChunkKind
from app.contracts.memory import MemoryKind
from app.contracts.search import IndexState, IndexType
from app.domain.scope import OrgRole, TenantContext
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import (
    memberships,
    organizations,
    principals,
    repositories,
    source_files,
    source_snapshots,
)
from app.ports.object_store import ObjectStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

grants = PHASE2_TABLES["repository_grants"]
session_table = PHASE2_TABLES["sessions"]


@pytest_asyncio.fixture
async def factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy import CheckConstraint

    source_files.c.path.type.collation = None  # type: ignore[attr-defined]
    for table in [source_snapshots, source_files, organizations, principals, session_table]:
        for c in list(table.constraints):
            if isinstance(c, CheckConstraint):
                table.constraints.discard(c)

    import app.db.time
    import app.security.sessions

    async def _mock_now(_s: object) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr(app.security.sessions, "database_now", _mock_now)
    monkeypatch.setattr(app.db.time, "database_now", _mock_now)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    organizations,
                    principals,
                    memberships,
                    repositories,
                    grants,
                    session_table,
                    source_snapshots,
                    source_files,
                    source_chunks,
                    search_indices,
                    chunk_embeddings,
                    repo_memories,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_retrieval_endpoints_unauthenticated(tmp_path: Path) -> None:
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=async_sessionmaker(),
            store=cast(ObjectStore, LocalObjectStore(tmp_path / "obj")),
            fixture_context=ctx,
        ),
    )

    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Exact search without session
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/search/exact",
            json={"query": "test"},
        )
        assert res.status_code in (401, 403)

        # Hybrid search without session
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/search/hybrid",
            json={"query": "test"},
        )
        assert res.status_code in (401, 403)

        # Context assembly without session
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/context/assemble",
            json={"query": "test"},
        )
        assert res.status_code in (401, 403)

        # Memories GET without session
        res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/memories",
        )
        assert res.status_code in (401, 403)

        # Source view without session
        res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/files/{file_id}/source-view",
        )
        assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_retrieval_and_context_endpoints_authenticated(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.phase5.unit.legacy_ranking import install_legacy_ranking

    install_legacy_ranking(monkeypatch, include_context=True)
    from unittest.mock import AsyncMock

    # Transport-only fixture; real retained-source authority is tested in PostgreSQL.
    monkeypatch.setattr("app.services.retrieval_authority.locate_source", AsyncMock())
    from sqlalchemy import literal, select

    monkeypatch.setattr(
        "app.services.memory_service.MemoryService._snapshot_available",
        lambda self, org_id, repository_id, snapshot_id: select(literal(True)),
    )
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.OWNER)

    # Seed database
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        await session.execute(
            organizations.insert().values(
                id=org_id,
                slug="test-org",
                display_name="Test Org",
            )
        )
        await session.execute(
            principals.insert().values(
                id=principal_id,
                subject_key=f"user-{principal_id}",
                display_name="Test User",
            )
        )
        await session.execute(
            memberships.insert().values(
                org_id=org_id,
                principal_id=principal_id,
                role="owner",
            )
        )
        await session.execute(
            repositories.insert().values(
                id=repo_id,
                org_id=org_id,
                repository_key="retrieval-test-repo",
                display_name="Retrieval Test Repo",
                source_kind="local_fixture",
            )
        )
        await session.execute(
            grants.insert().values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
                revoked_at=None,
            )
        )
        await session.execute(
            source_snapshots.insert().values(
                id=snap_id,
                org_id=org_id,
                repository_id=repo_id,
                artifact_id=uuid4(),
                admission_config_hash="a" * 64,
                identity_hash="b" * 64,
                state="sealed",
            )
        )
        await session.execute(
            source_files.insert().values(
                id=file_id,
                org_id=org_id,
                snapshot_id=snap_id,
                path="src/auth.py",
                sha256="abc12345" * 8,
                size_bytes=100,
                language="python",
            )
        )
        # Seed code chunk
        chunk_id = uuid4()
        await session.execute(
            source_chunks.insert().values(
                id=chunk_id,
                org_id=org_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                file_id=file_id,
                file_path="src/auth.py",
                chunk_index=0,
                chunk_kind=str(ChunkKind.FUNCTION),
                symbol_name="login_user",
                qualified_name="auth.login_user",
                cpg_node_id=None,
                start_byte=0,
                end_byte=50,
                start_line=1,
                end_line=10,
                content_sha256="chunksha" * 8,
                byte_count=50,
                estimated_tokens=15,
                safe_content="def login_user(username, password): return True",
                redaction_digest="digest" * 10 + "abcd",
                chunker_version="tree_sitter_v1",
                tsv=None,
                created_at=now,
            )
        )
        # Seed search index
        idx_id = uuid4()
        await session.execute(
            search_indices.insert().values(
                id=idx_id,
                org_id=org_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                index_type=str(IndexType.DENSE),
                state=str(IndexState.READY),
                embedding_provider="aws_bedrock_simulated",
                embedding_model="amazon.titan-embed-text-v2:0",
                dimension=1024,
                total_chunks=1,
                indexed_chunks=1,
                failed_chunks=0,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
        )
        # Seed chunk embedding
        await session.execute(
            chunk_embeddings.insert().values(
                org_id=org_id,
                chunk_id=chunk_id,
                index_id=idx_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                embedding_json=[0.1] * 1024,
                token_count=15,
                created_at=now,
            )
        )

    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=factory,
            store=cast(ObjectStore, LocalObjectStore(tmp_path / "obj")),
            fixture_context=ctx,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Establish session
        sess_res = await client.get("/api/v1/session")
        assert sess_res.status_code == 200, f"Status: {sess_res.status_code}, Body: {sess_res.text}"
        csrf_token = sess_res.json()["csrf_token"]
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": "test-key-1",
        }

        # 2. Exact Search
        exact_res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/search/exact",
            json={"query": "login_user", "limit": 10},
            headers=headers,
        )
        assert exact_res.status_code == 200
        exact_data = exact_res.json()
        assert exact_data["total_candidates"] >= 1
        assert exact_data["hits"][0]["symbol_name"] == "login_user"

        # 3. Lexical Search
        lex_res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/search/lexical",
            json={"query": "login_user password", "limit": 10},
            headers=headers,
        )
        assert lex_res.status_code == 200
        lex_data = lex_res.json()
        assert lex_data["total_candidates"] >= 1

        # 4. Dense Search
        dense_res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/search/dense",
            json={"query": "authenticate user password", "index_id": str(idx_id), "limit": 10},
            headers=headers,
        )
        assert dense_res.status_code == 200
        dense_data = dense_res.json()
        assert dense_data["total_candidates"] == 0
        assert dense_data["availability"] == "embedding_unconfigured"

        # 5. Hybrid Search
        hybrid_res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/search/hybrid",
            json={"query": "login_user", "limit": 10},
            headers=headers,
        )
        assert hybrid_res.status_code == 200
        hybrid_data = hybrid_res.json()
        assert hybrid_data["total_candidates"] >= 1
        assert hybrid_data["hits"][0]["symbol_name"] == "login_user"

        # 6. Context Assembly
        ctx_res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/context/assemble",
            json={"query": "login_user", "budget": {"max_tokens": 1024}},
            headers=headers,
        )
        assert ctx_res.status_code == 200
        ctx_data = ctx_res.json()
        assert ctx_data["items_count"] >= 1
        assert ctx_data["items"][0]["citation_key"] == "[src/auth.py#L1-L10]"
        assert "login_user" in ctx_data["items"][0]["safe_content"]

        # 7. Record Grounded Memory
        mem_create_res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/memories",
            json={
                "memory_kind": str(MemoryKind.TRIAGE_RESOLUTION),
                "topic": "auth-login",
                "summary": "Verified user login is secure against timing attacks.",
                "evidence_pointers": [
                    {
                        "file_path": "src/auth.py",
                        "start_line": 1,
                        "end_line": 10,
                        "symbol_name": "login_user",
                    }
                ],
            },
            headers=headers,
        )
        assert mem_create_res.status_code == 200
        mem_data = mem_create_res.json()
        assert mem_data["topic"] == "auth-login"
        assert len(mem_data["evidence_pointers"]) == 1

        # 8. Query Memory
        mem_query_res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/memories?query=timing",
            headers={"Origin": "http://testserver"},
        )
        assert mem_query_res.status_code == 200
        query_data = mem_query_res.json()
        assert query_data["total_candidates"] >= 1
        assert query_data["items"][0]["topic"] == "auth-login"
