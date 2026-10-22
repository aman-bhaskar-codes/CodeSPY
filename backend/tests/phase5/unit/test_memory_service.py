from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryQueryRequestV1,
)
from app.domain.errors import ScopeViolation
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships
from app.services.memory_service import MemoryService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

grants = PHASE2_TABLES["repository_grants"]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[memberships, grants, repo_memories],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def seed_authorized_principal(
    session: AsyncSession,
    org_id: UUID,
    repo_id: UUID,
    principal_id: UUID,
) -> None:
    await session.execute(
        memberships.insert().values(
            org_id=org_id,
            principal_id=principal_id,
            role="member",
        )
    )
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_record_and_query_memory_success(session: AsyncSession) -> None:
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()

    await seed_authorized_principal(session, org_id, repo_id, principal_id)
    svc = MemoryService(session)

    finding_id = uuid4()
    chunk_id = uuid4()
    pointer = EvidencePointerV1(
        file_path="src/auth/service.py",
        start_line=12,
        end_line=24,
        finding_id=finding_id,
        chunk_id=chunk_id,
        symbol_name="authenticate_user",
        sha256="abc12345",
    )

    create_req = MemoryCreateRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
        memory_kind=MemoryKind.SECURITY_FINDING_EVIDENCE,
        topic="auth-bypass-audit",
        summary=(
            "Verified password hash comparison uses constant-time equals "
            "to avoid timing side-channels."
        ),
        evidence_pointers=(pointer,),
    )

    entry = await svc.record_memory(create_req)
    assert entry.memory_id is not None
    assert entry.topic == "auth-bypass-audit"
    assert len(entry.evidence_pointers) == 1
    assert entry.evidence_pointers[0].file_path == "src/auth/service.py"
    assert entry.evidence_pointers[0].finding_id == finding_id

    # Query by matching query token
    q_res = await svc.query_memory(
        MemoryQueryRequestV1(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            query="constant-time",
        )
    )
    assert q_res.total_candidates == 1
    assert q_res.items[0].memory_id == entry.memory_id
    assert q_res.items[0].evidence_pointers[0].symbol_name == "authenticate_user"

    # Query with non-matching query
    no_res = await svc.query_memory(
        MemoryQueryRequestV1(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            query="unrelated keyword",
        )
    )
    assert no_res.total_candidates == 0


@pytest.mark.asyncio
async def test_record_memory_unauthorized_scope(session: AsyncSession) -> None:
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()

    svc = MemoryService(session)

    req = MemoryCreateRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
        memory_kind=MemoryKind.TRIAGE_RESOLUTION,
        topic="unauthorized-entry",
        summary="Should be rejected",
    )

    # 1. No membership
    with pytest.raises(ScopeViolation, match="not a member"):
        await svc.record_memory(req)

    # Add membership but no repo grant
    await session.execute(
        memberships.insert().values(
            org_id=org_id,
            principal_id=principal_id,
            role="member",
        )
    )
    await session.commit()

    # 2. Member but no grant
    with pytest.raises(ScopeViolation, match="lacks access"):
        await svc.record_memory(req)


@pytest.mark.asyncio
async def test_query_memory_filtering_and_limits(session: AsyncSession) -> None:
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()

    await seed_authorized_principal(session, org_id, repo_id, principal_id)
    svc = MemoryService(session)

    # Insert 3 memories
    for kind, topic, summary in [
        (MemoryKind.TRIAGE_RESOLUTION, "db-migration", "Migration 0019 tested and deployed."),
        (MemoryKind.ARCHITECTURE_FACT, "cache-topology", "Redis cluster with read-replicas."),
        (
            MemoryKind.ARCHITECTURE_FACT,
            "cache-eviction",
            "LRU cache eviction policy for sessions.",
        ),
    ]:
        await svc.record_memory(
            MemoryCreateRequestV1(
                org_id=org_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                principal_id=principal_id,
                memory_kind=kind,
                topic=topic,
                summary=summary,
            )
        )

    # Filter by memory_kind
    arch_res = await svc.query_memory(
        MemoryQueryRequestV1(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            query="cache",
            memory_kind=MemoryKind.ARCHITECTURE_FACT,
        )
    )
    assert arch_res.total_candidates == 2

    # Filter by topic
    topic_res = await svc.query_memory(
        MemoryQueryRequestV1(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            query="cache",
            topic="cache-topology",
        )
    )
    assert topic_res.total_candidates == 1
    assert topic_res.items[0].topic == "cache-topology"

    # Enforce limit
    lim_res = await svc.query_memory(
        MemoryQueryRequestV1(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            query="cache",
            limit=1,
        )
    )
    assert lim_res.total_candidates == 1


@pytest.mark.asyncio
async def test_query_memory_unauthorized_scope(session: AsyncSession) -> None:
    org_id = uuid4()
    repo_id = uuid4()
    unauth_principal = uuid4()

    svc = MemoryService(session)

    with pytest.raises(ScopeViolation, match="not a member"):
        await svc.query_memory(
            MemoryQueryRequestV1(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=unauth_principal,
                query="anything",
            )
        )
