import hashlib
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryQueryRequestV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.scan_tables import finding_occurrences
from app.models.tables import artifacts, memberships, repositories, source_files, source_snapshots
from app.services.memory_service import MemoryService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

grants = PHASE2_TABLES["repository_grants"]


@pytest_asyncio.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    async def sqlite_database_now(s: AsyncSession):
        return await s.scalar(sa.select(sa.func.current_timestamp()))

    # SQLite CURRENT_TIMESTAMP returns naive UTC, matching its stored timestamps.
    # PostgreSQL clock_timestamp/locking behavior requires separate qualification.
    monkeypatch.setattr("app.security.authorization.database_now", sqlite_database_now)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[memberships, grants, repo_memories],
            )
        )
    # The unit database models storage columns only; PostgreSQL guards are not exercised.
    async with engine.begin() as conn:
        for table in [
            repositories,
            PHASE2_TABLES["github_user_grants"],
            PHASE2_TABLES["github_repository_bindings"],
            source_snapshots,
            artifacts,
            source_files,
            source_chunks,
            finding_occurrences,
            PHASE2_TABLES["object_records"],
        ]:
            columns = [
                sa.Column(
                    c.name,
                    sa.Text()
                    if isinstance(c.type, sa.Text)
                    else (sa.JSON() if isinstance(c.type, sa.JSON) else c.type),
                    nullable=True,
                )
                for c in table.columns
            ]
            copy = sa.Table(table.name, sa.MetaData(), *columns)
            await conn.run_sync(lambda sync_conn, copy=copy: copy.create(sync_conn))
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
    if (
        await session.scalar(
            sa.select(repositories.c.id).where(
                repositories.c.org_id == org_id, repositories.c.id == repo_id
            )
        )
        is None
    ):
        await session.execute(
            repositories.insert().values(
                org_id=org_id,
                id=repo_id,
                repository_key=str(repo_id),
                display_name="Synthetic archive",
                source_kind="archive",
            )
        )
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
            permission="write",
            revoked_at=None,
        )
    )
    await session.commit()


async def seed_snapshot(session, org_id, repo_id, snap_id, state="sealed"):
    artifact_id = uuid4()
    await session.execute(
        source_snapshots.insert().values(
            org_id=org_id, id=snap_id, repository_id=repo_id, artifact_id=artifact_id, state=state
        )
    )
    await session.execute(
        artifacts.insert().values(
            org_id=org_id,
            id=artifact_id,
            repository_id=repo_id,
            sha256="a" * 64,
            size_bytes=1,
            storage_key=str(artifact_id),
        )
    )
    await session.execute(
        PHASE2_TABLES["object_records"]
        .insert()
        .values(
            org_id=org_id,
            id=uuid4(),
            repository_id=repo_id,
            key=str(artifact_id),
            state="available",
            kind="source",
            sha256="a" * 64,
            size_bytes=1,
        )
    )


