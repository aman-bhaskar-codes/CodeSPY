"""Real scoped publication/retry/source reads; only sandbox transport is a fixture."""

from uuid import uuid4

from app.adapters.queue.memory import MemoryQueue
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.source import AdmittedSourceReader
from app.contracts.ingestion import AdmissionPolicyV1, AdmissionPolicyV2, UploadReservationV1
from app.contracts.structure import AnalysisConfigV1
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import source_files
from app.services.analysis_sources import locate_source, stage_source
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.retries import RetryService
from app.services.submissions import SubmissionService
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from sqlalchemy import select
from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario
from tests.phase4.unit.test_admission_policy import archive


async def test_mixed_policy_admissions_preserve_snapshots_inventory_and_retry(factory, tmp_path):
    _, ctx, reservation, _, _, store = await submission_scenario(factory, tmp_path)
    body = archive([("main.py", b"def main(): pass\n"), ("uv.lock", b"version = 1\n")])
    queue = MemoryQueue()
    finalizer = JobFinalizer(factory, store)
    snapshots = []
    policies = []

    class FixtureProcessor:
        async def inspect(self, lease, policy):
            policies.append(policy)
            return inspect_zip_bytes(body, policy)

    consumer = JobConsumer(factory, queue, finalizer, FixtureProcessor())
    dispatcher = OutboxDispatcher(factory, queue)
    for policy in (AdmissionPolicyV1(), AdmissionPolicyV2()):
        service = SubmissionService(factory, store, policy=policy)
        sub = await service.reserve(
            ctx,
            UploadReservationV1(
                repository_id=reservation.repository_id,
                filename="fixture.zip",
                declared_size=len(body),
            ),
            uuid4().hex,
        )
        uploaded = await service.write_content(ctx, sub.id, chunks(body))
        assert await dispatcher.dispatch_once() >= 1
        for delivery in await queue.receive(2, wait_seconds=0):
            await consumer.handle(delivery)
        # Admission publishes the snapshot before its inventory child is consumed.
        record = await JobService(factory).get(ctx, uploaded.job_id)
        assert record.state == "succeeded"
        snapshots.append(record.snapshot_id)
        async with factory() as session:
            located = await locate_source(
                session, ctx.org_id, reservation.repository_id, record.snapshot_id
            )
            assert located.policy == policy
            persisted = (
                (
                    await session.execute(
                        select(source_files.c.id).where(
                            source_files.c.org_id == ctx.org_id,
                            source_files.c.snapshot_id == record.snapshot_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        root = tmp_path / policy.version
        root.mkdir()
        source = await stage_source(store, located, AnalysisConfigV1(toolchain_hash="a" * 64), root)
        assert {f.file_id for f in source.files} == set(persisted)
        lock = next(f for f in source.files if f.path == "uv.lock")
        assert lock.eligible == isinstance(policy, AdmissionPolicyV2)
        assert [u.path for u in AdmittedSourceReader(source, root).read_selected()] == ["main.py"]
        # Drain the inventory wakeup so the next iteration starts without child work.
        await dispatcher.dispatch_once()
        for delivery in await queue.receive(2, wait_seconds=0):
            await consumer.handle(delivery)
    assert snapshots[0] != snapshots[1]
    assert policies == [AdmissionPolicyV1(), AdmissionPolicyV2()]

    service = SubmissionService(factory, store, policy=AdmissionPolicyV2())
    sub = await service.reserve(
        ctx,
        UploadReservationV1(
            repository_id=reservation.repository_id, filename="retry.zip", declared_size=len(body)
        ),
        uuid4().hex,
    )
    uploaded = await service.write_content(ctx, sub.id, chunks(body))
    await service.cancel(ctx, sub.id)
    retried = await RetryService(factory, store).retry(ctx, uploaded.job_id, uuid4().hex)
    async with factory() as session:
        table = PHASE2_TABLES["source_submissions"]
        assert await session.scalar(
            select(table.c.policy_hash).where(
                table.c.org_id == ctx.org_id, table.c.id == retried.submission_id
            )
        ) == (AdmissionPolicyV2().digest())


async def test_github_v2_preserves_selected_acquisition_and_root_policy(factory, tmp_path):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from app.contracts.jobs import AttemptLeaseV2
    from app.models.tables import repositories
    from app.services.github_acquisition import GitHubAcquisitionService
    from sqlalchemy import insert, update
    from tests.phase2.integration.test_github_acquisition import FakeGitHub

    _, ctx, reservation, _, _, store = await submission_scenario(factory, tmp_path)
    now = datetime.now(UTC)
    async with factory.begin() as session:
        await session.execute(
            update(repositories)
            .where(
                repositories.c.id == reservation.repository_id, repositories.c.org_id == ctx.org_id
            )
            .values(source_kind="github")
        )
        await session.execute(
            insert(PHASE2_TABLES["github_repository_bindings"]).values(
                org_id=ctx.org_id,
                repository_id=reservation.repository_id,
                github_repository_id=11,
                installation_id=22,
                account_id=123,
                state="active",
                last_checked_at=now,
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["github_user_grants"]).values(
                principal_id=ctx.principal_id,
                token_cipher=b"fixture",
                key_id="fixture",
                expires_at=now + timedelta(hours=1),
                last_verified_at=now,
            )
        )
    body = archive(
        [
            ("provider-root/main.py", b"def main(): pass\n"),
            ("provider-root/uv.lock", b"version = 1\n"),
        ]
    )
    provider = FakeGitHub(body)
    policy = AdmissionPolicyV2(github_strip_root=True)
    acquisition = GitHubAcquisitionService(factory, store, provider, policy=policy)
    sub = await acquisition.request(ctx, reservation.repository_id, "main", uuid4().hex)
    lease = await JobService(factory).claim(ctx.org_id, sub.job_id, 1, "fixture-worker")
    assert isinstance(lease, AttemptLeaseV2)
    selected = await acquisition.acquire(lease)
    assert selected.sha256 == hashlib.sha256(body).hexdigest()
    provider.body = b"changed provider response must not replace verified bytes"
    assert await acquisition.acquire(lease) == selected
    assert provider.resolves == 1
    manifest = inspect_zip_bytes(body, policy, source_revision="a" * 40, github_repository_id=11)
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(lease, manifest)
    published = await finalizer.finish_admission(lease, prepared)
    async with factory() as session:
        located = await locate_source(
            session, ctx.org_id, reservation.repository_id, published.snapshot_id
        )
    assert located.policy == policy
    assert {f.path for f in located.files} == {"main.py", "uv.lock"}
    root = tmp_path / "github-source"
    root.mkdir()
    source = await stage_source(store, located, AnalysisConfigV1(toolchain_hash="a" * 64), root)
    assert [u.path for u in AdmittedSourceReader(source, root).read_selected()] == ["main.py"]
    assert source.policy.strip_single_root
