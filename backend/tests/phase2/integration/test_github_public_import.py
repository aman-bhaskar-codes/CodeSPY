import hashlib
import io
import zipfile
from uuid import uuid4

import pytest
from app.adapters.storage.local import LocalObjectStore
from app.contracts.ingestion import AdmissionPolicyV1
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories, source_files
from app.repositories.identity_repository import IdentityRepository
from app.services.github_acquisition import GitHubAcquisitionService
from app.services.jobs import JobService
from sqlalchemy import func, select, update

SHA = "b" * 40


class FakePublic:
    def __init__(self, body: bytes):
        self.body, self.resolves, self.names = body, 0, []

    async def resolve(self, full_name, ref):
        self.resolves += 1
        self.names.append((full_name, ref))
        return SHA

    async def archive(self, full_name, commit):
        assert commit == SHA
        yield self.body


def archive() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("root/a.py", "print(1)")
    return out.getvalue()


async def owner(factory):
    async with transaction(factory) as s:
        ctx = await IdentityRepository(s).create_fixture("fixture-" + uuid4().hex[:12])
    return ctx


def service(factory, tmp_path, provider):
    store = LocalObjectStore(tmp_path / "objects")
    return GitHubAcquisitionService(factory, store, None, public=provider)


async def count(factory, table, *where):
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(table).where(*where))


async def test_import_creates_repository_submission_and_job_idempotently(factory, tmp_path):
    ctx = await owner(factory)
    svc = service(factory, tmp_path, FakePublic(archive()))
    first = await svc.import_url(
        ctx.principal_id, "https://github.com/Owner/Repo.git", "main", "key-1"
    )
    again = await svc.import_url(
        ctx.principal_id, "https://github.com/Owner/Repo.git", "main", "key-1"
    )
    assert first == again and first.source_kind == "github_public_archive"
    assert first.state == "admission_queued" and first.job_id
    assert (
        await count(factory, repositories, repositories.c.repository_key == "github.com/Owner/Repo")
        == 1
    )
    limits = PHASE2_TABLES["org_work_limits"]
    async with factory() as s:
        reserved = await s.scalar(
            select(limits.c.reserved_bytes).where(limits.c.org_id == ctx.org_id)
        )
    assert reserved == AdmissionPolicyV1(github_strip_root=True).max_archive_bytes  # reserved once
    with pytest.raises(DuplicateConflict):
        await svc.import_url(ctx.principal_id, "https://github.com/Owner/Repo", "dev", "key-1")


async def test_second_import_of_same_url_reuses_repository(factory, tmp_path):
    ctx = await owner(factory)
    svc = service(factory, tmp_path, FakePublic(archive()))
    a = await svc.import_url(ctx.principal_id, "https://github.com/o/r", None, "k-a")
    b = await svc.import_url(ctx.principal_id, "https://github.com/o/r/tree/dev", None, "k-b")
    assert a.repository_id == b.repository_id and a.id != b.id
    assert (
        await count(factory, repositories, repositories.c.repository_key == "github.com/o/r") == 1
    )


async def test_acquire_pins_commit_once_and_freezes_selected_bytes(factory, tmp_path):
    ctx = await owner(factory)
    provider = FakePublic(archive())
    svc = service(factory, tmp_path, provider)
    sub = await svc.import_url(ctx.principal_id, "https://github.com/o/r", None, "k")
    lease = await JobService(factory).claim(ctx.org_id, sub.job_id, 1, "fixture-worker")
    selected = await svc.acquire(lease)
    assert selected.sha256 == hashlib.sha256(provider.body).hexdigest()
    provider.body = b"changed and invalid"
    assert await svc.acquire(lease) == selected
    assert provider.resolves == 1 and provider.names == [("o/r", None)]
    submissions = PHASE2_TABLES["source_submissions"]
    async with factory() as s:
        row = (
            (await s.execute(select(submissions).where(submissions.c.id == sub.id)))
            .mappings()
            .one()
        )
    assert row["resolved_commit"] == SHA and row["requested_ref"] is None
    assert row["source_kind"] == "github_public_archive"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r",
        "https://user@github.com/o/r",
        "https://evil.test/github.com/o/r",
        "https://github.com/o",
    ],
)
async def test_invalid_url_creates_nothing(factory, tmp_path, url):
    ctx = await owner(factory)
    svc = service(factory, tmp_path, FakePublic(archive()))
    with pytest.raises(ValueError, match="invalid GitHub URL"):
        await svc.import_url(ctx.principal_id, url, None, "k")
    assert await count(factory, repositories, repositories.c.org_id == ctx.org_id) == 0


