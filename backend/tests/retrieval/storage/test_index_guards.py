"""Real SQL authority checks with explicitly synthetic publication preparation."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4, uuid5

import pytest
from app.contracts.indexing import INDEX_NAMESPACE, IndexFileV1, IndexSpecV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.models.index_tables import index_specs
from app.models.phase2_tables import PHASE2_TABLES
from app.services.analysis_sources import locate_source
from scripts import phase2_db as harness
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.phase3.integration.test_structural_publication import admitted
from tests.retrieval.unit.test_index_contracts import config


async def source_case(migration_factory, tmp_path):
    ctx, snapshot, repo, _ = await admitted(migration_factory, tmp_path)
    async with migration_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
        objects = (
            (
                await session.execute(
                    select(PHASE2_TABLES["object_records"]).where(
                        PHASE2_TABLES["object_records"].c.org_id == ctx.org_id
                    )
                )
            )
            .mappings()
            .all()
        )
    files = tuple(
        IndexFileV1(file_id=f.file_id, path=f.path, sha256=f.sha256, size_bytes=f.size_bytes)
        for f in located.files
    )
    spec = IndexSpecV1(
        org_id=ctx.org_id,
        repository_id=repo,
        snapshot_id=snapshot,
        principal_id=ctx.principal_id,
        config=config(),
        source=located.archive,
        manifest=located.manifest,
        files=files,
        admitted_files=len(files),
    )
    by_key = {row["key"]: row["id"] for row in objects}
    return (
        ctx,
        spec,
        {
            "org_id": ctx.org_id,
            "id": uuid4(),
            "repository_id": repo,
            "snapshot_id": snapshot,
            "principal_id": ctx.principal_id,
            "config_hash": spec.config.semantic_hash(),
            "spec_hash": spec.semantic_hash(),
            "config_json": spec.config.model_dump(mode="json"),
            "spec_json": spec.model_dump(mode="json"),
            "canonical_text": canonical_bytes(spec.model_dump()).decode(),
            "source_object_id": by_key[located.archive.key],
            "manifest_object_id": by_key[located.manifest.key],
        },
    )


async def test_api_can_insert_admitted_spec_and_history_cannot_be_downgraded(
    api_factory, migration_factory, db_values, tmp_path
):
    _, _, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    with pytest.raises(Exception, match="index history must be preserved"):
        harness.migrate(
            db_values, db_values["CODESPY_TEST_DATABASE"], "0022_triage_commands", downgrade=True
        )
    async with api_factory() as session:
        assert await session.scalar(select(index_specs.c.id)) == values["id"]


async def test_changed_source_digest_cannot_create_spec(api_factory, migration_factory, tmp_path):
    _, _, values = await source_case(migration_factory, tmp_path)
    payload = {
        **values["spec_json"],
        "source": {**values["spec_json"]["source"], "sha256": "0" * 64},
    }
    encoded = canonical_bytes(payload)
    values.update(
        spec_json=payload,
        canonical_text=encoded.decode(),
        spec_hash=hashlib.sha256(encoded).hexdigest(),
    )
    async with api_factory() as session:
        with pytest.raises(DBAPIError, match="index admitted source unavailable"):
            await session.execute(index_specs.insert().values(**values))
        await session.rollback()
        assert await session.scalar(select(index_specs.c.id)) is None


async def test_guc_strings_without_live_attempt_cannot_insert_result(broker_factory):
    from app.models.index_tables import retrieval_chunk_sets

    async with broker_factory() as session:
        for key, value in (
            ("org_id", uuid4()),
            ("job_id", uuid4()),
            ("attempt_id", uuid4()),
            ("fence", 1),
        ):
            await session.execute(
                text("SELECT set_config(:key,:value,true)"),
                {"key": "codespy." + key, "value": str(value)},
            )
        with pytest.raises(DBAPIError, match="stale attempt"):
            await session.execute(
                retrieval_chunk_sets.insert().values(
                    org_id=uuid4(),
                    id=uuid4(),
                    repository_id=uuid4(),
                    snapshot_id=uuid4(),
                    config_hash="0" * 64,
                    materialization_hash="0" * 64,
                    manifest_hash="0" * 64,
                    manifest_json={},
                    canonical_text="{}",
                    chunk_count=0,
                    safe_bytes=0,
                    coverage="complete",
                )
            )
        await session.rollback()
        assert await session.scalar(text("SELECT count(*) FROM retrieval_chunk_sets")) == 0


async def test_real_worker_binding_can_commit_one_complete_publication(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.models.index_tables import index_publication_receipts

    case = await source_case(migration_factory, tmp_path)
    ctx, spec, values = case
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    binding = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, binding)
        draft, receipt = await publish_rows(session, spec, values["id"], binding)
        await terminalize(session, ctx.org_id, binding, receipt["index_id"])
    async with broker_factory() as session:
        row = (await session.execute(select(index_publication_receipts))).mappings().one()
        assert row["draft_digest"] == draft.semantic_hash()


async def owned_attempt(factory, ctx, spec, spec_id):
    """Synthetic SQL claim, not JobService/queue qualification (wired separately)."""
    from app.models.index_tables import index_attempt_specs, index_jobs
    from app.models.phase2_tables import PHASE2_TABLES
    from sqlalchemy import func, update

    job_id, attempt_id = uuid4(), uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            index_jobs.insert().values(
                org_id=ctx.org_id,
                id=job_id,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                principal_id=ctx.principal_id,
                index_spec_id=spec_id,
                config_hash=spec.config.semantic_hash(),
                request_key=str(job_id),
                request_hash="1" * 64,
                operation="index_snapshot",
                deadline=func.clock_timestamp() + text("interval '15 minutes'"),
            )
        )
        await session.execute(
            PHASE2_TABLES["job_attempts"]
            .insert()
            .values(
                org_id=ctx.org_id,
                job_id=job_id,
                id=attempt_id,
                fence=1,
                owner="owned-index-fixture",
                lease_expires_at=func.clock_timestamp() + text("interval '90 seconds'"),
                heartbeat_at=func.clock_timestamp(),
            )
        )
        await session.execute(
            update(index_jobs)
            .where(index_jobs.c.id == job_id)
            .values(state="running", attempt_count=1, fence=1, current_attempt_id=attempt_id)
        )
        binding = (job_id, attempt_id, 1)
        await set_worker(session, ctx.org_id, binding)
        await session.execute(
            index_attempt_specs.insert().values(
                org_id=ctx.org_id,
                id=attempt_id,
                job_id=job_id,
                fence=1,
                spec_id=spec_id,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                spec_hash=spec.semantic_hash(),
            )
        )
    return binding


async def set_worker(session, org, binding):
    for key, value in zip(
        ("org_id", "job_id", "attempt_id", "fence"), (org, *binding), strict=True
    ):
        await session.execute(
            text("SELECT set_config(:key,:value,true)"),
            {"key": "codespy." + key, "value": str(value)},
        )


async def publish_rows(
    session,
    spec,
    spec_id,
    binding,
    *,
    receipt=True,
    changes=None,
    receipt_spec_id=None,
    prepared=None,
    manifest_transform=None,
    use_repository=False,
):
    from app.models.chunk_tables import search_indices, source_chunks
    from app.models.index_tables import index_publication_receipts, retrieval_chunk_sets

    from tests.retrieval.unit.test_index_contracts import draft

    result = prepared or draft(spec)
    manifest = result.manifest_payload()
    if manifest_transform is not None:
        manifest = manifest_transform(manifest)
    materialization_hash = semantic_digest(manifest)
    set_id = uuid5(INDEX_NAMESPACE, materialization_hash)
    index_id = uuid5(set_id, "lexical-v1")
    if use_repository:
        from app.repositories.index_repository import IndexRepository

        stored = await IndexRepository(session).insert_set(result)
        assert stored["id"] == set_id
    else:
        await session.execute(
            retrieval_chunk_sets.insert().values(
                org_id=spec.org_id,
                id=set_id,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                config_hash=result.config_hash,
                materialization_hash=materialization_hash,
                manifest_hash=materialization_hash,
                manifest_json=json.loads(canonical_bytes(manifest)),
                canonical_text=canonical_bytes(manifest).decode(),
                chunk_count=len(result.chunks),
                safe_bytes=sum(len(c.safe_content.encode()) for c in result.chunks),
                coverage="complete",
            )
        )
    for c in result.chunks:
        fields = {
            "org_id": spec.org_id,
            "id": uuid5(set_id, semantic_digest(c.model_dump(exclude={"safe_content"}))),
            "repository_id": spec.repository_id,
            "snapshot_id": spec.snapshot_id,
            "file_id": c.file_id,
            "file_path": c.file_path,
            "chunk_index": c.chunk_index,
            "chunk_kind": c.chunk_kind,
            "start_byte": c.start_byte,
            "end_byte": c.end_byte,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "content_sha256": c.content_sha256,
            "byte_count": c.end_byte - c.start_byte,
            "estimated_tokens": c.token_count,
            "safe_content": c.safe_content,
            "redaction_digest": c.redaction_digest,
            "chunker_version": spec.config.chunker_version,
            "chunk_set_id": set_id,
            "safe_start_byte": c.safe_start_byte,
            "safe_end_byte": c.safe_end_byte,
            "safe_content_sha256": c.safe_content_sha256,
            "provenance_json": c.model_dump(mode="json", exclude={"safe_content"}),
        }
        await session.execute(source_chunks.insert().values(**{**fields, **(changes or {})}))
    await session.execute(
        search_indices.insert().values(
            org_id=spec.org_id,
            id=index_id,
            repository_id=spec.repository_id,
            snapshot_id=spec.snapshot_id,
            index_type="lexical",
            state="ready",
            chunk_set_id=set_id,
            total_chunks=len(result.chunks),
            indexed_chunks=len(result.chunks),
            failed_chunks=0,
            sealed_at=datetime.now(UTC),
        )
    )
    values = {
        "org_id": spec.org_id,
        "job_id": binding[0],
        "attempt_id": binding[1],
        "fence": binding[2],
        "repository_id": spec.repository_id,
        "snapshot_id": spec.snapshot_id,
        "spec_id": receipt_spec_id or spec_id,
        "spec_hash": spec.semantic_hash(),
        "chunk_set_id": set_id,
        "index_id": index_id,
        "draft_digest": result.semantic_hash(),
        "materialization_hash": materialization_hash,
        "coverage": "complete",
    }
    if receipt:
        await session.execute(index_publication_receipts.insert().values(**values))
    return result, values


async def terminalize(session, org, binding, index_id):
    from app.models.index_tables import index_jobs
    from sqlalchemy import func, update

    await session.execute(
        update(index_jobs)
        .where(index_jobs.c.org_id == org, index_jobs.c.id == binding[0])
        .values(state="succeeded", result_index_id=index_id, finished_at=func.clock_timestamp())
    )
    attempts = PHASE2_TABLES["job_attempts"]
    await session.execute(
        update(attempts)
        .where(attempts.c.org_id == org, attempts.c.id == binding[1])
        .values(state="succeeded", finished_at=func.clock_timestamp())
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_receipt",
        "expired_lease",
        "wrong_spec",
        "foreign_file",
        "wrong_safe_digest",
        "wrong_set_hash",
        "wrong_raw_size",
        "missing_worker_context",
    ],
)
async def test_invalid_publication_rolls_back_all_rows(
    api_factory, broker_factory, migration_factory, tmp_path, scenario
):
    from app.models.index_tables import (
        index_publication_receipts,
        retrieval_chunk_sets,
    )
    from sqlalchemy import func, update

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    binding = await owned_attempt(migration_factory, ctx, spec, values["id"])
    if scenario == "expired_lease":
        attempts = PHASE2_TABLES["job_attempts"]
        async with migration_factory() as session, session.begin():
            await session.execute(
                update(attempts)
                .where(attempts.c.id == binding[1])
                .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
            )
    async with broker_factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                if scenario != "missing_worker_context":
                    await set_worker(session, ctx.org_id, binding)
                changed = (
                    {"file_id": uuid4()}
                    if scenario == "foreign_file"
                    else {"safe_content_sha256": "0" * 64}
                    if scenario == "wrong_safe_digest"
                    else {"chunk_set_id": uuid4()}
                    if scenario == "wrong_set_hash"
                    else {"byte_count": 0}
                    if scenario == "wrong_raw_size"
                    else None
                )
                _, received = await publish_rows(
                    session,
                    spec,
                    values["id"],
                    binding,
                    receipt=scenario != "missing_receipt",
                    changes=changed,
                    receipt_spec_id=uuid4() if scenario == "wrong_spec" else None,
                )
                if scenario != "missing_receipt":
                    await terminalize(session, ctx.org_id, binding, received["index_id"])
                # Missing receipt is rejected by deferred guard at COMMIT, not by this helper.
    async with broker_factory() as session:
        for table in (retrieval_chunk_sets, index_publication_receipts):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        assert await session.scalar(text("SELECT 1")) == 1


async def test_repository_reads_typed_spec_and_exact_attempt_receipt(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.repositories.index_repository import IndexRepository

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
        repository = IndexRepository(session)
        assert await repository.get_spec(ctx, spec.repository_id, values["id"]) == spec
        assert await repository.get_spec(ctx, spec.repository_id, uuid4()) is None
    binding = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, binding)
        result, receipt = await publish_rows(
            session, spec, values["id"], binding, use_repository=True
        )
        await terminalize(session, ctx.org_id, binding, receipt["index_id"])
    async with broker_factory() as session:
        from app.contracts.search import SearchScopeV1

        repository = IndexRepository(session)
        scope = SearchScopeV1(
            org_id=ctx.org_id,
            repository_id=spec.repository_id,
            snapshot_id=spec.snapshot_id,
            principal_id=ctx.principal_id,
        )
        parent = await repository.locked_set(scope, spec.config.semantic_hash())
        assert parent is not None and parent["id"] == result.chunk_set_id()
        assert await repository.locked_set(scope, "0" * 64) is None
        stored = await repository.get_receipt(ctx.org_id, *binding)
        assert stored is not None and stored.chunk_set_id == result.chunk_set_id()
        assert stored.config_hash == spec.config.semantic_hash()
        assert (
            await repository.get_receipt(ctx.org_id, binding[0], binding[1], binding[2] + 1) is None
        )
        assert await repository.get_receipt(uuid4(), *binding) is None


@pytest.mark.parametrize("change", ["missing_scope", "duplicate_file", "unqualified_privacy"])
async def test_direct_spec_insert_cannot_bypass_shape_or_profile_checks(
    api_factory, migration_factory, tmp_path, change
):
    from app.domain.canonical_json import semantic_digest

    _, _, values = await source_case(migration_factory, tmp_path)
    payload = values["spec_json"]
    if change == "missing_scope":
        payload.pop("org_id")
    elif change == "duplicate_file":
        payload["files"] = payload["files"] * 2
        payload["admitted_files"] = 2
    else:
        payload["config"]["privacy_policy"]["redact_secrets"] = False
        values["config_json"] = payload["config"]
        values["config_hash"] = semantic_digest(payload["config"])
    raw = canonical_bytes(payload)
    values.update(
        spec_json=payload, canonical_text=raw.decode(), spec_hash=hashlib.sha256(raw).hexdigest()
    )
    async with api_factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(index_specs.insert().values(**values))
        await session.rollback()


async def test_reused_set_receipt_needs_its_own_terminal_job(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.models.index_tables import index_publication_receipts

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    first = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, first)
        _, receipt = await publish_rows(session, spec, values["id"], first)
        await terminalize(session, ctx.org_id, first, receipt["index_id"])
    second = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session:
        with pytest.raises(DBAPIError, match="uncommitted index publication"):
            async with session.begin():
                await set_worker(session, ctx.org_id, second)
                await session.execute(
                    index_publication_receipts.insert().values(
                        **{
                            **receipt,
                            "job_id": second[0],
                            "attempt_id": second[1],
                            "fence": second[2],
                        }
                    )
                )

    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, second)
        await session.execute(
            index_publication_receipts.insert().values(
                **{**receipt, "job_id": second[0], "attempt_id": second[1], "fence": second[2]}
            )
        )
        await terminalize(session, ctx.org_id, second, receipt["index_id"])
    async with broker_factory() as session:
        rows = (await session.execute(select(index_publication_receipts.c.job_id))).scalars().all()
        assert set(rows) == {first[0], second[0]}


@pytest.mark.parametrize("scenario", ["duplicate_manifest", "false_complete_coverage"])
async def test_manifest_is_a_complete_unambiguous_publication(
    api_factory, broker_factory, migration_factory, tmp_path, scenario
):
    from app.contracts.indexing import IndexCoverageV1, IndexOmissionV1, IndexPublicationDraftV1

    from tests.retrieval.unit.test_index_contracts import draft

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    binding = await owned_attempt(migration_factory, ctx, spec, values["id"])
    prepared = draft(spec)
    transform = None
    if scenario == "duplicate_manifest":
        second = prepared.chunks[0].model_copy(update={"chunk_index": 1})
        prepared = IndexPublicationDraftV1.model_validate(
            {**prepared.model_dump(), "chunks": (prepared.chunks[0], second)}
        )

        def transform(manifest):
            return {**manifest, "chunks": [manifest["chunks"][0], manifest["chunks"][0]]}
    else:
        prepared = draft(
            spec,
            coverage=IndexCoverageV1(
                admitted_files=1,
                selected_files=1,
                processed_files=1,
                omissions=(IndexOmissionV1(reason="chunk_limit", unit="chunk", count=1),),
            ),
        )
    async with broker_factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await set_worker(session, ctx.org_id, binding)
                _, receipt = await publish_rows(
                    session,
                    spec,
                    values["id"],
                    binding,
                    prepared=prepared,
                    manifest_transform=transform,
                )
                await terminalize(session, ctx.org_id, binding, receipt["index_id"])


async def test_receipt_cannot_publish_a_different_configuration(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.models.index_tables import index_publication_receipts

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    first = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, first)
        _, receipt = await publish_rows(session, spec, values["id"], first)
        await terminalize(session, ctx.org_id, first, receipt["index_id"])
    changed = IndexSpecV1.model_validate(
        {**spec.model_dump(), "config": config(runtime_source_sha256="2" * 64)}
    )
    raw = canonical_bytes(changed.model_dump())
    second_values = {
        **values,
        "id": uuid4(),
        "config_hash": changed.config.semantic_hash(),
        "config_json": changed.config.model_dump(mode="json"),
        "spec_json": changed.model_dump(mode="json"),
        "canonical_text": raw.decode(),
        "spec_hash": changed.semantic_hash(),
    }
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**second_values))
    second = await owned_attempt(migration_factory, ctx, changed, second_values["id"])
    async with broker_factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await set_worker(session, ctx.org_id, second)
                await session.execute(
                    index_publication_receipts.insert().values(
                        **{
                            **receipt,
                            "job_id": second[0],
                            "attempt_id": second[1],
                            "spec_id": second_values["id"],
                            "spec_hash": changed.semantic_hash(),
                        }
                    )
                )
                await terminalize(session, ctx.org_id, second, receipt["index_id"])


async def test_null_membership_cannot_attach_embeddings_to_qualified_content(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.models.chunk_tables import chunk_embeddings

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    binding = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, binding)
        result, receipt = await publish_rows(session, spec, values["id"], binding)
        await terminalize(session, ctx.org_id, binding, receipt["index_id"])
    async with api_factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    chunk_embeddings.insert().values(
                        org_id=ctx.org_id,
                        repository_id=spec.repository_id,
                        snapshot_id=spec.snapshot_id,
                        chunk_id=result.stored_chunk_id(result.chunks[0]),
                        index_id=receipt["index_id"],
                        chunk_set_id=None,
                        embedding_json=[0.5, 0.5],
                        token_count=4,
                    )
                )
