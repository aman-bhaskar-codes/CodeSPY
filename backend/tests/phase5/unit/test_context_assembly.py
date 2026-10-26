import hashlib
from collections.abc import AsyncIterator
from uuid import UUID, uuid4, uuid5

import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.context import (
    ContextAssemblyRequestV1,
    ContextBudgetV1,
)
from app.contracts.indexing import IndexCoverageV1
from app.contracts.search import (
    HybridSearchHitV1,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships, repositories, source_snapshots
from app.services.context_assembly import ContextAssemblyService
from app.services.lexical_search import LexicalSearchService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture(autouse=True)
def retained_source_stub(monkeypatch):
    # Unit fixtures model snapshot authority, not archive admission records.
    async def retained(*args):
        return None

    monkeypatch.setattr("app.services.retrieval_authority.locate_source", retained)
    # Isolate packaging from publication; real qualification is tested in PostgreSQL.
    from types import SimpleNamespace

    async def resolve(session, scope, *, chunk_set_id=None):
        import app.services.retrieval_authority as authority
        from app.domain.scope import OrgRole, TenantContext
        from app.security.authorization import require_snapshot
        from sqlalchemy.exc import NoResultFound

        await require_snapshot(
            session,
            TenantContext(
                org_id=scope.org_id, principal_id=scope.principal_id, role=OrgRole.MEMBER
            ),
            scope.repository_id,
            scope.snapshot_id,
        )
        try:
            await authority.locate_source(
                session, scope.org_id, scope.repository_id, scope.snapshot_id
            )
        except NoResultFound as exc:
            raise ScopeViolation("retrieval source unavailable") from exc
        expected = uuid5(scope.snapshot_id, "unit-qualified-set")
        if chunk_set_id is not None and chunk_set_id != expected:
            raise ScopeViolation("Unavailable set")
        return SimpleNamespace(
            chunk_set_id=expected,
            config_hash="b" * 64,
            materialization_hash="c" * 64,
            coverage=IndexCoverageV1(admitted_files=0, selected_files=0, processed_files=0),
        )

    monkeypatch.setattr("app.services.context_assembly.resolve_lexical_set", resolve)
    monkeypatch.setattr(
        "app.services.context_assembly.selected_chunks",
        lambda session, scope, resolved: source_chunks.c.chunk_set_id == resolved.chunk_set_id,
    )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    grants = PHASE2_TABLES["repository_grants"]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[memberships, grants, source_chunks],
            )
        )
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "CREATE TABLE repositories (org_id UUID, id UUID, repository_key TEXT, "
                "display_name TEXT, source_kind TEXT, created_at DATETIME)"
            )
        )
        await conn.execute(
            sa.text(
                "CREATE TABLE source_snapshots "
                "(org_id UUID, id UUID, repository_id UUID, state TEXT)"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def make_chunk_and_hit(
    org_id: UUID,
    repo_id: UUID,
    snap_id: UUID,
    path: str,
    sym: str,
    content: str,
    score: float,
    start_line: int = 1,
    end_line: int = 10,
) -> tuple[SourceChunkV1, HybridSearchHitV1]:
    cid = uuid4()
    fid = uuid4()
    raw = content.encode("utf-8")
    tokens = max(1, len(content) // 4)
    chunk = SourceChunkV1(
        chunk_id=cid,
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=fid,
        file_path=path,
        chunk_index=0,
        chunk_kind=ChunkKind.FUNCTION,
        symbol_name=sym,
        qualified_name=f"pkg.{sym}",
        cpg_node_id=uuid4(),
        start_byte=0,
        end_byte=len(raw),
        start_line=start_line,
        end_line=end_line,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        token_count=tokens,
        raw_content=raw,
        safe_content=content,
        redaction_digest="none",
        chunker_version="1.0.0",
    )
    hit = HybridSearchHitV1(
        chunk_id=cid,
        file_id=fid,
        file_path=path,
        chunk_kind=ChunkKind.FUNCTION,
        symbol_name=sym,
        qualified_name=f"pkg.{sym}",
        start_byte=0,
        end_byte=len(raw),
        start_line=start_line,
        end_line=end_line,
        score=score,
        snippet=content,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        chunk_set_id=uuid5(snap_id, "unit-qualified-set"),
        materialization_hash="c" * 64,
        safe_content_sha256=hashlib.sha256(raw).hexdigest(),
        match_modalities=("exact", "dense"),
        modality_ranks={"exact": 1, "dense": 2},
    )
    return chunk, hit


@pytest.mark.asyncio
async def test_context_assembly_knapsack_budget(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )

    await seed_snapshot(session, org_id, repo_id, snap_id)
    lex_svc = LexicalSearchService(session)
    c1, h1 = make_chunk_and_hit(
        org_id, repo_id, snap_id, "src/a.py", "func_a", "x = 1\n" * 30, 0.95
    )  # 45 tokens
    c2, h2 = make_chunk_and_hit(
        org_id, repo_id, snap_id, "src/b.py", "func_b", "y = 2\n" * 30, 0.85
    )  # 45 tokens
    c3, h3 = make_chunk_and_hit(
        org_id, repo_id, snap_id, "src/c.py", "func_c", "z = 3\n" * 30, 0.75
    )  # 45 tokens

    await seed_context_chunks(lex_svc, [c1, c2, c3])

    svc = ContextAssemblyService(session)
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    from app.services.syntax_chunker import count_tokens

    available = 2 * count_tokens(c1.safe_content)
    budget = ContextBudgetV1(
        max_tokens=available + 80, reserved_overhead_tokens=80, max_bytes=10000
    )
    packet = await svc.assemble_context(
        ContextAssemblyRequestV1(
            scope=scope,
            query="test query",
            candidate_hits=(h1, h2, h3),
            budget=budget,
            system_preamble="You are CodeSPY.",
        )
    )

    assert packet.items_count == 2
    assert packet.omitted_count == 1
    assert packet.total_tokens == available
    assert packet.items[0].chunk_id == c1.chunk_id
    assert packet.items[1].chunk_id == c2.chunk_id
    assert packet.items[0].citation_key == "[src/a.py#L1-L10]"
    assert len(packet.packet_sha256) == 64


@pytest.mark.asyncio
async def test_context_assembly_file_diversity_limit(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )

    await seed_snapshot(session, org_id, repo_id, snap_id)
    lex_svc = LexicalSearchService(session)
    chunks = []
    hits = []
    # 5 hits from same file
    for i in range(5):
        c, h = make_chunk_and_hit(
            org_id,
            repo_id,
            snap_id,
            "src/monolith.py",
            f"method_{i}",
            f"def method_{i}(): pass",
            1.0 - (i * 0.05),
        )
        chunks.append(c)
        hits.append(h)

    await seed_context_chunks(lex_svc, chunks)

    svc = ContextAssemblyService(session)
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    packet = await svc.assemble_context(
        ContextAssemblyRequestV1(
            scope=scope,
            query="find methods",
            candidate_hits=tuple(hits),
            deduplicate_files=True,
        )
    )

    # Diversity limit caps max chunks from same file to 3
    assert packet.items_count == 3
    assert packet.omitted_count == 2


@pytest.mark.asyncio
async def test_context_assembly_unauthorized_scope(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    svc = ContextAssemblyService(session)
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    with pytest.raises(ScopeViolation):
        await svc.assemble_context(
            ContextAssemblyRequestV1(
                scope=scope,
                query="unauthorized",
                candidate_hits=(),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["repository", "snapshot", "file_path", "file_id", "start_line", "content_sha256", "duplicate"],
)
async def test_context_rejects_untrusted_chunk_identity(session: AsyncSession, change: str) -> None:
    org_id, principal_id, repo_id, snap_id = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org_id, repository_id=repo_id, principal_id=principal_id, permission="read")
    )
    await seed_snapshot(session, org_id, repo_id, snap_id)
    chunk, hit = make_chunk_and_hit(
        org_id,
        uuid4() if change == "repository" else repo_id,
        uuid4() if change == "snapshot" else snap_id,
        "src/private.py",
        "private_function",
        "def private_function(): pass",
        1.0,
    )
    await seed_context_chunks(LexicalSearchService(session), [chunk])
    overrides: dict[str, object] = {
        "file_path": "src/forged.py",
        "file_id": uuid4(),
        "start_line": 2,
        "content_sha256": "forged",
    }
    if change in overrides:
        hit = hit.model_copy(update={change: overrides[change]})
    hits = (hit, hit) if change == "duplicate" else (hit,)
    with pytest.raises(ScopeViolation):
        await ContextAssemblyService(session).assemble_context(
            ContextAssemblyRequestV1(
                scope=SearchScopeV1(
                    org_id=org_id,
                    repository_id=repo_id,
                    snapshot_id=snap_id,
                    principal_id=principal_id,
                ),
                query="private_function",
                candidate_hits=hits,
            )
        )


async def seed_context_chunks(service, chunks):
    await service.index_chunks(chunks)
    for chunk in chunks:
        await service.s.execute(
            sa.update(source_chunks)
            .where(source_chunks.c.id == chunk.chunk_id)
            .values(
                chunk_set_id=uuid5(chunk.snapshot_id, "unit-qualified-set"),
                safe_content_sha256=hashlib.sha256(chunk.safe_content.encode()).hexdigest(),
            )
        )


async def seed_snapshot(session, org_id, repo_id, snap_id, state="sealed"):
    await session.execute(
        sa.insert(repositories).values(org_id=org_id, id=repo_id, source_kind="archive")
    )
    await session.execute(
        sa.insert(source_snapshots).values(
            org_id=org_id, id=snap_id, repository_id=repo_id, state=state
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["building", "missing", "wrong_repository"])
async def test_context_requires_sealed_repository_snapshot(session, state):
    org, principal, repo, snap = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org, principal_id=principal, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org, repository_id=repo, principal_id=principal, permission="read")
    )
    await seed_snapshot(session, org, repo, snap, "building" if state == "building" else "sealed")
    if state == "missing":
        await session.execute(sa.delete(source_snapshots))
    if state == "wrong_repository":
        await session.execute(sa.update(source_snapshots).values(repository_id=uuid4()))
    with pytest.raises(ScopeViolation):
        await ContextAssemblyService(session).assemble_context(
            ContextAssemblyRequestV1(
                scope=SearchScopeV1(
                    org_id=org, repository_id=repo, snapshot_id=snap, principal_id=principal
                ),
                query="test",
            )
        )


@pytest.mark.asyncio
async def test_context_counts_model_safe_bytes_and_tokens_not_stale_estimates(session):
    from app.services.syntax_chunker import count_tokens

    org, principal, repo, snap = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org, principal_id=principal, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org, repository_id=repo, principal_id=principal, permission="read")
    )
    await seed_snapshot(session, org, repo, snap)
    chunks, hits = zip(
        *(make_chunk_and_hit(org, repo, snap, f"{i}.py", "f", "secret", 1.0) for i in range(3)),
        strict=True,
    )
    await seed_context_chunks(LexicalSearchService(session), list(chunks))
    contents = ("é" * 200, "x" * 600, "small")
    for chunk, content in zip(chunks, contents, strict=True):
        await session.execute(
            sa.update(source_chunks)
            .where(source_chunks.c.id == chunk.chunk_id)
            .values(
                safe_content=content,
                safe_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                byte_count=1,
                estimated_tokens=0,
            )
        )
    hits = tuple(
        hit.model_copy(update={"safe_content_sha256": hashlib.sha256(content.encode()).hexdigest()})
        for hit, content in zip(hits, contents, strict=True)
    )
    packet = await ContextAssemblyService(session).assemble_context(
        ContextAssemblyRequestV1(
            scope=SearchScopeV1(
                org_id=org, repository_id=repo, snapshot_id=snap, principal_id=principal
            ),
            query="test",
            candidate_hits=hits,
            budget=ContextBudgetV1(max_tokens=1024, max_bytes=512, reserved_overhead_tokens=0),
        )
    )
    assert packet.items_count == 2
    assert packet.omitted_count == 1
    assert packet.total_bytes == len((contents[0] + contents[2]).encode())
    assert packet.total_tokens == sum(count_tokens(x) for x in (contents[0], contents[2]))
    assert packet.items[0].safe_content == contents[0]