async def test_only_owner_or_admin_may_import(factory, tmp_path):
    ctx = await owner(factory)
    async with transaction(factory) as s:
        await s.execute(
            update(memberships).where(memberships.c.org_id == ctx.org_id).values(role="member")
        )
    svc = service(factory, tmp_path, FakePublic(archive()))
    with pytest.raises(ScopeViolation):
        await svc.import_url(ctx.principal_id, "https://github.com/o/r", None, "k")
    assert await count(factory, repositories, repositories.c.org_id == ctx.org_id) == 0


async def test_each_tenant_gets_its_own_repository_row(factory, tmp_path):
    first, second = await owner(factory), await owner(factory)
    svc = service(factory, tmp_path, FakePublic(archive()))
    a = await svc.import_url(first.principal_id, "https://github.com/o/r", None, "k")
    b = await svc.import_url(second.principal_id, "https://github.com/o/r", None, "k")
    assert a.org_id != b.org_id and a.repository_id != b.repository_id


async def test_service_without_public_provider_is_unavailable(factory, tmp_path):
    ctx = await owner(factory)
    svc = GitHubAcquisitionService(factory, LocalObjectStore(tmp_path / "o"), None)
    with pytest.raises(RuntimeError, match="public GitHub provider unavailable"):
        await svc.import_url(ctx.principal_id, "https://github.com/o/r", None, "k")


async def test_migration_allows_kind_and_refuses_downgrade_with_history(
    factory, db_values, tmp_path
):
    from scripts import phase2_db as harness

    ctx = await owner(factory)
    await service(factory, tmp_path, FakePublic(archive())).import_url(
        ctx.principal_id, "https://github.com/o/r", None, "k"
    )
    name = db_values["CODESPY_TEST_DATABASE"]
    with pytest.raises(Exception, match="public GitHub submissions exist"):
        harness.migrate(db_values, name, "0041_native_vector_embeddings", downgrade=True)


async def test_public_import_passes_linux_admission_and_inventory(factory, tmp_path):
    import os

    from app.adapters.queue.memory import MemoryQueue
    from app.admission.process import DockerAdmissionRunner
    from app.services.job_finalization import JobFinalizer
    from app.workers.consumer import JobConsumer
    from app.workers.dispatcher import OutboxDispatcher
    from app.workers.operations.admit_source import StoredAdmissionProcessor

    image = os.environ.get("CODESPY_ADMISSION_IMAGE")
    if not image:
        pytest.skip("owned admission image required")
    ctx = await owner(factory)
    store = LocalObjectStore(tmp_path / "objects")
    svc = GitHubAcquisitionService(factory, store, None, public=FakePublic(archive()))
    sub = await svc.import_url(ctx.principal_id, "https://github.com/o/r", None, "k")
    queue = MemoryQueue()
    consumer = JobConsumer(
        factory,
        queue,
        JobFinalizer(factory, store),
        StoredAdmissionProcessor(
            factory, store, DockerAdmissionRunner(image, tmp_path / "runner"), svc
        ),
    )
    dispatcher = OutboxDispatcher(factory, queue)
    for _ in range(2):
        assert await dispatcher.dispatch_once() == 1
        await consumer.handle((await queue.receive(1, wait_seconds=0))[0])
    jobs, submissions = PHASE2_TABLES["jobs"], PHASE2_TABLES["source_submissions"]
    async with factory() as s:
        rows = (await s.execute(select(jobs).where(jobs.c.org_id == ctx.org_id))).mappings().all()
        assert len(rows) == 2 and {row["state"] for row in rows} == {"succeeded"}
        done = (
            (await s.execute(select(submissions).where(submissions.c.id == sub.id)))
            .mappings()
            .one()
        )
    assert done["state"] == "admitted" and done["snapshot_id"] is not None
    assert done["resolved_commit"] == SHA
    files = source_files
    async with factory() as s:
        paths = set(await s.scalars(select(files.c.path).where(files.c.org_id == ctx.org_id)))
    assert paths == {"a.py"}  # the provider root directory is stripped
