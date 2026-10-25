"""Legacy synthetic chunks fail closed; admitted-source authority remains current."""

import hashlib
import os
from uuid import uuid4

import pytest
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.context import ContextAssemblyRequestV1
from app.contracts.search import (
    ExactSearchRequestV1,
    HybridSearchRequestV1,
    LexicalSearchRequestV1,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES
from app.services.analysis_sources import locate_source
from app.services.context_assembly import ContextAssemblyService
from app.services.hybrid_search import HybridSearchService
from app.services.lexical_search import LexicalSearchService
from sqlalchemy import func, select, update
from tests.phase3.integration.test_structural_publication import admitted


async def test_admitted_retrieval_context_and_retained_source_authority(
    api_factory, broker_factory, migration_factory, tmp_path
):
    if os.environ.get("CODESPY_TEST_SCHEMA_TARGET", "0014_finding_triage") < "0017_search_chunks":
        pytest.skip("retrieval chunk storage requires schema >= 0017_search_chunks")
    ctx, snapshot, repo, _store = await admitted(migration_factory, tmp_path)
    async with migration_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    file = next(file for file in located.files if file.path == "a.py")
    # Admission really verified these bytes. Chunk boundaries/symbols are synthetic;
    # this test qualifies database retrieval integration, not parser output.
    raw = b"print(1)\n"
    assert file.sha256 == hashlib.sha256(raw).hexdigest()
    chunk = SourceChunkV1(
        chunk_id=uuid4(),
        org_id=ctx.org_id,
        repository_id=repo,
        snapshot_id=snapshot,
        file_id=file.file_id,
        file_path=file.path,
        chunk_index=0,
        chunk_kind=ChunkKind.FILE,
        symbol_name="print",
        qualified_name=None,
        start_byte=0,
        end_byte=len(raw),
        start_line=1,
        end_line=1,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        token_count=1,
        raw_content=raw,
        safe_content=raw.decode(),
        redaction_digest=hashlib.sha256(b"").hexdigest(),
        chunker_version="synthetic-integration-v1",
    )
    async with broker_factory() as session, session.begin():
        assert await LexicalSearchService(session).index_chunks([chunk]) == 1
    scope = SearchScopeV1(
        org_id=ctx.org_id, repository_id=repo, snapshot_id=snapshot, principal_id=ctx.principal_id
    )
    async with api_factory() as session, session.begin():
        lexical = LexicalSearchService(session)
        exact = await lexical.search_exact(ExactSearchRequestV1(scope=scope, query="print"))
        text = await lexical.search_lexical(LexicalSearchRequestV1(scope=scope, query="print"))
        hybrid = await HybridSearchService(session).search_hybrid(
            HybridSearchRequestV1(scope=scope, query="print")
        )
        assert exact.hits == text.hits == hybrid.hits == ()
        assert exact.lexical_availability == hybrid.lexical_availability == "index_unavailable"
        assert hybrid.coverage_status == "partial"
        packet = await ContextAssemblyService(session).assemble_context(
            ContextAssemblyRequestV1(scope=scope, query="print", candidate_hits=())
        )
        assert packet.items_count == 0 and packet.lexical_availability == "index_unavailable"

    # Reject mismatched scopes even if an authorized repository has indexed rows.
    for foreign in [
        scope.model_copy(update={"repository_id": uuid4()}),
        scope.model_copy(update={"snapshot_id": uuid4()}),
    ]:
        for channel in ("exact", "lexical", "hybrid", "context"):
            with pytest.raises(ScopeViolation):
                async with api_factory() as session, session.begin():
                    await search_channel(session, channel, foreign, hybrid.hits)

    # Tombstone available source objects, preserving immutable snapshot/index rows.
    objects = PHASE2_TABLES["object_records"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(objects).where(objects.c.org_id == ctx.org_id).values(state="deleted")
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(source_chunks)
                .where(source_chunks.c.org_id == ctx.org_id)
            )
            == 1
        )
    for channel in ("exact", "lexical", "hybrid", "context"):
        with pytest.raises(ScopeViolation):
            async with api_factory() as session, session.begin():
                await search_channel(session, channel, scope, hybrid.hits)


async def search_channel(session, channel, scope, hits):
    if channel == "exact":
        return await LexicalSearchService(session).search_exact(
            ExactSearchRequestV1(scope=scope, query="print")
        )
    if channel == "lexical":
        return await LexicalSearchService(session).search_lexical(
            LexicalSearchRequestV1(scope=scope, query="print")
        )
    if channel == "hybrid":
        return await HybridSearchService(session).search_hybrid(
            HybridSearchRequestV1(scope=scope, query="print")
        )
    return await ContextAssemblyService(session).assemble_context(
        ContextAssemblyRequestV1(scope=scope, query="print", candidate_hits=hits)
    )
