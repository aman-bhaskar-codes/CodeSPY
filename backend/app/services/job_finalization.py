"""Fenced atomic publication with one forward deadline and durable COMMIT recovery."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.evidence import EvidenceEnvelopeV1
from app.contracts.ingestion import ArtifactManifestV1, ObjectDescriptor
from app.contracts.jobs import AttemptLeaseV2, EnqueueJobV2, JobRecordV2
from app.contracts.persistence import (
    ArtifactCreate,
    CoverageV1,
    ResultBatchV1,
    SnapshotCreate,
    SourceFileCreate,
)
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.errors import DuplicateConflict, FinalizationOutcomeUnknown, StaleAttempt
from app.domain.scope import Contract
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import analysis_runs, source_snapshots
from app.ports.object_store import ObjectStore
from app.repositories.event_repository import append_event
from app.repositories.job_repository import attempts, job_record, jobs, load_job, worker_context
from app.security.authorization import lock_scope
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate
from app.services.jobs import JobService
from app.services.publication import PublicationExecutor

objects = PHASE2_TABLES["object_records"]
submissions = PHASE2_TABLES["source_submissions"]
entries = PHASE2_TABLES["inventory_entries"]


class PreparedAdmissionV1(Contract):
    manifest: ArtifactManifestV1
    source_object_id: UUID
    manifest_object_id: UUID
    source: ObjectDescriptor
    manifest_object: ObjectDescriptor


def descriptor(row: dict[str, Any]) -> ObjectDescriptor:
    return ObjectDescriptor(
        key=row["key"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        version_id=row["version_id"],
    )


class JobFinalizer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        *,
        budget_seconds: float = 60,
        safety_seconds: float = 5,
        cleanup_seconds: float = 5,
    ):
        if not 0 < budget_seconds <= 60 or not 0 < cleanup_seconds <= safety_seconds <= 5:
            raise ValueError("invalid finalization profile")
        self.factory = factory
        self.store = store
        self.jobs = JobService(factory)
        self.budget_seconds = budget_seconds
        self.safety_seconds = safety_seconds
        self.cleanup_seconds = cleanup_seconds

    async def _verify(self, obj: ObjectDescriptor) -> None:
        digest = hashlib.sha256()
        size = 0
        async for block in self.store.open_verified(obj):
            size += len(block)
            if size > obj.size_bytes:
                raise DuplicateConflict("object exceeds descriptor")
            digest.update(block)
        if size != obj.size_bytes or digest.hexdigest() != obj.sha256:
            raise DuplicateConflict("object descriptor mismatch")

    async def prepare_admission(
        self, lease: AttemptLeaseV2, manifest: ArtifactManifestV1
    ) -> PreparedAdmissionV1:
        manifest = ArtifactManifestV1.model_validate(manifest.model_dump())
        encoded = canonical_bytes(manifest.model_dump())
        if len(encoded) > 67108864:
            raise ValueError("manifest exceeds bound")
        manifest_hash = hashlib.sha256(encoded).hexdigest()
        async with transaction(self.factory) as s:
            job, _ctx = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, job)
            if job["operation"] != "admit_source" or job["config_hash"] != manifest.policy_hash:
                raise DuplicateConflict("admission policy differs")
            sub = (
                (
                    await s.execute(
                        select(submissions)
                        .where(
                            submissions.c.org_id == lease.org_id,
                            submissions.c.id == job["submission_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                sub["actual_sha"] != manifest.archive_sha256
                or sub["actual_size"] != manifest.archive_size
                or sub["resolved_commit"] != manifest.source_revision
            ):
                raise DuplicateConflict("selected acquisition differs")
            source_row = (
                (
                    await s.execute(
                        select(objects).where(
                            objects.c.org_id == lease.org_id,
                            objects.c.id == sub["input_object_id"],
                            objects.c.state.in_(["verified", "available"]),
                        )
                    )
                )
                .mappings()
                .one()
            )
            selected = descriptor(dict(source_row))
            prefix = f"source/{lease.org_id}/{job['repository_id']}/{manifest.archive_sha256}"
            source = ObjectDescriptor(
                key=prefix + "/source.zip",
                sha256=manifest.archive_sha256,
                size_bytes=manifest.archive_size,
            )
            manifest_object = ObjectDescriptor(
                key=prefix + f"/{manifest_hash}/manifest.json",
                sha256=manifest_hash,
                size_bytes=len(encoded),
            )
            ids = []
            for obj, kind in [(source, "source"), (manifest_object, "manifest")]:
                old = (
                    (
                        await s.execute(
                            select(objects)
                            .where(
                                objects.c.org_id == lease.org_id,
                                objects.c.repository_id == job["repository_id"],
                                objects.c.key == obj.key,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if old:
                    if (
                        old["state"] == "deleting"
                        or old["sha256"] != obj.sha256
                        or old["size_bytes"] != obj.size_bytes
                    ):
                        raise DuplicateConflict("canonical object unavailable or conflicting")
                    if old["state"] == "deleted":
                        # The immutable byte/key identity survives cleanup. A new
                        # owner/fence creates a fresh verification intent atomically.
                        await s.execute(
                            update(objects)
                            .where(
                                objects.c.org_id == lease.org_id,
                                objects.c.id == old["id"],
                                objects.c.state == "deleted",
                            )
                            .values(
                                state="reserved",
                                owner_job_id=lease.job_id,
                                owner_fence=lease.fence,
                                version_id=None,
                                expires_at=func.clock_timestamp() + text("interval '1 hour'"),
                            )
                        )
                        tasks = PHASE2_TABLES["cleanup_tasks"]
                        await s.execute(
                            update(tasks)
                            .where(tasks.c.org_id == lease.org_id, tasks.c.object_id == old["id"])
                            .values(
                                state="done",
                                claim_token=None,
                                claim_expires_at=None,
                                attempt_count=0,
                                last_error_code=None,
                            )
                        )
                    ids.append(old["id"])
                else:
                    id = uuid4()
                    ids.append(id)
                    await s.execute(
                        insert(objects).values(
                            org_id=lease.org_id,
                            id=id,
                            repository_id=job["repository_id"],
                            kind=kind,
                            state="reserved",
                            key=obj.key,
                            sha256=obj.sha256,
                            size_bytes=obj.size_bytes,
                            owner_job_id=lease.job_id,
                            owner_fence=lease.fence,
                            expires_at=func.clock_timestamp() + text("interval '1 hour'"),
                        )
                    )
            stage_id = uuid4()
            stage_key = (
                f"stage/{lease.org_id}/{job['repository_id']}/"
                f"{lease.job_id}/{lease.fence}/{stage_id}"
            )
            await s.execute(
                insert(objects).values(
                    org_id=lease.org_id,
                    id=stage_id,
                    repository_id=job["repository_id"],
                    kind="stage",
                    state="reserved",
                    expires_at=func.clock_timestamp() + text("interval '1 hour'"),
                    key=stage_key,
                    sha256=manifest_hash,
                    size_bytes=len(encoded),
                    owner_job_id=lease.job_id,
                    owner_fence=lease.fence,
                )
            )
        await self._verify(selected)
        source = await self.store.put_if_absent(source.key, selected)

        async def chunks() -> AsyncIterator[bytes]:
            yield encoded

        stage = await self.store.put_stage(stage_key, chunks(), 67108864)
        manifest_object = await self.store.put_if_absent(manifest_object.key, stage)
        async with transaction(self.factory) as s:
            for id, obj in [(ids[0], source), (ids[1], manifest_object), (stage_id, stage)]:
                await s.execute(
                    update(objects)
                    .where(
                        objects.c.org_id == lease.org_id,
                        objects.c.id == id,
                        objects.c.state.in_(["reserved", "writing", "verified"]),
                    )
                    .values(
                        state="verified",
                        sha256=obj.sha256,
                        size_bytes=obj.size_bytes,
                        version_id=obj.version_id,
                    )
                )
        return PreparedAdmissionV1(
            manifest=manifest,
            source_object_id=ids[0],
            manifest_object_id=ids[1],
            source=source,
            manifest_object=manifest_object,
        )

    async def _entry(
        self,
        s: AsyncSession,
        lease: AttemptLeaseV2,
        job: dict[str, Any],
        budget: FinalizationBudget,
    ) -> dict[str, Any]:
        attempt = await self.jobs._current(s, lease, job)
        now = await database_now(s)
        required = budget.remaining() + self.safety_seconds
        if (
            min(
                (attempt["lease_expires_at"] - now).total_seconds(),
                (job["deadline"] - now).total_seconds(),
            )
            < required
        ):
            raise StaleAttempt("insufficient lease or deadline for finalization")
        await worker_context(s, lease)
        return attempt

    async def _commit(self, session: AsyncSession) -> None:
        await session.commit()

    async def _execute(
        self,
        work: Callable[[AsyncSession, FinalizationBudget], Awaitable[JobRecordV2]],
        resolve: Callable[[], Awaitable[JobRecordV2]],
    ) -> JobRecordV2:
        executor = PublicationExecutor(
            self.factory,
            budget_seconds=self.budget_seconds,
            safety_seconds=self.safety_seconds,
            cleanup_seconds=self.cleanup_seconds,
            commit_hook=self._commit,
        )
        return await executor.execute(work, resolve)

    async def _durable(
        self, lease: AttemptLeaseV2, expected: str, *, inventory: bool
    ) -> JobRecordV2:
        s = self.factory()
        try:
            async with asyncio.timeout(5):
                await s.begin()
                await s.execute(text("SET LOCAL statement_timeout='2s'"))
                await s.execute(text("SET LOCAL lock_timeout='1s'"))
                job, _ = await self.jobs._locked(s, lease.org_id, lease.job_id, access=False)
                result = await self._terminal(s, lease, job, expected, inventory=inventory)
                if result is None:
                    raise FinalizationOutcomeUnknown(
                        "publication did not resolve to a durable matching outcome"
                    )
                await s.rollback()
                return result
        except (StaleAttempt, DuplicateConflict):
            raise
        except Exception:
            raise FinalizationOutcomeUnknown("publication outcome requires recovery") from None
        finally:
            await rollback_or_invalidate(s, self.cleanup_seconds)

    async def _terminal(
        self,
        s: AsyncSession,
        lease: AttemptLeaseV2,
        job: dict[str, Any],
        expected: str,
        *,
        inventory: bool,
    ) -> JobRecordV2 | None:
        if job["state"] not in {"succeeded", "partial", "failed", "cancelled"}:
            return None
        if (
            job["state"] not in {"succeeded", "partial"}
            or job["fence"] != lease.fence
            or job["current_attempt_id"] != lease.attempt_id
        ):
            raise StaleAttempt("terminal outcome belongs to another result")
        if inventory:
            digest = await s.scalar(
                select(analysis_runs.c.result_hash).where(
                    analysis_runs.c.org_id == lease.org_id,
                    analysis_runs.c.snapshot_id == job["snapshot_id"],
                    analysis_runs.c.id == job["result_run_id"],
                )
            )
        else:
            digest = await s.scalar(
                select(objects.c.sha256).where(
                    objects.c.org_id == lease.org_id, objects.c.id == job["result_object_id"]
                )
            )
        if digest != expected:
            raise DuplicateConflict("durable publication digest differs")
        return job_record(job)

    async def finish_admission(
        self,
        lease: AttemptLeaseV2,
        prepared: PreparedAdmissionV1,
        *,
        before_lock: Callable[[], Awaitable[None]] | None = None,
    ) -> JobRecordV2:
        prepared = PreparedAdmissionV1.model_validate(prepared.model_dump())
        manifest = prepared.manifest
        expected = hashlib.sha256(canonical_bytes(manifest.model_dump())).hexdigest()
        if (
            prepared.manifest_object.sha256 != expected
            or prepared.source.sha256 != manifest.archive_sha256
            or prepared.source.size_bytes != manifest.archive_size
        ):
            raise DuplicateConflict("prepared object identity differs")
        await self._verify(prepared.source)
        await self._verify(prepared.manifest_object)
        files = [
            SourceFileCreate(
                id=uuid4(),
                path=e.path,
                sha256=e.sha256,
                size_bytes=e.size_bytes,
                language=e.language_hint,
            )
            for e in manifest.entries
        ]

        async def work(s: AsyncSession, budget: FinalizationBudget) -> JobRecordV2:
            hint = await load_job(s, lease.org_id, lease.job_id)
            ctx = await lock_scope(s, lease.org_id, hint["principal_id"], hint["repository_id"])
            assert ctx is not None
            identity = semantic_digest(
                {
                    "org_id": lease.org_id,
                    "repository_id": hint["repository_id"],
                    "artifact_sha256": manifest.archive_sha256,
                    "source_revision": manifest.source_revision,
                    "admission_config_hash": manifest.policy_hash,
                }
            )
            await s.execute(
                select(source_snapshots.c.id)
                .where(
                    source_snapshots.c.org_id == lease.org_id,
                    source_snapshots.c.repository_id == hint["repository_id"],
                    source_snapshots.c.identity_hash == identity,
                )
                .with_for_update()
            )
            job = await load_job(s, lease.org_id, lease.job_id, lock=True)
            terminal = await self._terminal(s, lease, job, expected, inventory=False)
            if terminal:
                return terminal
            if job["operation"] != "admit_source" or job["config_hash"] != manifest.policy_hash:
                raise DuplicateConflict("admission job input differs")
            await self._entry(s, lease, job, budget)
            sub = (
                (
                    await s.execute(
                        select(submissions)
                        .where(
                            submissions.c.org_id == lease.org_id,
                            submissions.c.id == job["submission_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                sub["state"] != "admitting"
                or sub["actual_sha"] != manifest.archive_sha256
                or sub["actual_size"] != manifest.archive_size
                or sub["policy_hash"] != manifest.policy_hash
                or sub["resolved_commit"] != manifest.source_revision
            ):
                raise DuplicateConflict("selected source input differs")
            objrows = (
                (
                    await s.execute(
                        select(objects)
                        .where(
                            objects.c.org_id == lease.org_id,
                            objects.c.repository_id == job["repository_id"],
                            objects.c.id.in_(
                                [prepared.source_object_id, prepared.manifest_object_id]
                            ),
                        )
                        .order_by(objects.c.id)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            byid = {row["id"]: dict(row) for row in objrows}
            for id, obj in [
                (prepared.source_object_id, prepared.source),
                (prepared.manifest_object_id, prepared.manifest_object),
            ]:
                row = byid.get(id)
                if (
                    row is None
                    or row["state"] not in {"verified", "available"}
                    or descriptor(row) != obj
                ):
                    raise DuplicateConflict("object ledger differs")
            snapshot = await self.jobs.persistence._create_snapshot_in_session(
                s,
                ctx,
                SnapshotCreate(
                    repository_id=job["repository_id"],
                    artifact=ArtifactCreate(
                        sha256=prepared.source.sha256,
                        size_bytes=prepared.source.size_bytes,
                        storage_key=prepared.source.key,
                    ),
                    source_revision=manifest.source_revision,
                    admission_config_hash=manifest.policy_hash,
                ),
                files,
            )
            stored = await self.jobs.persistence._resolve_snapshot_files_in_session(
                s, ctx, snapshot.id, files
            )
            bypath = {e.path: e for e in manifest.entries}
            expected_entries = {
                (f.id, bypath[f.path].eligible, bypath[f.path].reason, manifest.policy_hash)
                for f in stored
            }
            existing = {
                (r.file_id, r.eligible, r.exclusion_reason, r.policy_hash)
                for r in (
                    await s.execute(
                        select(
                            entries.c.file_id,
                            entries.c.eligible,
                            entries.c.exclusion_reason,
                            entries.c.policy_hash,
                        ).where(
                            entries.c.org_id == lease.org_id, entries.c.snapshot_id == snapshot.id
                        )
                    )
                ).all()
            }
            if existing != expected_entries:
                attached = await s.scalar(
                    select(submissions.c.id)
                    .where(
                        submissions.c.org_id == lease.org_id,
                        submissions.c.snapshot_id == snapshot.id,
                        submissions.c.state == "admitted",
                    )
                    .limit(1)
                )
                if existing or attached:
                    raise DuplicateConflict("sealed inventory associations differ")
                if expected_entries:
                    rows = [
                        {
                            "org_id": lease.org_id,
                            "snapshot_id": snapshot.id,
                            "file_id": identity,
                            "eligible": eligible,
                            "exclusion_reason": reason,
                            "policy_hash": policy,
                        }
                        for identity, eligible, reason, policy in expected_entries
                    ]
                    # One bounded VALUES statement per batch avoids 50k driver
                    # round trips while every row still runs the fencing guards.
                    for offset in range(0, len(rows), 500):
                        await s.execute(insert(entries).values(rows[offset : offset + 500]))
            await s.execute(
                update(objects)
                .where(
                    objects.c.org_id == lease.org_id,
                    objects.c.id.in_([prepared.source_object_id, prepared.manifest_object_id]),
                )
                .values(state="available", expires_at=None)
            )
            await self.jobs.release_reservation(s, job, "admitted", snapshot_id=snapshot.id)
            job = dict(
                (
                    await s.execute(
                        update(jobs)
                        .where(jobs.c.org_id == lease.org_id, jobs.c.id == lease.job_id)
                        .values(
                            state="succeeded",
                            snapshot_id=snapshot.id,
                            result_object_id=prepared.manifest_object_id,
                            finished_at=func.clock_timestamp(),
                        )
                        .returning(jobs)
                    )
                )
                .mappings()
                .one()
            )
            await self.jobs._enqueue(
                s,
                ctx,
                EnqueueJobV2(
                    repository_id=job["repository_id"],
                    submission_id=job["submission_id"],
                    snapshot_id=snapshot.id,
                    operation="inventory_snapshot",
                    request_key=f"child:{lease.job_id}",
                    parent_job_id=lease.job_id,
                    config_hash=manifest.policy_hash,
                ),
            )
            await s.execute(
                update(attempts)
                .where(
                    attempts.c.org_id == lease.org_id,
                    attempts.c.job_id == lease.job_id,
                    attempts.c.id == lease.attempt_id,
                )
                .values(state="succeeded", finished_at=func.clock_timestamp())
            )
            await append_event(s, job, "admitted", {"file_count": len(files)})
            return job_record(job)

        if before_lock is not None:
            await before_lock()
        return await self._execute(work, lambda: self._durable(lease, expected, inventory=False))

    async def inventory_batch(self, lease: AttemptLeaseV2) -> ResultBatchV1:
        if lease.run_ref is None:
            raise ValueError("inventory requires a run")
        async with transaction(self.factory) as s:
            job, _ctx = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, job)
            parent = await load_job(s, lease.org_id, job["parent_job_id"])
            row = (
                (
                    await s.execute(
                        select(objects).where(
                            objects.c.org_id == lease.org_id,
                            objects.c.id == parent["result_object_id"],
                            objects.c.state == "available",
                        )
                    )
                )
                .mappings()
                .one()
            )
            obj = descriptor(dict(row))
        raw = bytearray()
        async for block in self.store.open_verified(obj):
            raw.extend(block)
            if len(raw) > 67108864:
                raise DuplicateConflict("manifest exceeds bound")
        manifest = ArtifactManifestV1.model_validate_json(raw)
        if manifest.policy_hash != job["config_hash"]:
            raise DuplicateConflict("inventory policy differs")
        count = len(manifest.entries)
        eligible = sum(e.eligible for e in manifest.entries)
        limitations = [
            "Byte inventory only; no parsing, semantic analysis or security conclusions."
        ]
        evidence = EvidenceEnvelopeV1(
            scope=lease.run_ref,
            id=uuid4(),
            provider_name="codespy-source-inventory",
            provider_version="1",
            operation="inventory_snapshot",
            observation_mode="static",
            basis="observed",
            coverage_status="complete",
            payload={
                "file_count": count,
                "eligible_count": eligible,
                "excluded_count": count - eligible,
                "empty_repository": count == 0,
                "manifest_sha256": obj.sha256,
            },
            limitations=limitations,
            captured_at=datetime.now(UTC),
        )
        return ResultBatchV1(
            scope=lease.run_ref,
            disposition="completed",
            coverage=CoverageV1(
                status="complete",
                scope_description=(
                    "Byte inventory of all admitted regular files; no semantic analysis"
                ),
                admitted_count=count,
                processed_count=count,
                omitted_count=0,
                truncated=False,
            ),
            evidence=[evidence],
            limitations=limitations,
        )

    async def finish_inventory(self, lease: AttemptLeaseV2, batch: ResultBatchV1) -> JobRecordV2:
        batch = ResultBatchV1.model_validate(batch.model_dump())
        if (
            lease.run_ref is None
            or batch.scope != lease.run_ref
            or batch.nodes
            or batch.edges
            or batch.derivations
        ):
            raise DuplicateConflict("inventory result scope or graph differs")
        ref = lease.run_ref
        expected = batch.semantic_hash()

        async def work(s: AsyncSession, budget: FinalizationBudget) -> JobRecordV2:
            job, ctx = await self.jobs._locked(s, lease.org_id, lease.job_id)
            assert ctx is not None
            terminal = await self._terminal(s, lease, job, expected, inventory=True)
            if terminal:
                return terminal
            if job["operation"] != "inventory_snapshot":
                raise DuplicateConflict("operation differs")
            await self._entry(s, lease, job, budget)
            version = await s.scalar(
                select(analysis_runs.c.write_version)
                .where(
                    analysis_runs.c.org_id == lease.org_id,
                    analysis_runs.c.snapshot_id == ref.snapshot_id,
                    analysis_runs.c.id == ref.run_id,
                )
                .with_for_update()
            )
            if version is None:
                raise StaleAttempt("run unavailable")
            result = await self.jobs.persistence._persist_result_in_session(
                s, ctx, ref, batch, version
            )
            job = dict(
                (
                    await s.execute(
                        update(jobs)
                        .where(jobs.c.org_id == lease.org_id, jobs.c.id == lease.job_id)
                        .values(
                            state="succeeded" if result.status == "completed" else "partial",
                            result_run_id=result.id,
                            finished_at=func.clock_timestamp(),
                        )
                        .returning(jobs)
                    )
                )
                .mappings()
                .one()
            )
            await s.execute(
                update(attempts)
                .where(
                    attempts.c.org_id == lease.org_id,
                    attempts.c.job_id == lease.job_id,
                    attempts.c.id == lease.attempt_id,
                )
                .values(state="succeeded", finished_at=func.clock_timestamp())
            )
            await append_event(
                s,
                job,
                "inventory_completed" if result.status == "completed" else "inventory_partial",
            )
            return job_record(job)

        return await self._execute(work, lambda: self._durable(lease, expected, inventory=True))
