"""Published-set retrieval authority using actual jobs, receipts and PostgreSQL guards."""

from uuid import uuid4

import pytest
from app.contracts.context import ContextAssemblyRequestV1, ContextBudgetV1
from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryQueryRequestV1,
)
from app.contracts.search import (
    ExactSearchRequestV1,
    HybridSearchRequestV1,
    LexicalSearchRequestV1,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES
from app.services.context_assembly import ContextAssemblyService
from app.services.hybrid_search import HybridSearchService
from app.services.index_publication import IndexFinalizer
from app.services.lexical_search import LexicalSearchService
from app.services.memory_service import MemoryService
from sqlalchemy import func, insert, select, text, update

from tests.retrieval.storage.test_index_publication import claimed


async def published(api, broker, migration, tmp_path):
    ctx, _created, lease, prepared = await claimed(api, broker, migration, tmp_path)
    receipt = await IndexFinalizer(broker).finish(lease, prepared)
    scope = SearchScopeV1(
        org_id=ctx.org_id,
        principal_id=ctx.principal_id,
        repository_id=prepared.spec.repository_id,
        snapshot_id=prepared.spec.snapshot_id,
    )
    return ctx, scope, receipt


async def test_published_set_drives_exact_fts_hybrid_context_and_memory(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, scope, receipt = await published(api_factory, broker_factory, migration_factory, tmp_path)
    async with api_factory() as s, s.begin():
        exact = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="a.py")
        )
        lexical = await LexicalSearchService(s).search_lexical(
            LexicalSearchRequestV1(scope=scope, query="print")
        )
        hybrid = await HybridSearchService(s).search_hybrid(
            HybridSearchRequestV1(scope=scope, query="print")
        )
        for result in (exact, lexical, hybrid):
            assert len(result.hits) == 1
            assert (
                result.chunk_set_id == receipt.chunk_set_id
                and result.lexical_availability == "available"
            )
            assert (
                result.hits[0].safe_content_sha256
                and result.hits[0].materialization_hash == receipt.materialization_hash
            )
        packet = await ContextAssemblyService(s).assemble_context(
            ContextAssemblyRequestV1(
                scope=hybrid.scope,
                query="print",
                candidate_hits=hybrid.hits,
                budget=ContextBudgetV1(max_tokens=128, max_bytes=512, reserved_overhead_tokens=0),
            )
        )
        assert packet.chunk_set_id == receipt.chunk_set_id and packet.items_count == 1
        assert packet.items[0].safe_content == "print(1)\n" and packet.items[0].start_byte == 0
        assert packet.total_tokens <= 128 and packet.total_bytes <= 512
        hit = hybrid.hits[0]
        pointer = EvidencePointerV1(
            file_path=hit.file_path,
            start_line=hit.start_line,
            end_line=hit.end_line,
            chunk_id=hit.chunk_id,
            sha256=hit.content_sha256,
            chunk_set_id=hit.chunk_set_id,
            config_hash=hit.config_hash,
            materialization_hash=hit.materialization_hash,
            safe_content_sha256=hit.safe_content_sha256,
        )
        memory = await MemoryService(s).record_memory(
            MemoryCreateRequestV1(
                org_id=ctx.org_id,
                principal_id=ctx.principal_id,
                repository_id=scope.repository_id,
                snapshot_id=scope.snapshot_id,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="synthetic",
                summary="Print entrypoint",
                evidence_pointers=(pointer,),
            )
        )
        queried = await MemoryService(s).query_memory(
            MemoryQueryRequestV1(
                org_id=ctx.org_id,
                principal_id=ctx.principal_id,
                repository_id=scope.repository_id,
                snapshot_id=scope.snapshot_id,
                query="Print",
            )
        )
        assert [item.memory_id for item in queried.items] == [memory.memory_id]


