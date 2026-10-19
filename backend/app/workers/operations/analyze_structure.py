"""Execute structural analysis pipeline inside worker and trigger fenced publication."""

import hashlib
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from app.analyzers.pipeline import analyze
from app.contracts.ingestion import AdmissionPolicyV1, ArtifactManifestV1, ObjectDescriptor
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import (
    AnalysisConfigV1,
    ArchitectureReportV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourcePolicyV1,
)
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.models.phase3_tables import analysis_specs
from app.models.tables import artifacts, source_files, source_snapshots
from app.ports.object_store import ObjectStore
from app.services.job_finalization import descriptor
from app.services.jobs import JobService
from app.services.structural_finalization import StructuralFinalizer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class StructuralAnalysisOperation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        finalizer: StructuralFinalizer,
    ) -> None:
        self.factory = factory
        self.store = store
        self.finalizer = finalizer
        self.jobs = JobService(factory)

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> ArchitectureReportV1:
        if lease.run_ref is None:
            raise ValueError("analysis requires run_ref")

        async with transaction(self.factory) as s:
            job, _ = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, job)

            if job["operation"] != "analyze_structure":
                raise DuplicateConflict("job operation differs")

            spec_row = (
                (
                    await s.execute(
                        select(analysis_specs).where(
                            analysis_specs.c.org_id == lease.org_id,
                            analysis_specs.c.id == job["analysis_spec_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            config = AnalysisConfigV1.model_validate(spec_row["config_json"])

            # Read snapshot info
            snap = (
                (
                    await s.execute(
                        select(source_snapshots).where(
                            source_snapshots.c.org_id == lease.org_id,
                            source_snapshots.c.id == job["snapshot_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            if snap["state"] != "sealed":
                raise ScopeViolation("snapshot is not sealed")

            # Read artifact (archive.zip)
            art = (
                (
                    await s.execute(
                        select(artifacts).where(
                            artifacts.c.org_id == lease.org_id,
                            artifacts.c.id == snap["artifact_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            archive_desc = ObjectDescriptor(
                key=art["storage_key"],
                sha256=art["sha256"],
                size_bytes=art["size_bytes"],
                version_id=None,
            )

            # Read snapshot files
            entries = PHASE2_TABLES["inventory_entries"]
            file_rows = (
                (
                    await s.execute(
                        select(
                            source_files.c.id,
                            source_files.c.path,
                            source_files.c.sha256,
                            source_files.c.size_bytes,
                            entries.c.eligible,
                            entries.c.exclusion_reason,
                        )
                        .select_from(
                            source_files.join(
                                entries,
                                (entries.c.org_id == source_files.c.org_id)
                                & (entries.c.file_id == source_files.c.id),
                            )
                        )
                        .where(
                            source_files.c.org_id == lease.org_id,
                            source_files.c.repository_id == job["repository_id"],
                            entries.c.snapshot_id == job["snapshot_id"],
                        )
                        .order_by(source_files.c.path)
                    )
                )
                .mappings()
                .all()
            )

            # Read manifest object from objects table if exists
            objects = PHASE2_TABLES["object_records"]
            manifest_row = (
                (
                    await s.execute(
                        select(objects).where(
                            objects.c.org_id == lease.org_id,
                            objects.c.repository_id == job["repository_id"],
                            objects.c.sha256 == snap["manifest_hash"],
                            objects.c.state.in_(["verified", "available"]),
                        )
                    )
                )
                .mappings()
                .first()
            )

        # Download archive and manifest
        archive_raw = bytearray()
        async for chunk in self.store.open_verified(archive_desc):
            archive_raw.extend(chunk)

        manifest_raw = bytearray()
        if manifest_row is not None:
            manifest_desc = descriptor(dict(manifest_row))
            async for chunk in self.store.open_verified(manifest_desc):
                manifest_raw.extend(chunk)
        else:
            from app.admission.zip_inventory import inspect_zip_bytes

            adm_manifest = inspect_zip_bytes(bytes(archive_raw), AdmissionPolicyV1())
            manifest_raw.extend(adm_manifest.model_dump_json().encode("utf-8"))

        manifest = ArtifactManifestV1.model_validate_json(manifest_raw)

        source_files_v1 = [
            SourceFileV1(
                file_id=row["id"],
                path=row["path"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                eligible=row["eligible"],
                exclusion_reason=row["exclusion_reason"],
            )
            for row in file_rows
        ]

        source_input = SourceInputV1(
            archive=SourceDescriptorV1(
                path="archive.zip",
                sha256=archive_desc.sha256,
                size_bytes=archive_desc.size_bytes,
            ),
            manifest=SourceDescriptorV1(
                path="manifest.json",
                sha256=hashlib.sha256(manifest_raw).hexdigest(),
                size_bytes=len(manifest_raw),
            ),
            files=tuple(source_files_v1),
            policy=SourcePolicyV1(
                policy_hash=manifest.policy_hash,
                admission=AdmissionPolicyV1(),
            ),
            config=config,
            config_hash=spec_row["config_hash"],
            toolchain_hash=spec_row["toolchain_hash"],
            request_nonce=uuid4().hex,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "archive.zip").write_bytes(archive_raw)
            (tmp_path / "manifest.json").write_bytes(manifest_raw)

            draft = analyze(source_input, tmp_path)

        if enter_finalization is not None:
            await enter_finalization()

        return await self.finalizer.finish_analysis(lease, draft)