def test_context_rejects_more_candidates_than_search_limit():
    from pydantic import ValidationError

    _, hit = make_chunk_and_hit(uuid4(), uuid4(), uuid4(), "a.py", "f", "pass", 1.0)
    with pytest.raises(ValidationError, match="at most 100"):
        ContextAssemblyRequestV1(query="test", candidate_hits=(hit,) * 101)
    assert (
        len(ContextAssemblyRequestV1(query="test", candidate_hits=(hit,) * 100).candidate_hits)
        == 100
    )


@pytest.mark.asyncio
async def test_context_omits_token_overflow_and_ignores_untrusted_snippet(session):
    from app.services.syntax_chunker import count_tokens

    org, principal, repo, snap = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org, principal_id=principal, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org, repository_id=repo, principal_id=principal, permission="read")
    )
    await seed_snapshot(session, org, repo, snap)
    first, first_hit = make_chunk_and_hit(org, repo, snap, "a.py", "a", "x = 1\n" * 100, 1.0)
    second, second_hit = make_chunk_and_hit(org, repo, snap, "b.py", "b", "pass", 0.5)
    await seed_context_chunks(LexicalSearchService(session), [first, second])
    scope = SearchScopeV1(org_id=org, repository_id=repo, snapshot_id=snap, principal_id=principal)
    request = ContextAssemblyRequestV1(
        scope=scope,
        query="test",
        candidate_hits=(first_hit, second_hit.model_copy(update={"snippet": "forged"})),
        budget=ContextBudgetV1(max_tokens=128, reserved_overhead_tokens=120),
    )
    packet = await ContextAssemblyService(session).assemble_context(request)
    assert packet.items_count == packet.omitted_count == 1
    assert packet.items[0].safe_content == "pass"
    assert packet.total_bytes == 4
    assert packet.total_tokens == count_tokens("pass")
    empty = await ContextAssemblyService(session).assemble_context(
        request.model_copy(
            update={"budget": ContextBudgetV1(max_tokens=128, reserved_overhead_tokens=129)}
        )
    )
    assert empty.items_count == empty.total_bytes == empty.total_tokens == 0
    assert empty.omitted_count == 2