async def test_legacy_matching_canary_never_enters_qualified_search_or_context(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, scope, receipt = await published(api_factory, broker_factory, migration_factory, tmp_path)
    async with migration_factory() as s, s.begin():
        row = dict((await s.execute(select(source_chunks))).mappings().one())
        row.update(
            id=uuid4(),
            chunk_set_id=None,
            safe_content="legacy private canary",
            safe_content_sha256=None,
            safe_start_byte=None,
            safe_end_byte=None,
            provenance_json=None,
            tsv=None,
            chunker_version="legacy-test",
        )
        row.pop("provenance_json")
        await s.execute(insert(source_chunks).values(**row))
    async with api_factory() as s:
        result = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="a.py")
        )
        assert len(result.hits) == 1 and "canary" not in result.model_dump_json()
        assert result.chunk_set_id == receipt.chunk_set_id
        with pytest.raises(ScopeViolation):
            await ContextAssemblyService(s).assemble_context(
                ContextAssemblyRequestV1(
                    scope=scope,
                    query="forged",
                    candidate_hits=(result_to_hybrid(result.hits[0], chunk_set_id=None),),
                )
            )


def result_to_hybrid(hit, **changes):
    from app.contracts.search import HybridSearchHitV1

    return HybridSearchHitV1(
        **{
            **hit.model_dump(exclude={"match_reasons"}),
            "match_modalities": ("exact",),
            "modality_ranks": {"exact": 1},
            **changes,
        }
    )


async def test_explicit_foreign_or_missing_set_rejects_without_fallback(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, scope, _ = await published(api_factory, broker_factory, migration_factory, tmp_path)
    async with api_factory() as s:
        with pytest.raises(ScopeViolation):
            await LexicalSearchService(s).search_exact(
                ExactSearchRequestV1(
                    scope=scope.model_copy(update={"chunk_set_id": uuid4()}), query="a.py"
                )
            )
        from app.services.retrieval_selection import resolve_lexical_set

        with pytest.raises(ScopeViolation):
            await resolve_lexical_set(
                s, scope.model_copy(update={"chunk_set_id": uuid4()}), chunk_set_id=uuid4()
            )


async def test_source_expiry_relative_to_database_time_rejects_reads(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, scope, _ = await published(api_factory, broker_factory, migration_factory, tmp_path)
    objects = PHASE2_TABLES["object_records"]
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(objects)
            .where(objects.c.org_id == scope.org_id, objects.c.kind == "source")
            .values(expires_at=func.clock_timestamp() - text("interval '1 second'"))
        )
    async with api_factory() as s:
        with pytest.raises(ScopeViolation):
            await LexicalSearchService(s).search_lexical(
                LexicalSearchRequestV1(scope=scope, query="print")
            )


async def another_set(api, broker, ctx, scope, request_key="new-set"):
    from app.indexing.receipt import _issue
    from app.repositories.index_repository import IndexRepository
    from app.services.jobs import JobService

    from tests.retrieval.storage.test_index_jobs import service
    from tests.retrieval.unit.test_index_contracts import config, draft

    changed = config(runtime_source_sha256="f" * 64)
    created = await service(api).create(
        ctx, scope.repository_id, scope.snapshot_id, changed, request_key
    )
    lease = await JobService(broker).claim(ctx.org_id, created.job.id, 1, "replacement")
    async with api() as s:
        spec = await IndexRepository(s).get_spec(
            ctx, scope.repository_id, created.job.index_spec_id
        )
    return await IndexFinalizer(broker).finish(lease, _issue(spec, draft(spec)))


async def test_publication_between_channels_does_not_mix_materializations(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, scope, first = await published(api_factory, broker_factory, migration_factory, tmp_path)
    published_ids = []

    class PublishAfterExact(LexicalSearchService):
        async def search_exact(self, request):
            result = await super().search_exact(request)
            second = await another_set(api_factory, broker_factory, ctx, scope)
            published_ids.append(second.chunk_set_id)
            return result

    async with api_factory() as s:
        result = await HybridSearchService(s, lexical_service=PublishAfterExact(s)).search_hybrid(
            HybridSearchRequestV1(scope=scope, query="print")
        )
        assert result.scope.chunk_set_id == result.chunk_set_id == first.chunk_set_id
        assert all(hit.chunk_set_id == first.chunk_set_id for hit in result.hits)
        packet = await ContextAssemblyService(s).assemble_context(
            ContextAssemblyRequestV1(scope=result.scope, query="print", candidate_hits=result.hits)
        )
        assert packet.chunk_set_id == first.chunk_set_id
        fresh = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="a.py")
        )
        assert fresh.chunk_set_id == published_ids[0] != first.chunk_set_id


