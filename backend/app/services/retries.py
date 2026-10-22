"""Explicit retries allocate new history; selected acquisitions are never redownloaded."""

import hashlib
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.ingestion import ObjectDescriptor, resolve_admission_policy
from app.contracts.jobs import EnqueueJobV2, EnqueueJobV3, RetryResponseV1, RetryResponseV2
from app.contracts.scan_jobs import EnqueueScanJobV4, RetryScanResponseV3
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import CapacityExceeded, DuplicateConflict, RetryRejected
from app.domain.scope import TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.object_store import ObjectStore
from app.ports.scan_execution import ScanCapabilities
from app.repositories.job_repository import jobs, load_job
from app.security.authorization import lock_scope, require_repository
from app.services.jobs import JobService

RECOVERABLE = {
    "attempts_exhausted",
    "storage_unavailable",
    "queue_unavailable",
    "provider_unavailable",
    "admission_process_failed",
    "analysis_process_failed",
    "analysis_unavailable",
    "finalization_timeout",
    "deadline_exceeded",
    "lease_expired",
    "user_requested",
    "access_revoked",
}


class RetryService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        *,
        scan_capabilities: ScanCapabilities | None = None,
    ):
        self.factory = factory
        self.store = store
        self.jobs = JobService(factory)
        self.scan_capabilities = scan_capabilities

    async def _existing(
        self, s: AsyncSession, ctx: TenantContext, old: dict[str, Any], key: str
    ) -> RetryResponseV1 | None:
        row = (
            (
                await s.execute(
                    select(jobs).where(jobs.c.org_id == ctx.org_id, jobs.c.request_key == key)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        await require_repository(s, ctx.org_id, ctx.principal_id, row["repository_id"], write=True)
        if (
            row["retry_of_job_id"] != old["id"]
            or row["operation"] != old["operation"]
            or row["repository_id"] != old["repository_id"]
            or row["config_hash"] != old["config_hash"]
        ):
            raise DuplicateConflict("retry key has different input")
        return RetryResponseV1(
            retry_job_id=row["id"],
            submission_id=row["submission_id"],
            retry_of_job_id=old["id"],
            retry_of_submission_id=old["submission_id"]
            if old["operation"] == "admit_source"
            else None,
        )

    async def retry(
        self, ctx: TenantContext, job_id: UUID, key: str
    ) -> RetryResponseV1 | RetryResponseV2 | RetryScanResponseV3:
        if not 1 <= len(key.encode()) <= 128 or key.startswith(("job:", "child:")):
            raise ValueError("invalid retry key")
        subs = PHASE2_TABLES["source_submissions"]
        objects = PHASE2_TABLES["object_records"]
        limits = PHASE2_TABLES["org_work_limits"]
        async with transaction(self.factory) as s:
            old = await load_job(s, ctx.org_id, job_id)
            await require_repository(
                s, ctx.org_id, ctx.principal_id, old["repository_id"], write=True
            )
            if old["operation"] == "scan_snapshot":
                return await self._retry_scan(s, ctx, old, key)
            if old["operation"] == "analyze_structure":
                return await self._retry_structure(s, ctx, old, key)
            existing = await self._existing(s, ctx, old, key)
            if existing:
                return existing
            if old["state"] not in {"failed", "cancelled"} or old["submission_id"] is None:
                raise RetryRejected("retry_not_supported")
            source = (
                (
                    await s.execute(
                        select(subs).where(
                            subs.c.org_id == ctx.org_id, subs.c.id == old["submission_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            try:
                expected_policy = resolve_admission_policy(
                    source["policy_hash"],
                    github_strip_root=source["source_kind"] == "github_archive",
                ).digest()
            except ValueError:
                raise RetryRejected("retry_not_supported") from None
            if (
                source["policy_hash"] != old["config_hash"]
                or source["policy_hash"] != expected_policy
            ):
                raise RetryRejected("retry_not_supported")
            if old["operation"] == "admit_source":
                if source["state"] not in {"failed", "expired", "cancelled"}:
                    raise RetryRejected("retry_not_supported")
                code = old["failure_code"]
                if code == "attempts_exhausted":
                    attempts = PHASE2_TABLES["job_attempts"]
                    code = await s.scalar(
                        select(attempts.c.error_code)
                        .where(attempts.c.org_id == ctx.org_id, attempts.c.job_id == job_id)
                        .order_by(attempts.c.fence.desc())
                        .limit(1)
                    )
                if code not in RECOVERABLE:
                    raise RetryRejected("retry_not_supported")
                input_id = source["input_object_id"]
            else:
                if (
                    source["state"] != "admitted"
                    or old["snapshot_id"] != source["snapshot_id"]
                    or old["parent_job_id"] is None
                ):
                    raise RetryRejected("retry_not_supported")
                parent = await load_job(s, ctx.org_id, old["parent_job_id"])
                input_id = parent["result_object_id"]
            obj = (
                (
                    await s.execute(
                        select(objects).where(
                            objects.c.org_id == ctx.org_id, objects.c.id == input_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            now = await database_now(s)
            if (
                obj is None
                or obj["state"] not in {"verified", "available"}
                or (obj["expires_at"] is not None and obj["expires_at"] <= now)
            ):
                raise RetryRejected("source_unavailable_for_retry")
            expected = ObjectDescriptor(
                key=obj["key"],
                sha256=obj["sha256"],
                size_bytes=obj["size_bytes"],
                version_id=obj["version_id"],
            )
        digest = hashlib.sha256()
        size = 0
        try:
            async for block in self.store.open_verified(expected):
                size += len(block)
                if size > expected.size_bytes:
                    raise ValueError("descriptor bound")
                digest.update(block)
        except (OSError, ValueError, DuplicateConflict):
            raise RetryRejected("source_unavailable_for_retry") from None
        if digest.hexdigest() != expected.sha256 or size != expected.size_bytes:
            raise RetryRejected("source_unavailable_for_retry")
        async with transaction(self.factory) as s:
            await lock_scope(s, ctx.org_id, ctx.principal_id, old["repository_id"])
            locked = await load_job(s, ctx.org_id, job_id, lock=True)
            existing = await self._existing(s, ctx, locked, key)
            if existing:
                return existing
            source = (
                (
                    await s.execute(
                        select(subs)
                        .where(subs.c.org_id == ctx.org_id, subs.c.id == old["submission_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            obj = (
                (
                    await s.execute(
                        select(objects)
                        .where(objects.c.org_id == ctx.org_id, objects.c.id == input_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = await database_now(s)
            actual = ObjectDescriptor(
                key=obj["key"],
                sha256=obj["sha256"],
                size_bytes=obj["size_bytes"],
                version_id=obj["version_id"],
            )
            if (
                actual != expected
                or obj["state"] not in {"verified", "available"}
                or (obj["expires_at"] is not None and obj["expires_at"] <= now)
            ):
                raise RetryRejected("source_unavailable_for_retry")
            submission_id = source["id"]
            retry_sub = None
            if old["operation"] == "admit_source":
                quota = (
                    (await s.execute(select(limits).where(limits.c.org_id == ctx.org_id)))
                    .mappings()
                    .one()
                )
                if quota["reserved_bytes"] + size > quota["max_reserved_bytes"]:
                    raise CapacityExceeded("retry byte quota reached")
                submission_id = uuid4()
                retry_sub = source["id"]
                await s.execute(
                    insert(subs).values(
                        org_id=ctx.org_id,
                        id=submission_id,
                        repository_id=source["repository_id"],
                        principal_id=ctx.principal_id,
                        source_kind=source["source_kind"],
                        state="admission_queued",
                        request_key=key,
                        request_hash=semantic_digest(
                            {"retry_of_job_id": job_id, "policy_hash": old["config_hash"]}
                        ),
                        retry_of_submission_id=source["id"],
                        reserved_bytes=size,
                        declared_size=size,
                        declared_sha=source["actual_sha"],
                        actual_size=size,
                        actual_sha=source["actual_sha"],
                        input_object_id=input_id,
                        requested_ref=source["requested_ref"],
                        resolved_commit=source["resolved_commit"],
                        policy_hash=source["policy_hash"],
                        expires_at=func.clock_timestamp() + text("interval '24 hours'"),
                    )
                )
                await s.execute(
                    update(limits)
                    .where(limits.c.org_id == ctx.org_id)
                    .values(reserved_bytes=limits.c.reserved_bytes + size)
                )
            request = EnqueueJobV2(
                repository_id=old["repository_id"],
                submission_id=submission_id,
                snapshot_id=old["snapshot_id"]
                if old["operation"] == "inventory_snapshot"
                else None,
                operation=old["operation"],
                request_key=key,
                config_hash=old["config_hash"],
                parent_job_id=old["parent_job_id"],
                retry_of_job_id=job_id,
            )
            new = await self.jobs._enqueue(s, ctx, request)
            return RetryResponseV1(
                retry_job_id=new["id"],
                submission_id=submission_id,
                retry_of_job_id=job_id,
                retry_of_submission_id=retry_sub,
            )

    async def _retry_scan(
        self, s: AsyncSession, ctx: TenantContext, old: dict[str, Any], key: str
    ) -> RetryScanResponseV3:
        from app.models.scan_tables import scan_jobs
        from app.repositories.scan_repository import ScanRepository
        from app.services.analysis_sources import locate_source

        await lock_scope(s, ctx.org_id, ctx.principal_id, old["repository_id"], old["snapshot_id"])
        old = await load_job(s, ctx.org_id, old["id"], lock=True)
        if old["principal_id"] != ctx.principal_id:
            raise RetryRejected("retry_principal_mismatch")
        existing = (
            (
                await s.execute(
                    select(scan_jobs).where(
                        scan_jobs.c.org_id == ctx.org_id,
                        scan_jobs.c.request_key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                any(
                    existing[k] != old[k]
                    for k in (
                        "principal_id",
                        "repository_id",
                        "snapshot_id",
                        "scan_spec_id",
                        "config_hash",
                        "operation",
                    )
                )
                or existing["retry_of_job_id"] != old["id"]
            ):
                raise DuplicateConflict("retry key has different input")
            return RetryScanResponseV3(
                retry_job_id=existing["id"],
                retry_of_job_id=old["id"],
                scan_spec_id=old["scan_spec_id"],
            )
        if old["state"] not in {"failed", "cancelled"}:
            raise RetryRejected("retry_not_supported")
        allowed = {
            "scanner_unavailable",
            "scanner_timeout",
            "scanner_process_failed",
            "finalization_timeout",
            "deadline_exceeded",
            "lease_expired",
            "user_requested",
            "access_revoked",
            "storage_unavailable",
        }
        code = old["failure_code"]
        if old["state"] == "failed":
            if code == "attempts_exhausted":
                attempts = PHASE2_TABLES["job_attempts"]
                code = await s.scalar(
                    select(attempts.c.error_code)
                    .where(
                        attempts.c.org_id == ctx.org_id,
                        attempts.c.job_id == old["id"],
                    )
                    .order_by(attempts.c.fence.desc())
                    .limit(1)
                )
            if code not in allowed:
                raise RetryRejected("retry_not_supported")
        spec = await ScanRepository(s).get_spec(ctx, old["repository_id"], old["scan_spec_id"])
        if spec is None or self.scan_capabilities is None:
            raise RetryRejected("scanner_unavailable")
        self.scan_capabilities.validate(spec.config)
        await locate_source(s, ctx.org_id, old["repository_id"], old["snapshot_id"])
        new = await self.jobs._enqueue(
            s,
            ctx,
            EnqueueScanJobV4(
                repository_id=old["repository_id"],
                snapshot_id=old["snapshot_id"],
                scan_spec_id=old["scan_spec_id"],
                config_hash=old["config_hash"],
                request_key=key,
                retry_of_job_id=old["id"],
            ),
        )
        return RetryScanResponseV3(
            retry_job_id=new["id"], retry_of_job_id=old["id"], scan_spec_id=old["scan_spec_id"]
        )

    async def _retry_structure(
        self, s: AsyncSession, ctx: TenantContext, old: dict[str, Any], key: str
    ) -> RetryResponseV2:
        from app.models.phase3_tables import structural_jobs
        from app.services.analysis_sources import locate_source

        # Use the standard lock hierarchy, including org capacity, before replay lookup.
        await lock_scope(s, ctx.org_id, ctx.principal_id, old["repository_id"], old["snapshot_id"])
        old = await load_job(s, ctx.org_id, old["id"], lock=True)
        # The immutable spec and publication guard bind the requesting principal.
        # Other writers can start a new analysis; they cannot inherit that identity.
        if old["principal_id"] != ctx.principal_id:
            raise RetryRejected("retry_principal_mismatch")
        existing = (
            (
                await s.execute(
                    select(structural_jobs).where(
                        structural_jobs.c.org_id == ctx.org_id,
                        structural_jobs.c.request_key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            if (
                existing["retry_of_job_id"] != old["id"]
                or existing["analysis_spec_id"] != old["analysis_spec_id"]
                or existing["config_hash"] != old["config_hash"]
            ):
                raise DuplicateConflict("retry key has different input")
            new = dict(existing)
        else:
            if old["state"] not in {"failed", "cancelled"}:
                raise RetryRejected("retry_not_supported")
            if old["state"] == "failed" and old["failure_code"] not in RECOVERABLE:
                raise RetryRejected("retry_not_supported")
            await locate_source(s, ctx.org_id, old["repository_id"], old["snapshot_id"])
            new = await self.jobs._enqueue(
                s,
                ctx,
                EnqueueJobV3(
                    repository_id=old["repository_id"],
                    snapshot_id=old["snapshot_id"],
                    operation="analyze_structure",
                    analysis_spec_id=old["analysis_spec_id"],
                    config_hash=old["config_hash"],
                    request_key=key,
                    retry_of_job_id=old["id"],
                ),
            )
        return RetryResponseV2(
            retry_job_id=new["id"],
            retry_of_job_id=old["id"],
            analysis_spec_id=old["analysis_spec_id"],
        )
