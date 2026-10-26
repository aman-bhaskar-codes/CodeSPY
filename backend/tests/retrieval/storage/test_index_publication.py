"""Real durable publication using synthetic preparation receipts, not native qualification."""

import asyncio

import pytest
from app.contracts.jobs import AttemptLeaseV2
from app.domain.errors import DuplicateConflict, ScopeViolation, StaleAttempt
from app.indexing.receipt import _issue
from app.models.chunk_tables import search_indices, source_chunks
from app.models.index_tables import index_jobs, index_publication_receipts, retrieval_chunk_sets
from app.models.phase2_tables import PHASE2_TABLES
from app.services.index_publication import IndexFinalizer
from app.services.jobs import JobService
from sqlalchemy import func, select, text, update

from tests.retrieval.storage.test_index_guards import source_case
from tests.retrieval.storage.test_index_jobs import service
from tests.retrieval.unit.test_index_contracts import chunk, draft


async def claimed(api, broker, migration, tmp_path, key="publish"):
    ctx, initial, _ = await source_case(migration, tmp_path)
    created = await service(api).create(
        ctx, initial.repository_id, initial.snapshot_id, initial.config, key
    )
    lease = await JobService(broker).claim(ctx.org_id, created.job.id, 1, "publisher")
    assert isinstance(lease, AttemptLeaseV2)
    from app.repositories.index_repository import IndexRepository

    async with api() as s:
        spec = await IndexRepository(s).get_spec(
            ctx, initial.repository_id, created.job.index_spec_id
        )
    assert spec is not None
    result = draft(spec, chunks=(chunk(spec.files[0], "print(1)\n"),))
    return ctx, created, lease, _issue(spec, result)