async def test_historical_memory_retains_original_ids_after_new_publication(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, scope, first = await published(api_factory, broker_factory, migration_factory, tmp_path)
    async with api_factory() as s, s.begin():
        result = await HybridSearchService(s).search_hybrid(
            HybridSearchRequestV1(scope=scope, query="print")
        )
        hit = result.hits[0]
        pointer = EvidencePointerV1(
            file_path=hit.file_path,
            start_line=hit.start_line,
            end_line=hit.end_line,
            chunk_id=hit.chunk_id,
            chunk_set_id=hit.chunk_set_id,
            config_hash=hit.config_hash,
            materialization_hash=hit.materialization_hash,
            safe_content_sha256=hit.safe_content_sha256,
        )
        original = await MemoryService(s).record_memory(
            MemoryCreateRequestV1(
                org_id=ctx.org_id,
                principal_id=ctx.principal_id,
                repository_id=scope.repository_id,
                snapshot_id=scope.snapshot_id,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="historical",
                summary="Historical print fact",
                evidence_pointers=(pointer,),
            )
        )
    second = await another_set(api_factory, broker_factory, ctx, scope)
    assert first.chunk_set_id != second.chunk_set_id
    query = MemoryQueryRequestV1(
        org_id=ctx.org_id,
        principal_id=ctx.principal_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        query="Historical",
    )
    async with api_factory() as s:
        response = await MemoryService(s).query_memory(query)
        assert response.items[0].memory_id == original.memory_id
        assert response.items[0].evidence_pointers[0].chunk_set_id == first.chunk_set_id
    async with migration_factory() as s, s.begin():
        objects = PHASE2_TABLES["object_records"]
        await s.execute(
            update(objects).where(objects.c.org_id == scope.org_id).values(state="deleted")
        )
    async with api_factory() as s:
        assert (await MemoryService(s).query_memory(query)).items == ()
        from app.models.memory_tables import repo_memories

        assert await s.scalar(select(repo_memories.c.id)) == original.memory_id


async def test_qualified_empty_set_differs_from_absent_index(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    import io
    import zipfile

    from app.contracts.indexing import IndexCoverageV1, IndexPublicationDraftV1
    from app.indexing.receipt import _issue
    from app.repositories.index_repository import IndexRepository
    from app.services.jobs import JobService

    from tests.phase2.integration import test_submission_ingestion as ingestion
    from tests.phase3.integration.test_structural_publication import admitted
    from tests.retrieval.storage.test_index_jobs import service
    from tests.retrieval.unit.test_index_contracts import config

    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("a.py", b"")
    monkeypatch.setattr(ingestion, "zip_bytes", body.getvalue)
    ctx, snapshot, repo, _ = await admitted(migration_factory, tmp_path)
    created = await service(api_factory).create(ctx, repo, snapshot, config(), "empty")
    lease = await JobService(broker_factory).claim(ctx.org_id, created.job.id, 1, "empty")
    async with api_factory() as s:
        spec = await IndexRepository(s).get_spec(ctx, repo, created.job.index_spec_id)
    assert spec.files[0].size_bytes == 0
    scope = SearchScopeV1(
        org_id=ctx.org_id,
        principal_id=ctx.principal_id,
        repository_id=spec.repository_id,
        snapshot_id=spec.snapshot_id,
    )
    async with api_factory() as s:
        result = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="none")
        )
        assert (
            result.lexical_availability == "index_unavailable"
            and result.coverage_status != "complete"
        )
    # A genuinely empty verified file produces complete zero-chunk coverage.
    draft = IndexPublicationDraftV1(
        org_id=scope.org_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        spec_hash=spec.semantic_hash(),
        config_hash=spec.config.semantic_hash(),
        chunks=(),
        processed_file_ids=tuple(f.file_id for f in spec.files),
        coverage=IndexCoverageV1(admitted_files=1, selected_files=1, processed_files=1),
    )
    await IndexFinalizer(broker_factory).finish(lease, _issue(spec, draft))
    async with api_factory() as s:
        result = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="none")
        )
        assert result.hits == () and result.lexical_availability == "available"
        assert result.coverage_status == "complete" and result.coverage.processed_files == 1