@pytest.mark.asyncio
async def test_context_rejects_missing_retained_source(session, monkeypatch):
    from sqlalchemy.exc import NoResultFound

    org, principal, repo, snap = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org, principal_id=principal, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org, repository_id=repo, principal_id=principal, permission="read")
    )
    await seed_snapshot(session, org, repo, snap)

    async def missing(*args):
        raise NoResultFound()

    monkeypatch.setattr("app.services.retrieval_authority.locate_source", missing)
    with pytest.raises(ScopeViolation, match="retrieval source unavailable"):
        await ContextAssemblyService(session).assemble_context(
            ContextAssemblyRequestV1(
                scope=SearchScopeV1(
                    org_id=org, repository_id=repo, snapshot_id=snap, principal_id=principal
                ),
                query="test",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["legacy", "mixed", "materialization", "safe_digest", "stored_digest"]
)
async def test_context_requires_one_qualified_set_and_verified_content(session, change):
    org, principal, repo, snap = (uuid4() for _ in range(4))
    await session.execute(
        memberships.insert().values(org_id=org, principal_id=principal, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org, repository_id=repo, principal_id=principal, permission="read")
    )
    await seed_snapshot(session, org, repo, snap)
    chunk, hit = make_chunk_and_hit(org, repo, snap, "a.py", "f", "safe", 1.0)
    await seed_context_chunks(LexicalSearchService(session), [chunk])
    changes = {
        "legacy": {"chunk_set_id": None},
        "mixed": {"chunk_set_id": uuid4()},
        "materialization": {"materialization_hash": "d" * 64},
        "safe_digest": {"safe_content_sha256": "d" * 64},
    }
    if change == "stored_digest":
        await session.execute(sa.update(source_chunks).values(safe_content="tampered"))
    else:
        hit = hit.model_copy(update=changes[change])
    with pytest.raises(ScopeViolation):
        await ContextAssemblyService(session).assemble_context(
            ContextAssemblyRequestV1(
                scope=SearchScopeV1(
                    org_id=org, repository_id=repo, snapshot_id=snap, principal_id=principal
                ),
                query="test",
                candidate_hits=(hit,),
            )
        )


@pytest.mark.asyncio
async def test_context_pins_candidate_set_across_final_revalidation(session, monkeypatch):
    org, principal, repo, snap = (uuid4() for _ in range(4))
    await session.execute(
        memberships.insert().values(org_id=org, principal_id=principal, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org, repository_id=repo, principal_id=principal, permission="read")
    )
    await seed_snapshot(session, org, repo, snap)
    chunk, hit = make_chunk_and_hit(org, repo, snap, "a.py", "f", "safe", 1.0)
    await seed_context_chunks(LexicalSearchService(session), [chunk])
    import app.services.context_assembly as module

    original = module.resolve_lexical_set
    observed = []

    async def tracked(session, scope, *, chunk_set_id=None):
        observed.append(chunk_set_id)
        return await original(session, scope, chunk_set_id=chunk_set_id)

    monkeypatch.setattr(module, "resolve_lexical_set", tracked)
    packet = await ContextAssemblyService(session).assemble_context(
        ContextAssemblyRequestV1(
            scope=SearchScopeV1(
                org_id=org, repository_id=repo, snapshot_id=snap, principal_id=principal
            ),
            query="test",
            candidate_hits=(hit,),
        )
    )
    assert observed == [hit.chunk_set_id, hit.chunk_set_id]
    assert packet.scope.chunk_set_id == packet.chunk_set_id == hit.chunk_set_id
    assert packet.items[0].safe_content_sha256 == hashlib.sha256(b"safe").hexdigest()


@pytest.mark.asyncio
async def test_empty_context_reports_missing_index_explicitly(session, monkeypatch):
    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.context_assembly.resolve_lexical_set", unavailable)
    packet = await ContextAssemblyService(session).assemble_context(
        ContextAssemblyRequestV1(
            scope=SearchScopeV1(
                org_id=uuid4(), repository_id=uuid4(), snapshot_id=uuid4(), principal_id=uuid4()
            ),
            query="test",
        )
    )
    assert packet.lexical_availability == "index_unavailable"
    assert packet.chunk_set_id is packet.coverage is None
    assert not packet.items


async def test_context_digest_binds_preamble_and_budget_not_random_packet(session, monkeypatch):
    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.context_assembly.resolve_lexical_set", unavailable)
    req = ContextAssemblyRequestV1(
        scope=SearchScopeV1(
            org_id=uuid4(), repository_id=uuid4(), snapshot_id=uuid4(), principal_id=uuid4()
        ),
        query="stable",
        system_preamble="preview",
    )
    svc = ContextAssemblyService(session)
    a = await svc.assemble_context(req)
    b = await svc.assemble_context(req)
    assert a.packet_id != b.packet_id
    assert a.packet_sha256 == b.packet_sha256
    changed = await svc.assemble_context(req.model_copy(update={"system_preamble": "changed"}))
    assert changed.packet_sha256 != a.packet_sha256