@pytest.mark.asyncio
async def test_record_and_query_memory_success(session: AsyncSession, monkeypatch) -> None:
    stub_qualified_reads(monkeypatch)
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()

    await seed_authorized_principal(session, org_id, repo_id, principal_id)
    await seed_snapshot(session, org_id, repo_id, snap_id)
    svc = MemoryService(session)

    finding_id = uuid4()
    chunk_id = uuid4()
    file_id = uuid4()
    await session.execute(
        source_files.insert().values(
            org_id=org_id,
            snapshot_id=snap_id,
            id=file_id,
            path="src/auth/service.py",
            sha256="a" * 64,
        )
    )
    await session.execute(
        finding_occurrences.insert().values(
            org_id=org_id, snapshot_id=snap_id, id=finding_id, file_id=file_id, run_id=uuid4()
        )
    )
    await session.execute(
        source_chunks.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
            id=chunk_id,
            file_id=file_id,
            file_path="src/auth/service.py",
            start_line=1,
            end_line=30,
        )
    )
    set_id = uuid4()
    await session.execute(
        source_chunks.update()
        .where(source_chunks.c.id == chunk_id)
        .values(
            chunk_set_id=set_id,
            safe_content="safe",
            safe_content_sha256=hashlib.sha256(b"safe").hexdigest(),
            content_sha256="a" * 64,
        )
    )
    pointer = EvidencePointerV1(
        chunk_set_id=set_id,
        materialization_hash="c" * 64,
        safe_content_sha256=hashlib.sha256(b"safe").hexdigest(),
        file_path="src/auth/service.py",
        start_line=12,
        end_line=24,
        finding_id=finding_id,
        chunk_id=chunk_id,
        symbol_name="authenticate_user",
        sha256="a" * 64,
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
    with pytest.raises(ScopeViolation):
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
    with pytest.raises(ScopeViolation):
        await svc.record_memory(req)


@pytest.mark.asyncio
async def test_query_memory_filtering_and_limits(session: AsyncSession) -> None:
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()

    await seed_authorized_principal(session, org_id, repo_id, principal_id)
    svc = MemoryService(session)

    await seed_snapshot(session, org_id, repo_id, snap_id)

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

    with pytest.raises(ScopeViolation):
        await svc.query_memory(
            MemoryQueryRequestV1(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=unauth_principal,
                query="anything",
            )
        )


@pytest.mark.asyncio
async def test_memory_authority_and_snapshot_filter(session: AsyncSession) -> None:
    org, repo, principal, snap = uuid4(), uuid4(), uuid4(), uuid4()
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    svc = MemoryService(session)
    request = MemoryCreateRequestV1(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        principal_id=principal,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="architecture",
        summary="retained fact",
    )
    await session.execute(grants.update().values(permission="read"))
    with pytest.raises(ScopeViolation):
        await svc.record_memory(request)
    await session.execute(grants.update().values(permission="write"))
    with pytest.raises(ScopeViolation):
        await svc.record_memory(request.model_copy(update={"snapshot_id": uuid4()}))
    foreign = uuid4()
    await seed_snapshot(session, org, uuid4(), foreign)
    with pytest.raises(ScopeViolation):
        await svc.record_memory(request.model_copy(update={"snapshot_id": foreign}))
    entry = await svc.record_memory(request)
    query = MemoryQueryRequestV1(
        org_id=org, repository_id=repo, principal_id=principal, query="retained", snapshot_id=snap
    )
    assert (await svc.query_memory(query)).items[0].memory_id == entry.memory_id
    with pytest.raises(ScopeViolation):
        await svc.query_memory(query.model_copy(update={"snapshot_id": foreign}))
    await session.execute(
        source_snapshots.update().where(source_snapshots.c.id == snap).values(state="building")
    )
    assert not (await svc.query_memory(query)).items
    with pytest.raises(ScopeViolation):
        await svc.record_memory(request)
    await session.execute(
        source_snapshots.update().where(source_snapshots.c.id == snap).values(state="sealed")
    )
    await session.execute(PHASE2_TABLES["object_records"].update().values(state="deleted"))
    assert not (await svc.query_memory(query)).items
    await session.execute(grants.update().values(revoked_at=sa.func.current_timestamp()))
    with pytest.raises(ScopeViolation):
        await svc.query_memory(query)


@pytest.mark.asyncio
async def test_memory_rejects_unscoped_evidence(session: AsyncSession) -> None:
    org, repo, principal, snap = uuid4(), uuid4(), uuid4(), uuid4()
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    request = MemoryCreateRequestV1(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        principal_id=principal,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="architecture",
        summary="fact",
        evidence_pointers=(EvidencePointerV1(file_path="missing.py", start_line=1, end_line=2),),
    )
    with pytest.raises(ScopeViolation):
        await MemoryService(session).record_memory(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["chunk", "finding", "range"])
async def test_memory_pointer_identity_is_scoped(session: AsyncSession, invalid: str) -> None:
    org, repo, principal, snap, file_id = (uuid4() for _ in range(5))
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    await session.execute(
        source_files.insert().values(
            org_id=org, snapshot_id=snap, id=file_id, path="a.py", sha256="a" * 64
        )
    )
    pointer = EvidencePointerV1(
        file_path="a.py",
        start_line=3,
        end_line=4,
        chunk_id=uuid4() if invalid == "chunk" else None,
        finding_id=uuid4() if invalid == "finding" else None,
    )
    if invalid == "range":
        pointer = pointer.model_copy(update={"end_line": 2})
    with pytest.raises(ScopeViolation):
        await MemoryService(session).record_memory(
            MemoryCreateRequestV1(
                org_id=org,
                repository_id=repo,
                principal_id=principal,
                snapshot_id=snap,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="test",
                summary="fact",
                evidence_pointers=(pointer,),
            )
        )


@pytest.mark.asyncio
async def test_memory_is_shared_with_current_repository_readers(session: AsyncSession) -> None:
    org, repo, author, reader, snap = (uuid4() for _ in range(5))
    await seed_authorized_principal(session, org, repo, author)
    await seed_authorized_principal(session, org, repo, reader)
    await session.execute(
        grants.update().where(grants.c.principal_id == reader).values(permission="read")
    )
    await seed_snapshot(session, org, repo, snap)
    svc = MemoryService(session)
    entry = await svc.record_memory(
        MemoryCreateRequestV1(
            org_id=org,
            repository_id=repo,
            principal_id=author,
            snapshot_id=snap,
            memory_kind=MemoryKind.ARCHITECTURE_FACT,
            topic="shared",
            summary="retained",
        )
    )
    result = await svc.query_memory(
        MemoryQueryRequestV1(org_id=org, repository_id=repo, principal_id=reader, query="retained")
    )
    assert result.items[0].memory_id == entry.memory_id


@pytest.mark.asyncio
async def test_memory_matches_before_limit_and_escapes_wildcards(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    org, repo, principal, snap = (uuid4() for _ in range(4))
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    svc = MemoryService(session)
    old = await svc.record_memory(
        MemoryCreateRequestV1(
            org_id=org,
            repository_id=repo,
            principal_id=principal,
            snapshot_id=snap,
            memory_kind=MemoryKind.ARCHITECTURE_FACT,
            topic="target",
            summary="100% literal_value",
        )
    )
    await session.execute(
        repo_memories.update()
        .where(repo_memories.c.id == old.memory_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=1))
    )
    for _ in range(3):
        await svc.record_memory(
            MemoryCreateRequestV1(
                org_id=org,
                repository_id=repo,
                principal_id=principal,
                snapshot_id=snap,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="unrelated",
                summary="nothing",
            )
        )
    query = MemoryQueryRequestV1(
        org_id=org, repository_id=repo, principal_id=principal, query="100%", limit=1
    )
    assert (await svc.query_memory(query)).items[0].memory_id == old.memory_id
    assert not (await svc.query_memory(query.model_copy(update={"query": "missing_%"}))).items


@pytest.mark.asyncio
async def test_memory_denies_viewer_writes_despite_write_grant(session: AsyncSession) -> None:
    org, repo, principal, snap = (uuid4() for _ in range(4))
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    await session.execute(memberships.update().values(role="viewer"))
    with pytest.raises(ScopeViolation):
        await MemoryService(session).record_memory(
            MemoryCreateRequestV1(
                org_id=org,
                repository_id=repo,
                principal_id=principal,
                snapshot_id=snap,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="test",
                summary="fact",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["revoked", "expired", "inactive"])
async def test_memory_denies_stale_github_authority(session: AsyncSession, invalid: str) -> None:
    from datetime import UTC, datetime, timedelta

    org, repo, principal, snap = (uuid4() for _ in range(4))
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    svc = MemoryService(session)
    request = MemoryCreateRequestV1(
        org_id=org,
        repository_id=repo,
        principal_id=principal,
        snapshot_id=snap,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="test",
        summary="fact",
    )
    await svc.record_memory(request)
    await session.execute(repositories.update().values(source_kind="github"))
    now = datetime.now(UTC)
    await session.execute(
        PHASE2_TABLES["github_user_grants"]
        .insert()
        .values(
            principal_id=principal,
            token_cipher=b"synthetic-not-a-token",
            key_id="test-only",
            expires_at=now + timedelta(hours=1),
            revoked_at=None,
            version=0,
            last_verified_at=now,
        )
    )
    await session.execute(
        PHASE2_TABLES["github_repository_bindings"]
        .insert()
        .values(
            org_id=org,
            repository_id=repo,
            github_repository_id=1,
            installation_id=1,
            account_id=1,
            state="active",
            version=0,
            last_checked_at=now,
        )
    )
    query = MemoryQueryRequestV1(
        org_id=org, repository_id=repo, principal_id=principal, query="fact"
    )
    assert len((await svc.query_memory(query)).items) == 1
    if invalid == "inactive":
        await session.execute(
            PHASE2_TABLES["github_repository_bindings"].update().values(state="suspended")
        )
    elif invalid == "revoked":
        await session.execute(PHASE2_TABLES["github_user_grants"].update().values(revoked_at=now))
    else:
        await session.execute(
            PHASE2_TABLES["github_user_grants"].update().values(expires_at=now - timedelta(hours=1))
        )
    with pytest.raises(ScopeViolation):
        await svc.record_memory(request)
    with pytest.raises(ScopeViolation):
        await svc.query_memory(
            MemoryQueryRequestV1(
                org_id=org, repository_id=repo, principal_id=principal, query="fact"
            )
        )


def stub_qualified_reads(monkeypatch):
    # Packaging unit seam only: PostgreSQL tests establish actual set qualification.
    from types import SimpleNamespace

    async def resolve(session, scope, *, chunk_set_id=None):
        return SimpleNamespace(
            chunk_set_id=chunk_set_id, materialization_hash="c" * 64, config_hash="b" * 64
        )

    monkeypatch.setattr("app.services.memory_service.resolve_lexical_set", resolve)
    monkeypatch.setattr(
        "app.services.memory_service.selected_chunks",
        lambda session, scope, resolved: source_chunks.c.chunk_set_id == resolved.chunk_set_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["legacy", "materialization", "safe_digest", "source_digest", "stored_digest", "unavailable"],
)
async def test_memory_revalidates_qualified_evidence_on_create_and_read(
    session, monkeypatch, invalid
):
    stub_qualified_reads(monkeypatch)
    org, repo, principal, snap, file_id, chunk_id, set_id = (uuid4() for _ in range(7))
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    await session.execute(
        source_files.insert().values(
            org_id=org, snapshot_id=snap, id=file_id, path="a.py", sha256="a" * 64
        )
    )
    await session.execute(
        source_chunks.insert().values(
            org_id=org,
            repository_id=repo,
            snapshot_id=snap,
            id=chunk_id,
            file_id=file_id,
            file_path="a.py",
            start_line=1,
            end_line=10,
            chunk_set_id=set_id,
            safe_content="safe",
            safe_content_sha256=hashlib.sha256(b"safe").hexdigest(),
            content_sha256="a" * 64,
        )
    )
    pointer = EvidencePointerV1(
        file_path="a.py",
        start_line=1,
        end_line=2,
        chunk_id=chunk_id,
        chunk_set_id=set_id,
        materialization_hash="c" * 64,
        safe_content_sha256=hashlib.sha256(b"safe").hexdigest(),
        sha256="a" * 64,
    )
    request = MemoryCreateRequestV1(
        org_id=org,
        repository_id=repo,
        snapshot_id=snap,
        principal_id=principal,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="fact",
        summary="historical evidence",
        evidence_pointers=(pointer,),
    )
    service = MemoryService(session)
    entry = await service.record_memory(request)
    query = MemoryQueryRequestV1(
        org_id=org, repository_id=repo, principal_id=principal, query="evidence"
    )
    assert (await service.query_memory(query)).items[0].memory_id == entry.memory_id
    changes = {
        "legacy": {"chunk_set_id": None},
        "materialization": {"materialization_hash": "d" * 64},
        "safe_digest": {"safe_content_sha256": "d" * 64},
        "source_digest": {"sha256": "d" * 64},
    }
    if invalid == "stored_digest":
        await session.execute(source_chunks.update().values(safe_content="tampered"))
    elif invalid == "unavailable":

        async def unavailable(*args, **kwargs):
            raise ScopeViolation("Unavailable historical set")

        monkeypatch.setattr("app.services.memory_service.resolve_lexical_set", unavailable)
    else:
        pointer = pointer.model_copy(update=changes[invalid])
        await session.execute(
            repo_memories.update()
            .where(repo_memories.c.id == entry.memory_id)
            .values(evidence_pointers=[pointer.model_dump(mode="json")])
        )
    with pytest.raises(ScopeViolation):
        await service.record_memory(request.model_copy(update={"evidence_pointers": (pointer,)}))
    assert not (await service.query_memory(query)).items


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["validation", "cancellation"])
async def test_memory_closes_stream_after_iteration_failure(session, monkeypatch, failure):
    import asyncio

    from pydantic import ValidationError

    org, repo, principal, snap = (uuid4() for _ in range(4))
    await seed_authorized_principal(session, org, repo, principal)
    await seed_snapshot(session, org, repo, snap)
    closed = []

    class FailingResult:
        def mappings(self):
            async def rows():
                if failure == "cancellation":
                    raise asyncio.CancelledError()
                yield {"evidence_pointers": [{"file_path": "a.py"}]}

            return rows()

        async def close(self):
            closed.append(True)

    async def stream(*args, **kwargs):
        return FailingResult()

    monkeypatch.setattr(session, "stream", stream)
    expected = asyncio.CancelledError if failure == "cancellation" else ValidationError
    with pytest.raises(expected):
        await MemoryService(session).query_memory(
            MemoryQueryRequestV1(
                org_id=org, repository_id=repo, principal_id=principal, query="evidence"
            )
        )
    assert closed == [True]