async def test_finalizer_commits_complete_set_and_idempotent_receipt(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, created, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    finalizer = IndexFinalizer(broker_factory)
    receipt = await finalizer.finish(lease, prepared)
    assert receipt == await finalizer.finish(lease, prepared)
    assert receipt.index_id == prepared.draft.lexical_index_id()
    status = await service(api_factory).get(
        ctx, prepared.spec.repository_id, prepared.spec.snapshot_id, created.job.id
    )
    assert status.job.state == "succeeded" and status.receipt == receipt
    async with broker_factory() as s:
        row = (await s.execute(select(source_chunks))).mappings().one()
        assert row["id"] == prepared.draft.stored_chunk_id(prepared.draft.chunks[0])
        assert row["safe_content"] == "print(1)\n" and row["tsv"] is not None
        assert await s.scalar(text("SELECT tsv @@ to_tsquery('simple','py:B') FROM source_chunks"))
        assert await s.scalar(
            text("SELECT tsv @@ to_tsquery('simple','print:C') FROM source_chunks")
        )
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 1


@pytest.mark.parametrize("cause", ["expired", "cancelled", "revoked", "source_deleted"])
async def test_lost_authority_never_publishes(
    api_factory, broker_factory, migration_factory, tmp_path, cause
):
    ctx, created, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    if cause == "cancelled":
        await JobService(api_factory).request_cancel(ctx, created.job.id)
    else:
        async with migration_factory() as s, s.begin():
            if cause == "expired":
                table = PHASE2_TABLES["job_attempts"]
                await s.execute(
                    update(table)
                    .where(table.c.id == lease.attempt_id)
                    .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
                )
            elif cause == "revoked":
                grants = PHASE2_TABLES["repository_grants"]
                await s.execute(grants.delete().where(grants.c.principal_id == ctx.principal_id))
                from app.models.tables import memberships

                await s.execute(
                    update(memberships)
                    .where(memberships.c.principal_id == ctx.principal_id)
                    .values(role="viewer")
                )
            else:
                objects = PHASE2_TABLES["object_records"]
                await s.execute(
                    update(objects)
                    .where(objects.c.key == prepared.spec.source.key)
                    .values(state="deleted")
                )
    with pytest.raises((StaleAttempt, ScopeViolation, DuplicateConflict)):
        await IndexFinalizer(broker_factory).finish(lease, prepared)
    async with broker_factory() as s:
        assert await s.scalar(select(func.count()).select_from(retrieval_chunk_sets)) == 0
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 0


async def test_concurrent_identical_requests_share_immutable_set(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, _created, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    second = await service(api_factory).create(
        ctx, prepared.spec.repository_id, prepared.spec.snapshot_id, prepared.spec.config, "second"
    )
    lease2 = await JobService(broker_factory).claim(ctx.org_id, second.job.id, 1, "publisher-2")
    assert isinstance(lease2, AttemptLeaseV2)
    receipts = await asyncio.gather(
        *(
            IndexFinalizer(broker_factory).finish(current_lease, prepared)
            for current_lease in (lease, lease2)
        )
    )
    assert receipts[0].chunk_set_id == receipts[1].chunk_set_id
    async with broker_factory() as s:
        for table in (retrieval_chunk_sets, search_indices, source_chunks):
            assert await s.scalar(select(func.count()).select_from(table)) == 1
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 2


async def test_lost_commit_ack_recovers_receipt_from_new_session(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, _, lease, prepared = await claimed(api_factory, broker_factory, migration_factory, tmp_path)
    finalizer = IndexFinalizer(broker_factory)

    async def lost_ack(session):
        await session.commit()
        raise OSError("synthetic lost acknowledgement")

    finalizer.executor.commit_hook = lost_ack
    receipt = await finalizer.finish(lease, prepared)
    assert receipt == await finalizer._durable(lease, prepared.draft_digest)


async def test_total_budget_rolls_back_multiple_individually_fast_statements(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, _, lease, prepared = await claimed(api_factory, broker_factory, migration_factory, tmp_path)
    finalizer = IndexFinalizer(
        broker_factory, budget_seconds=0.25, safety_seconds=0.1, cleanup_seconds=0.1
    )
    from app.services.publication import PublicationExecutor

    class SlowExecutor(PublicationExecutor):
        async def execute(self, work, resolve):
            async def delayed(session, budget):
                await session.execute(text("SELECT pg_sleep(0.15)"))
                await session.execute(text("SELECT pg_sleep(0.15)"))
                return await work(session, budget)

            return await super().execute(delayed, resolve)

    finalizer.executor = SlowExecutor(
        broker_factory, budget_seconds=0.25, safety_seconds=0.1, cleanup_seconds=0.1
    )
    with pytest.raises(TimeoutError):
        await finalizer.finish(lease, prepared)
    async with broker_factory() as s:
        assert await s.scalar(text("SELECT 1")) == 1
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 0
        assert await s.scalar(select(func.count()).select_from(retrieval_chunk_sets)) == 0


async def test_unattested_draft_is_rejected_before_publication(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, _, lease, prepared = await claimed(api_factory, broker_factory, migration_factory, tmp_path)
    with pytest.raises((ValueError, DuplicateConflict)):
        await IndexFinalizer(broker_factory).finish(lease, prepared.draft)


async def test_configured_consumer_publishes_and_acks_only_durable_receipt(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.domain.canonical_json import canonical_bytes
    from app.ports.queue import Delivery
    from app.repositories.index_repository import IndexRepository
    from app.workers.consumer import JobConsumer

    ctx, initial, _ = await source_case(migration_factory, tmp_path)
    created = await service(api_factory).create(
        ctx, initial.repository_id, initial.snapshot_id, initial.config, "consumer"
    )
    async with api_factory() as s:
        payload = await s.scalar(
            select(PHASE2_TABLES["outbox"].c.payload).where(
                PHASE2_TABLES["outbox"].c.job_id == created.job.id
            )
        )
        stored = await IndexRepository(s).get_spec(
            ctx, initial.repository_id, created.job.index_spec_id
        )

    class Queue:
        acked = False

        async def ack(self, receipt):
            status = await service(api_factory).get(
                ctx, stored.repository_id, stored.snapshot_id, created.job.id
            )
            assert status.receipt is not None
            self.acked = True

        async def extend(self, *args):
            pass

    class Operation:
        async def execute(self, lease, *, enter_finalization):
            await enter_finalization()
            return await IndexFinalizer(broker_factory).finish(lease, _issue(stored, draft(stored)))

    queue = Queue()
    await JobConsumer(broker_factory, queue, None, None, index_operation=Operation()).handle(
        Delivery(canonical_bytes(payload), "delivery")
    )
    assert queue.acked


@pytest.mark.parametrize("cause", ["cancel", "revoke", "delete", "digest"])
async def test_worker_rechecks_authority_after_unlocked_source_read(
    api_factory, broker_factory, migration_factory, tmp_path, cause
):
    from app.domain.errors import ScopeViolation
    from app.ports.index_builder import IndexCapabilitiesV1
    from app.workers.operations.index_snapshot import IndexOperation

    ctx, created, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )

    class Reader:
        async def read(self, request):
            # Independent writes complete while source I/O is awaited.
            async with asyncio.timeout(2):
                if cause == "cancel":
                    await JobService(api_factory).request_cancel(ctx, created.job.id)
                elif cause == "revoke":
                    from app.models.tables import memberships

                    async with migration_factory() as s, s.begin():
                        await s.execute(
                            update(memberships)
                            .where(
                                memberships.c.org_id == ctx.org_id,
                                memberships.c.principal_id == ctx.principal_id,
                            )
                            .values(role="viewer")
                        )
                elif cause == "delete":
                    table = PHASE2_TABLES["object_records"]
                    async with migration_factory() as s, s.begin():
                        await s.execute(
                            update(table)
                            .where(table.c.key == prepared.spec.source.key)
                            .values(state="deleted")
                        )
            return b"wrong" if cause == "digest" else b"print(1)\n"

    class Builder:
        async def capabilities(self):
            return IndexCapabilitiesV1(
                runtime_source_sha256=prepared.spec.config.runtime_source_sha256,
                max_request_bytes=98368,
            )

        async def build(self, *args, **kwargs):
            raise AssertionError("lost authority or corrupt input must never reach builder")

    root = tmp_path / "worker-inputs"
    root.mkdir()
    operation = IndexOperation(
        broker_factory,
        None,
        Reader(),
        Builder(),
        IndexFinalizer(broker_factory),
        verified_root=root,
    )
    with pytest.raises((StaleAttempt, ScopeViolation, DuplicateConflict)):
        await operation.execute(lease)
    assert list(root.iterdir()) == []
    async with broker_factory() as s:
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 0


async def test_conflicting_replay_preserves_existing_set_and_changed_config_creates_new_set(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, _, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    first = await IndexFinalizer(broker_factory).finish(lease, prepared)
    svc = service(api_factory)
    second = await svc.create(
        ctx,
        prepared.spec.repository_id,
        prepared.spec.snapshot_id,
        prepared.spec.config,
        "conflict",
    )
    next_lease = await JobService(broker_factory).claim(ctx.org_id, second.job.id, 1, "other")
    conflicting = draft(prepared.spec, chunks=(chunk(prepared.spec.files[0], "prin"),))
    with pytest.raises(DuplicateConflict):
        await IndexFinalizer(broker_factory).finish(next_lease, _issue(prepared.spec, conflicting))
    await JobService(api_factory).request_cancel(ctx, second.job.id)
    await JobService(broker_factory).recover_due()
    changed_config = prepared.spec.config.model_copy(update={"runtime_source_sha256": "f" * 64})
    third = await svc.create(
        ctx, prepared.spec.repository_id, prepared.spec.snapshot_id, changed_config, "changed"
    )
    final_lease = await JobService(broker_factory).claim(ctx.org_id, third.job.id, 1, "changed")
    from app.repositories.index_repository import IndexRepository

    async with api_factory() as s:
        changed_spec = await IndexRepository(s).get_spec(
            ctx, prepared.spec.repository_id, third.job.index_spec_id
        )
    second_receipt = await IndexFinalizer(broker_factory).finish(
        final_lease, _issue(changed_spec, draft(changed_spec))
    )
    assert first.chunk_set_id != second_receipt.chunk_set_id
    async with broker_factory() as s:
        assert await s.scalar(select(func.count()).select_from(retrieval_chunk_sets)) == 2
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 2


async def test_worker_aggregate_preparation_timeout_cleans_staging(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.ports.index_builder import IndexCapabilitiesV1
    from app.scanners.process import NativeProcessError
    from app.workers.operations.index_snapshot import IndexOperation

    _ctx, created, lease, prepared = await claimed(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    # Owner-only fixture expiry relative to live DB time; no production clock override.
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(index_jobs)
            .where(index_jobs.c.id == created.job.id)
            .values(deadline=func.clock_timestamp() + text("interval '65.2 seconds'"))
        )
    closed = asyncio.Event()

    class Reader:
        async def read(self, request):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    class Builder:
        async def capabilities(self):
            return IndexCapabilitiesV1(
                runtime_source_sha256=prepared.spec.config.runtime_source_sha256,
                max_request_bytes=98368,
            )

    root = tmp_path / "timeout-inputs"
    root.mkdir()
    with pytest.raises(NativeProcessError, match="index_timeout"):
        await IndexOperation(
            broker_factory, None, Reader(), Builder(), None, verified_root=root
        ).execute(lease)
    assert closed.is_set() and list(root.iterdir()) == []


async def test_cancelled_finalization_rolls_back_and_connection_recovers(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.services.publication import PublicationExecutor

    _, _, lease, prepared = await claimed(api_factory, broker_factory, migration_factory, tmp_path)
    entered = asyncio.Event()

    class BlockingExecutor(PublicationExecutor):
        async def execute(self, work, resolve):
            async def block(session, budget):
                result = await work(session, budget)
                entered.set()
                await session.execute(text("SELECT pg_sleep(10)"))
                return result

            return await super().execute(block, resolve)

    finalizer = IndexFinalizer(broker_factory)
    finalizer.executor = BlockingExecutor(broker_factory)
    task = asyncio.create_task(finalizer.finish(lease, prepared))
    async with asyncio.timeout(5):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with broker_factory() as s:
        assert await s.scalar(text("SELECT 1")) == 1
        assert await s.scalar(select(func.count()).select_from(retrieval_chunk_sets)) == 0
        assert await s.scalar(select(func.count()).select_from(index_publication_receipts)) == 0


async def test_native_admission_reader_builder_publication_chain(
    api_factory, broker_factory, migration_factory, tmp_path
):
    import os
    from pathlib import Path

    from app.contracts.indexing import IndexBuildConfigV1
    from app.indexing.adapter import DockerIndexBuilder
    from app.indexing.docker import IndexDockerController
    from app.scanners.docker import DockerController
    from app.scanners.ownership import ScannerOwnership
    from app.services.index_jobs import IndexJobService
    from app.source_read.adapter import DockerSourceReader
    from app.workers.operations.index_snapshot import IndexOperation

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
        pytest.skip("owned native reader and indexer images required")
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    socket = Path.home() / ".docker/run/docker.sock"
    reader_controller = DockerController(
        Path("/usr/local/bin/docker"), socket, tmp_path / "reader-config"
    )
    reader_owner = ScannerOwnership.create(tmp_path / "reader-owner", reader_image)
    reader = DockerSourceReader(reader_owner, reader_controller, store)
    index_controller = IndexDockerController(
        Path("/usr/local/bin/docker"), socket, tmp_path / "index-config"
    )
    index_owner = ScannerOwnership.create(tmp_path / "index-owner", image)
    root = tmp_path / "verified"
    root.mkdir(mode=0o700)
    builder = DockerIndexBuilder(
        index_owner, index_controller, root, runtime_source_sha256=source_hash
    )
    config = IndexBuildConfigV1(runtime_image=image, runtime_source_sha256=source_hash)
    created = await IndexJobService(api_factory, builder).create(
        ctx, repo, snapshot, config, "native"
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, created.job.id, 1, "native")
    try:
        operation = IndexOperation(
            broker_factory,
            store,
            reader,
            builder,
            IndexFinalizer(broker_factory),
            verified_root=root,
        )

        async def gate():
            await JobService(broker_factory).heartbeat(lease)

        result = await operation.execute(lease, enter_finalization=gate)
        status = await IndexJobService(api_factory, builder).get(
            ctx, repo, snapshot, created.job.id
        )
        assert result == status.receipt and status.job.state == "succeeded"
        async with broker_factory() as s:
            assert (
                await s.scalar(
                    select(source_chunks.c.safe_content).where(
                        source_chunks.c.chunk_set_id == result.chunk_set_id
                    )
                )
                == "print(1)\n"
            )
        assert list(root.iterdir()) == []
    finally:
        await builder.recover_owned()
        await reader.recover_owned()


@pytest.mark.performance
async def test_native_maximum_materialization_commits_inside_publication_budget(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    import io
    import os
    import time
    import zipfile

    from app.admission.zip_inventory import inspect_zip_bytes
    from app.contracts.ingestion import AdmissionPolicyV1
    from app.ports.index_builder import VerifiedIndexInputV1
    from app.repositories.index_repository import IndexRepository
    from app.services.index_jobs import IndexJobService
    from app.services.job_finalization import JobFinalizer

    from tests.phase2.integration import test_submission_ingestion as ingestion
    from tests.retrieval.native.test_index_runtime import runtime

    if os.environ.get("CODESPY_INDEX_PUBLICATION_CAPACITY") != "1":
        pytest.skip("owned maximum-bound publication qualification not opted in")
    unit = b"value = 123456789\n"
    raw = (unit * (131072 // len(unit) + 1))[:131072]
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", compression=zipfile.ZIP_STORED) as archive:
        for i in range(128):
            archive.writestr(f"src/{i:03d}.py", raw)
    payload = body.getvalue()
    monkeypatch.setattr(ingestion, "zip_bytes", lambda: payload)
    svc, ctx, _, submission, _, store = await ingestion.submission_scenario(
        migration_factory, tmp_path
    )

    async def blocks():
        for offset in range(0, len(payload), 65536):
            yield payload[offset : offset + 65536]

    uploaded = await svc.write_content(ctx, submission.id, blocks())
    admission_lease = await JobService(migration_factory).claim(
        ctx.org_id, uploaded.job_id, 1, "admission"
    )
    admission = JobFinalizer(migration_factory, store)
    inventory = await admission.prepare_admission(
        admission_lease, inspect_zip_bytes(payload, AdmissionPolicyV1())
    )
    admitted = await admission.finish_admission(admission_lease, inventory)
    _, _, builder, root, config = runtime(tmp_path / "native")
    created = await IndexJobService(api_factory, builder).create(
        ctx, submission.repository_id, admitted.snapshot_id, config, "capacity"
    )
    lease = await JobService(broker_factory).claim(ctx.org_id, created.job.id, 1, "capacity")
    async with api_factory() as s:
        spec = await IndexRepository(s).get_spec(
            ctx, submission.repository_id, created.job.index_spec_id
        )
    inputs = []
    for file in spec.files:
        path = root / str(file.file_id)
        path.write_bytes(raw)
        inputs.append(VerifiedIndexInputV1(file, path))
    prepared = await builder.build(spec, tuple(inputs), timeout_seconds=180)
    assert len(prepared.draft.chunks) == 4096 and sum(f.size_bytes for f in spec.files) == 16777216
    await JobService(broker_factory).heartbeat(lease)
    started = time.monotonic()
    receipt = await IndexFinalizer(broker_factory).finish(lease, prepared)
    elapsed = time.monotonic() - started
    assert elapsed < 60 and receipt.coverage.status == "partial"
    async with broker_factory() as s:
        assert await s.scalar(select(func.count()).select_from(source_chunks)) == 4096
    print(
        f"maximum_publication_seconds={elapsed:.3f}; chunks=4096; "
        "source_bytes=16777216; coverage=partial"
    )
    await builder.recover_owned()
