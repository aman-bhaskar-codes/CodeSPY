"""Locate immutable admitted inputs; storage I/O happens after DB locks close."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ingestion import (
    AdmissionPolicy,
    ArtifactManifestV1,
    ObjectDescriptor,
    resolve_admission_policy,
)
from app.contracts.structure import (
    AnalysisConfigV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourcePolicyV1,
)
from app.domain.errors import DuplicateConflict
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import artifacts, source_files, source_snapshots
from app.ports.object_store import ObjectStore
from app.services.job_finalization import descriptor


def source_file_query(org_id: UUID, snapshot_id: UUID) -> Select[Any]:
    entries = PHASE2_TABLES["inventory_entries"]
    return (
        select(source_files, entries.c.eligible, entries.c.exclusion_reason, entries.c.policy_hash)
        .join(
            entries,
            (entries.c.org_id == source_files.c.org_id)
            & (entries.c.snapshot_id == source_files.c.snapshot_id)
            & (entries.c.file_id == source_files.c.id),
        )
        .where(source_files.c.org_id == org_id, source_files.c.snapshot_id == snapshot_id)
        .order_by(source_files.c.path)
    )


@dataclass(frozen=True)
class LocatedSource:
    archive: ObjectDescriptor
    manifest: ObjectDescriptor
    files: tuple[SourceFileV1, ...]
    policy: AdmissionPolicy
    revision: str | None


async def locate_source(
    s: AsyncSession, org_id: UUID, repository_id: UUID, snapshot_id: UUID
) -> LocatedSource:
    objects, submissions, jobs = (
        PHASE2_TABLES[n] for n in ("object_records", "source_submissions", "jobs")
    )
    snap = (
        (
            await s.execute(
                select(source_snapshots).where(
                    source_snapshots.c.org_id == org_id,
                    source_snapshots.c.repository_id == repository_id,
                    source_snapshots.c.id == snapshot_id,
                    source_snapshots.c.state == "sealed",
                )
            )
        )
        .mappings()
        .one()
    )
    art = (
        (
            await s.execute(
                select(artifacts).where(
                    artifacts.c.org_id == org_id,
                    artifacts.c.id == snap["artifact_id"],
                )
            )
        )
        .mappings()
        .one()
    )
    admitted = (
        (
            await s.execute(
                select(
                    jobs.c.result_object_id, submissions.c.source_kind, submissions.c.policy_hash
                )
                .join(
                    submissions,
                    (submissions.c.org_id == jobs.c.org_id)
                    & (submissions.c.id == jobs.c.submission_id),
                )
                .where(
                    jobs.c.org_id == org_id,
                    jobs.c.repository_id == repository_id,
                    jobs.c.snapshot_id == snapshot_id,
                    jobs.c.operation == "admit_source",
                    jobs.c.state == "succeeded",
                    submissions.c.state == "admitted",
                    submissions.c.snapshot_id == snapshot_id,
                    submissions.c.actual_sha == art["sha256"],
                    submissions.c.policy_hash == snap["admission_config_hash"],
                    submissions.c.resolved_commit.is_not_distinct_from(snap["source_revision"]),
                )
                .order_by(jobs.c.id)
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if admitted is None:
        raise DuplicateConflict("verified admission unavailable")
    try:
        policy = resolve_admission_policy(
            admitted["policy_hash"], github_strip_root=admitted["source_kind"] == "github_archive"
        )
    except ValueError:
        raise DuplicateConflict("unsupported admission policy") from None
    if (
        policy.digest() != admitted["policy_hash"]
        or policy.digest() != snap["admission_config_hash"]
    ):
        raise DuplicateConflict("unsupported admission policy")
    rows = (
        (
            await s.execute(
                select(objects).where(
                    objects.c.org_id == org_id,
                    objects.c.repository_id == repository_id,
                    objects.c.state == "available",
                    ((objects.c.key == art["storage_key"]) & (objects.c.kind == "source"))
                    | (
                        (objects.c.id == admitted["result_object_id"])
                        & (objects.c.kind == "manifest")
                    ),
                )
            )
        )
        .mappings()
        .all()
    )
    bykey = {r["key"]: dict(r) for r in rows}
    archive_row = bykey.get(art["storage_key"])
    manifest_row = next((dict(r) for r in rows if r["id"] == admitted["result_object_id"]), None)
    if (
        not archive_row
        or not manifest_row
        or archive_row["sha256"] != art["sha256"]
        or archive_row["size_bytes"] != art["size_bytes"]
    ):
        raise DuplicateConflict("available admitted objects differ")
    files = (await s.execute(source_file_query(org_id, snapshot_id))).mappings().all()
    file_count = await s.scalar(
        select(func.count())
        .select_from(source_files)
        .where(
            source_files.c.org_id == org_id,
            source_files.c.snapshot_id == snapshot_id,
        )
    )
    if len(files) != file_count or any(r["policy_hash"] != policy.digest() for r in files):
        raise DuplicateConflict("sealed inventory mapping differs")
    return LocatedSource(
        descriptor(archive_row),
        descriptor(manifest_row),
        tuple(
            SourceFileV1(
                file_id=r["id"],
                path=r["path"],
                sha256=r["sha256"],
                size_bytes=r["size_bytes"],
                language=r["language"],
                eligible=r["eligible"],
                exclusion_reason=r["exclusion_reason"],
            )
            for r in files
        ),
        policy,
        snap["source_revision"],
    )


async def stage_source(
    store: ObjectStore, located: LocatedSource, config: AnalysisConfigV1, root: Path
) -> SourceInputV1:
    for obj, filename, cap in (
        (located.archive, "archive.zip", 104857600),
        (located.manifest, "manifest.json", 67108864),
    ):
        if obj.size_bytes > cap:
            raise DuplicateConflict("source object exceeds bound")
        size = 0
        with (root / filename).open("xb") as stream:
            async for block in store.open_verified(obj):
                size += len(block)
                if size > cap or size > obj.size_bytes:
                    raise DuplicateConflict("source stream exceeds bound")
                stream.write(block)
        if size != obj.size_bytes:
            raise DuplicateConflict("source stream incomplete")
    manifest = ArtifactManifestV1.model_validate_json((root / "manifest.json").read_bytes())
    if (
        manifest.archive_sha256 != located.archive.sha256
        or manifest.archive_size != located.archive.size_bytes
        or manifest.policy_hash != located.policy.digest()
        or manifest.source_revision != located.revision
    ):
        raise DuplicateConflict("admitted manifest identity differs")
    return SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=located.archive.sha256, size_bytes=located.archive.size_bytes
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=located.manifest.sha256,
            size_bytes=located.manifest.size_bytes,
        ),
        files=located.files,
        policy=SourcePolicyV1(
            strip_single_root=located.policy.github_strip_root,
            policy_hash=located.policy.digest(),
            admission=located.policy,
        ),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=config.toolchain_hash,
        request_nonce=uuid4().hex,
    )