async def test_native_redacted_index_search_and_context_keep_original_provenance(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    import io
    import os
    import zipfile
    from pathlib import Path

    from app.contracts.indexing import IndexBuildConfigV1
    from app.indexing.adapter import DockerIndexBuilder
    from app.indexing.docker import IndexDockerController
    from app.scanners.docker import DockerController
    from app.scanners.ownership import ScannerOwnership
    from app.services.index_jobs import IndexJobService
    from app.services.jobs import JobService
    from app.source_read.adapter import DockerSourceReader
    from app.workers.operations.index_snapshot import IndexOperation

    from tests.phase2.integration import test_submission_ingestion as ingestion
    from tests.phase3.integration.test_structural_publication import admitted

    image, source_hash, reader_image = (
        os.environ.get(k)
        for k in (
            "CODESPY_INDEXER_IMAGE",
            "CODESPY_INDEXER_SOURCE_HASH",
            "CODESPY_SOURCE_READER_IMAGE",
        )
    )
    if not all((image, source_hash, reader_image)):
        pytest.skip("owned qualified reader/indexer images required")
    canary = b"codespy_sec_canary_retrieval_123456789"
    raw = b"token = '" + canary + b"'\nprint(1)\n"
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("a.py", raw)
    monkeypatch.setattr(ingestion, "zip_bytes", body.getvalue)
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    socket = Path.home() / ".docker/run/docker.sock"
    reader = DockerSourceReader(
        ScannerOwnership.create(tmp_path / "reader-owner", reader_image),
        DockerController(Path("/usr/local/bin/docker"), socket, tmp_path / "reader-config"),
        store,
    )
    root = tmp_path / "verified"
    root.mkdir(mode=0o700)
    builder = DockerIndexBuilder(
        ScannerOwnership.create(tmp_path / "index-owner", image),
        IndexDockerController(Path("/usr/local/bin/docker"), socket, tmp_path / "index-config"),
        root,
        runtime_source_sha256=source_hash,
    )
    svc = IndexJobService(api_factory, builder)
    created = await svc.create(
        ctx,
        repo,
        snapshot,
        IndexBuildConfigV1(runtime_image=image, runtime_source_sha256=source_hash),
        "native-search",
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, created.job.id, 1, "native-search")
    try:

        async def gate():
            await JobService(broker_factory).heartbeat(lease)

        receipt = await IndexOperation(
            broker_factory,
            store,
            reader,
            builder,
            IndexFinalizer(broker_factory),
            verified_root=root,
        ).execute(lease, enter_finalization=gate)
        scope = SearchScopeV1(
            org_id=ctx.org_id,
            principal_id=ctx.principal_id,
            repository_id=repo,
            snapshot_id=snapshot,
        )
        async with api_factory() as s:
            found = await HybridSearchService(s).search_hybrid(
                HybridSearchRequestV1(scope=scope, query="print")
            )
            assert found.chunk_set_id == receipt.chunk_set_id and found.hits
            packet = await ContextAssemblyService(s).assemble_context(
                ContextAssemblyRequestV1(
                    scope=found.scope,
                    query="print",
                    candidate_hits=found.hits,
                    budget=ContextBudgetV1(
                        max_tokens=128, max_bytes=512, reserved_overhead_tokens=0
                    ),
                )
            )
            assert canary not in found.model_dump_json().encode()
            assert canary not in packet.model_dump_json().encode()
            assert packet.items[0].start_byte == 0 and packet.items[0].end_byte == len(raw)
            assert packet.items[0].safe_content_sha256 != found.hits[0].content_sha256
            assert packet.total_tokens <= 128 and packet.total_bytes <= 512
            assert packet.items[0].citation_key == "[a.py#L1-L2]"
            with pytest.raises(ScopeViolation):
                await HybridSearchService(s).search_hybrid(
                    HybridSearchRequestV1(
                        scope=scope.model_copy(update={"snapshot_id": uuid4()}), query="print"
                    )
                )
    finally:
        await builder.recover_owned()
        await reader.recover_owned()
    assert list(root.iterdir()) == []


async def test_qualified_history_cannot_be_cleared_or_resealed(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.services.index_lifecycle import IndexLifecycleService

    _, scope, receipt = await published(api_factory, broker_factory, migration_factory, tmp_path)
    async with broker_factory() as s:
        with pytest.raises(ScopeViolation):
            await LexicalSearchService(s).clear_snapshot_chunks(scope.org_id, scope.snapshot_id)
        with pytest.raises((ScopeViolation, ValueError)):
            await IndexLifecycleService(s).seal_index(scope.org_id, receipt.index_id)
        assert await s.scalar(select(func.count()).select_from(source_chunks)) == 1


@pytest.mark.parametrize("state", ["building", "failed", "stale"])
async def test_unqualified_index_states_cannot_replace_published_selection(
    api_factory, broker_factory, migration_factory, tmp_path, state
):
    from app.models.chunk_tables import search_indices

    _, scope, receipt = await published(api_factory, broker_factory, migration_factory, tmp_path)
    unqualified = uuid4()
    async with migration_factory() as s, s.begin():
        await s.execute(
            insert(search_indices).values(
                org_id=scope.org_id,
                id=unqualified,
                repository_id=scope.repository_id,
                snapshot_id=scope.snapshot_id,
                index_type="lexical",
                state=state,
                chunk_set_id=None,
            )
        )
    async with api_factory() as s:
        result = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="a.py")
        )
        assert result.chunk_set_id == receipt.chunk_set_id and len(result.hits) == 1
        with pytest.raises(ScopeViolation):
            await LexicalSearchService(s).search_exact(
                ExactSearchRequestV1(
                    scope=scope.model_copy(update={"chunk_set_id": unqualified}), query="a.py"
                )
            )


