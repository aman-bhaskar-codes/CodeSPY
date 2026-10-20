"""Load the selected durable acquisition and inventory it in the Linux sandbox."""

from app.admission.process import DockerAdmissionRunner
from app.contracts.ingestion import AdmissionPolicy, ArtifactManifestV1
from app.contracts.jobs import AttemptLeaseV2
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.object_store import ObjectStore
from app.services.github_acquisition import GitHubAcquisitionService
from app.services.job_finalization import descriptor
from app.services.jobs import JobService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class StoredAdmissionProcessor:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        runner: DockerAdmissionRunner,
        acquisition: GitHubAcquisitionService | None = None,
    ) -> None:
        self.factory = factory
        self.store = store
        self.runner = runner
        self.acquisition = acquisition
        self.jobs = JobService(factory)

    async def inspect(self, lease: AttemptLeaseV2, policy: AdmissionPolicy) -> ArtifactManifestV1:
        submissions = PHASE2_TABLES["source_submissions"]
        objects = PHASE2_TABLES["object_records"]
        bindings = PHASE2_TABLES["github_repository_bindings"]
        if policy.github_strip_root:
            if self.acquisition is None:
                raise RuntimeError("GitHub provider unavailable")
            await self.acquisition.acquire(lease)
        async with transaction(self.factory) as session:
            job, _ = await self.jobs._locked(session, lease.org_id, lease.job_id)
            await self.jobs._current(session, lease, job)
            sub = (
                (
                    await session.execute(
                        select(submissions).where(
                            submissions.c.org_id == lease.org_id,
                            submissions.c.id == job["submission_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            row = (
                (
                    await session.execute(
                        select(objects).where(
                            objects.c.org_id == lease.org_id,
                            objects.c.repository_id == job["repository_id"],
                            objects.c.id == sub["input_object_id"],
                            objects.c.state.in_(["verified", "available"]),
                        )
                    )
                )
                .mappings()
                .one()
            )
            selected = descriptor(dict(row))
            if selected.sha256 != sub["actual_sha"] or selected.size_bytes != sub["actual_size"]:
                raise DuplicateConflict("selected acquisition differs")
            github_id = None
            if sub["source_kind"] == "github_archive":
                github_id = await session.scalar(
                    select(bindings.c.github_repository_id).where(
                        bindings.c.org_id == lease.org_id,
                        bindings.c.repository_id == job["repository_id"],
                        bindings.c.state == "active",
                    )
                )
                if github_id is None or sub["resolved_commit"] is None:
                    raise DuplicateConflict("GitHub acquisition is not pinned")
            revision = sub["resolved_commit"]
        return await self.runner.inspect_stream(
            self.store.open_verified(selected),
            selected,
            policy,
            source_revision=revision,
            github_repository_id=github_id,
        )
