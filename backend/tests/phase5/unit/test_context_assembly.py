from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.context import (
    ContextAssemblyRequestV1,
    ContextBudgetV1,
)
from app.contracts.search import (
    HybridSearchHitV1,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships
from app.services.context_assembly import ContextAssemblyService
from app.services.lexical_search import LexicalSearchService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


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
        content_sha256="abc",
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
        content_sha256="abc",
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

    await lex_svc.index_chunks([c1, c2, c3])

    svc = ContextAssemblyService(session)
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    # Budget allows ~120 tokens max (max_tokens=200, reserved=80 -> available=120)
    # So c1 (~50) and c2 (~50) fit (~100 tokens), but c3 would exceed 120 tokens!
    budget = ContextBudgetV1(max_tokens=200, reserved_overhead_tokens=80, max_bytes=10000)
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
    assert packet.total_tokens <= 120
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

    await lex_svc.index_chunks(chunks)

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
    chunk, hit = make_chunk_and_hit(
        org_id,
        uuid4() if change == "repository" else repo_id,
        uuid4() if change == "snapshot" else snap_id,
        "src/private.py",
        "private_function",
        "def private_function(): pass",
        1.0,
    )
    await LexicalSearchService(session).index_chunks([chunk])
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