async def test_partial_parent_coverage_is_preserved_in_search_and_context(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.contracts.indexing import IndexCoverageV1, IndexOmissionV1
    from app.indexing.receipt import _issue

    ctx, _, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    coverage = IndexCoverageV1(
        admitted_files=1,
        selected_files=1,
        processed_files=1,
        omissions=(IndexOmissionV1(reason="chunk_limit", unit="chunk", count=1),),
    )
    partial = prepared.draft.model_copy(update={"coverage": coverage})
    receipt = await IndexFinalizer(broker_factory).finish(lease, _issue(prepared.spec, partial))
    scope = SearchScopeV1(
        org_id=ctx.org_id,
        principal_id=ctx.principal_id,
        repository_id=prepared.spec.repository_id,
        snapshot_id=prepared.spec.snapshot_id,
    )
    async with api_factory() as s:
        exact = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="a.py")
        )
        hybrid = await HybridSearchService(s).search_hybrid(
            HybridSearchRequestV1(scope=scope, query="print")
        )
        assert exact.coverage_status == hybrid.coverage_status == "partial"
        assert exact.coverage == hybrid.coverage == receipt.coverage == coverage
        context = await ContextAssemblyService(s).assemble_context(
            ContextAssemblyRequestV1(scope=hybrid.scope, query="print", candidate_hits=hybrid.hits)
        )
        assert context.coverage == coverage and context.chunk_set_id == receipt.chunk_set_id


async def test_existing_foreign_materialization_cannot_be_selected(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, scope, own = await published(
        api_factory, broker_factory, migration_factory, tmp_path / "own"
    )
    _, _, foreign = await published(
        api_factory, broker_factory, migration_factory, tmp_path / "foreign"
    )
    assert own.chunk_set_id != foreign.chunk_set_id
    async with api_factory() as s:
        with pytest.raises(ScopeViolation):
            await HybridSearchService(s).search_hybrid(
                HybridSearchRequestV1(
                    scope=scope.model_copy(update={"chunk_set_id": foreign.chunk_set_id}),
                    query="print",
                )
            )
        result = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=scope, query="a.py")
        )
        assert result.chunk_set_id == own.chunk_set_id
        assert all(hit.chunk_set_id == own.chunk_set_id for hit in result.hits)
