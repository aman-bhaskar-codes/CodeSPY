"""Durable, principal-bound indexing requests over retained admitted input."""

from collections import Counter
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.indexing import (
    EnqueueIndexJobV5,
    IndexAvailabilityV1,
    IndexBuildConfigV1,
    IndexCreateRequestV1,
    IndexCreateResponseV1,
    IndexFileV1,
    IndexOmissionV1,
    IndexRetryResponseV1,
    IndexSpecV1,
    IndexStatusV1,
    OmissionReason,
)
from app.contracts.search import SearchScopeV1
from app.contracts.structure import SourceFileV1
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import DuplicateConflict, RetryRejected, ScopeViolation
from app.domain.scope import TenantContext
from app.models.index_tables import index_jobs, index_specs
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.index_builder import IndexBuilderUnavailable
from app.repositories.index_repository import IndexRepository
from app.repositories.job_repository import index_job_record, load_job
from app.security.authorization import lock_scope, require_snapshot
from app.services.analysis_sources import LocatedSource, locate_source
from app.services.jobs import JobService
from app.services.privacy_transform import is_sensitive_path
from app.services.retrieval_selection import resolve_lexical_set

RECOVERABLE = frozenset(
    {
        "lease_expired",
        "storage_unavailable",
        "queue_unavailable",
        "index_process_failed",
        "index_cleanup_pending",
        "index_timeout",
        "index_runtime_unavailable",
        "finalization_timeout",
    }
)


class IndexJobCapabilities(Protocol):
    async def validate(self, config: IndexBuildConfigV1) -> None: ...


def select_index_files(
    files: tuple[SourceFileV1, ...], config: IndexBuildConfigV1
) -> tuple[tuple[IndexFileV1, ...], tuple[IndexOmissionV1, ...]]:
    selected: list[IndexFileV1] = []
    omitted: Counter[OmissionReason] = Counter()
    size = metadata = 0
    for source in sorted(files, key=lambda f: (f.path, f.file_id)):
        file = SourceFileV1.model_validate(source.model_dump())
        reason: OmissionReason | None = None
        if is_sensitive_path(file.path, config.privacy_policy.sensitive_path_patterns):
            reason = "sensitive_path"
        elif (
            not file.eligible
            or file.size_bytes > 2_097_152
            or len(selected) >= 128
            or size + file.size_bytes > 16_777_216
        ):
            reason = "source_limit"
        if reason is not None:
            omitted[reason] += 1
            continue
        candidate = IndexFileV1(
            file_id=file.file_id, path=file.path, sha256=file.sha256, size_bytes=file.size_bytes
        )
        row_bytes = len(canonical_bytes(candidate.model_dump())) + 1
        # Leave 16 KiB for the bounded descriptors, fixed profile and omission summary.
        if metadata + row_bytes > 81_920:
            omitted["manifest_limit"] += 1
            continue
        selected.append(candidate)
        metadata += row_bytes
        size += file.size_bytes
    return tuple(selected), tuple(
        IndexOmissionV1(reason=r, unit="file", count=n) for r, n in sorted(omitted.items())
    )


def make_spec(
    ctx: TenantContext,
    repository_id: UUID,
    snapshot_id: UUID,
    config: IndexBuildConfigV1,
    source: LocatedSource,
) -> IndexSpecV1:
    files, omissions = select_index_files(source.files, config)
    return IndexSpecV1(
        org_id=ctx.org_id,
        principal_id=ctx.principal_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        config=config,
        source=source.archive,
        manifest=source.manifest,
        strip_root=source.policy.github_strip_root,
        files=files,
        admitted_files=len(source.files),
        selection_omissions=omissions,
    )


class IndexJobService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        capabilities: IndexJobCapabilities | None = None,
    ):
        self.factory, self.capabilities = factory, capabilities
        self.jobs = JobService(factory)

    async def validate(self, config: IndexBuildConfigV1) -> None:
        if self.capabilities is None:
            raise IndexBuilderUnavailable("index_builder_unavailable")
        await self.capabilities.validate(config)

    async def availability(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        config: IndexBuildConfigV1 | None = None,
    ) -> IndexAvailabilityV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            selected = await resolve_lexical_set(
                session,
                SearchScopeV1(
                    org_id=ctx.org_id,
                    principal_id=ctx.principal_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                ),
            )
            can_build = False
            if config is not None and self.capabilities is not None:
                try:
                    await require_snapshot(session, ctx, repository_id, snapshot_id, write=True)
                    await self.validate(config)
                    located = await locate_source(session, ctx.org_id, repository_id, snapshot_id)
                    await self._retained(
                        session, make_spec(ctx, repository_id, snapshot_id, config, located)
                    )
                    can_build = True
                except (ScopeViolation, DuplicateConflict, NoResultFound, IndexBuilderUnavailable):
                    can_build = False
            fields = selected.response_fields() if selected else {}
            if selected:
                fields["index_id"] = selected.index_id
            return IndexAvailabilityV1(
                repository_id=repository_id, snapshot_id=snapshot_id, can_build=can_build, **fields
            )

    async def cancel(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID, job_id: UUID
    ) -> IndexStatusV1:
        await self.get(ctx, repository_id, snapshot_id, job_id)
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id, write=True)
        await self.jobs.request_cancel(ctx, job_id)
        return await self.get(ctx, repository_id, snapshot_id, job_id)

    async def _actions(
        self, session: AsyncSession, ctx: TenantContext, row: dict[str, Any]
    ) -> tuple[bool, bool]:
        grants = PHASE2_TABLES["repository_grants"]
        permission = await session.scalar(
            select(grants.c.permission).where(
                grants.c.org_id == ctx.org_id,
                grants.c.repository_id == row["repository_id"],
                grants.c.principal_id == ctx.principal_id,
                grants.c.revoked_at.is_(None),
            )
        )
        if permission != "write":
            return False, False
        can_cancel = row["state"] in {"queued", "retry_wait", "running"}
        can_retry = False
        if (
            self.capabilities is not None
            and row["principal_id"] == ctx.principal_id
            and row["state"] in {"failed", "cancelled"}
            and row["deadline"] > await database_now(session)
        ):
            code = row["failure_code"]
            if code == "attempts_exhausted":
                attempts = PHASE2_TABLES["job_attempts"]
                code = await session.scalar(
                    select(attempts.c.error_code)
                    .where(attempts.c.org_id == ctx.org_id, attempts.c.job_id == row["id"])
                    .order_by(attempts.c.fence.desc())
                    .limit(1)
                )
            if row["state"] == "cancelled" or code in RECOVERABLE:
                try:
                    spec = await IndexRepository(session).get_spec(
                        ctx, row["repository_id"], row["index_spec_id"]
                    )
                    if spec is not None:
                        located = await locate_source(
                            session, ctx.org_id, row["repository_id"], row["snapshot_id"]
                        )
                        current = make_spec(
                            ctx, row["repository_id"], row["snapshot_id"], spec.config, located
                        )
                        await self._retained(session, spec)
                        can_retry = current.semantic_hash() == spec.semantic_hash()
                except (DuplicateConflict, NoResultFound, ScopeViolation):
                    can_retry = False
        return can_cancel, can_retry

    async def _retained(self, session: AsyncSession, spec: IndexSpecV1) -> dict[str, UUID]:
        objects = PHASE2_TABLES["object_records"]
        rows = (
            (
                await session.execute(
                    select(objects).where(
                        objects.c.org_id == spec.org_id,
                        objects.c.repository_id == spec.repository_id,
                        objects.c.key.in_((spec.source.key, spec.manifest.key)),
                        objects.c.state == "available",
                    )
                )
            )
            .mappings()
            .all()
        )
        now = await database_now(session)
        result = {}
        for kind, descriptor in (("source", spec.source), ("manifest", spec.manifest)):
            matches = [r for r in rows if r["kind"] == kind and r["key"] == descriptor.key]
            if (
                len(matches) != 1
                or matches[0]["sha256"] != descriptor.sha256
                or matches[0]["size_bytes"] != descriptor.size_bytes
                or matches[0]["version_id"] != descriptor.version_id
                or (matches[0]["expires_at"] is not None and matches[0]["expires_at"] <= now)
            ):
                raise DuplicateConflict("retained index input unavailable")
            result[kind] = matches[0]["id"]
        return result

    async def create(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        config: IndexBuildConfigV1,
        request_key: str,
    ) -> IndexCreateResponseV1:
        config = IndexBuildConfigV1.model_validate(config.model_dump())
        request = IndexCreateRequestV1(config=config, request_key=request_key)
        config = request.config
        await self.validate(config)
        async with transaction(self.factory) as session:
            await lock_scope(session, ctx.org_id, ctx.principal_id, repository_id)
            await require_snapshot(session, ctx, repository_id, snapshot_id, write=True)
            old = (
                (
                    await session.execute(
                        select(index_jobs).where(
                            index_jobs.c.org_id == ctx.org_id,
                            index_jobs.c.request_key == request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if old is not None:
                if (
                    old["operation"] != "index_snapshot"
                    or old["principal_id"] != ctx.principal_id
                    or old["repository_id"] != repository_id
                    or old["snapshot_id"] != snapshot_id
                    or old["config_hash"] != config.semantic_hash()
                    or old["retry_of_job_id"] is not None
                ):
                    raise DuplicateConflict("request key has different input")
                return IndexCreateResponseV1(job=index_job_record(dict(old)))
            located = await locate_source(session, ctx.org_id, repository_id, snapshot_id)
            spec = make_spec(ctx, repository_id, snapshot_id, config, located)
            objects = await self._retained(session, spec)
            spec_id = uuid4()
            await session.execute(
                insert(index_specs).values(
                    org_id=ctx.org_id,
                    id=spec_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    principal_id=ctx.principal_id,
                    config_hash=config.semantic_hash(),
                    spec_hash=spec.semantic_hash(),
                    config_json=config.model_dump(mode="json"),
                    spec_json=spec.model_dump(mode="json"),
                    canonical_text=canonical_bytes(spec.model_dump()).decode(),
                    source_object_id=objects["source"],
                    manifest_object_id=objects["manifest"],
                )
            )
            job = await self.jobs._enqueue(
                session,
                ctx,
                EnqueueIndexJobV5(
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    index_spec_id=spec_id,
                    config_hash=config.semantic_hash(),
                    request_key=request_key,
                ),
            )
            return IndexCreateResponseV1(job=index_job_record(job))

    async def get(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID, job_id: UUID
    ) -> IndexStatusV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            row = await load_job(session, ctx.org_id, job_id)
            if (
                row["operation"] != "index_snapshot"
                or row["repository_id"] != repository_id
                or row["snapshot_id"] != snapshot_id
            ):
                raise ScopeViolation("index job unavailable")
            receipt = None
            if row["state"] in {"succeeded", "partial"}:
                receipt = await IndexRepository(session).get_receipt(
                    ctx.org_id, job_id, row["current_attempt_id"], row["fence"]
                )
            can_cancel, can_retry = await self._actions(session, ctx, row)
            return IndexStatusV1(
                job=index_job_record(row),
                receipt=receipt,
                can_cancel=can_cancel,
                can_retry=can_retry,
            )

    async def retry(
        self, ctx: TenantContext, repository_id: UUID, job_id: UUID, request_key: str
    ) -> IndexRetryResponseV1:
        # Read immutable configuration before external readiness IO; reauthorize under locks below.
        async with transaction(self.factory) as session:
            old = await load_job(session, ctx.org_id, job_id)
            await require_snapshot(session, ctx, repository_id, old["snapshot_id"], write=True)
            if (
                old["operation"] != "index_snapshot"
                or old["repository_id"] != repository_id
                or old["principal_id"] != ctx.principal_id
            ):
                raise RetryRejected("retry_not_supported")
            spec = await IndexRepository(session).get_spec(ctx, repository_id, old["index_spec_id"])
            if spec is None:
                raise RetryRejected("retry_input_unavailable")
        IndexCreateRequestV1(config=spec.config, request_key=request_key)
        await self.validate(spec.config)
        async with transaction(self.factory) as session:
            await lock_scope(session, ctx.org_id, ctx.principal_id, repository_id)
            old = await load_job(session, ctx.org_id, job_id, lock=True)
            await require_snapshot(session, ctx, repository_id, old["snapshot_id"], write=True)
            if old["principal_id"] != ctx.principal_id:
                raise RetryRejected("retry_not_supported")
            existing = (
                (
                    await session.execute(
                        select(index_jobs).where(
                            index_jobs.c.org_id == ctx.org_id,
                            index_jobs.c.request_key == request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["operation"] != "index_snapshot"
                    or existing["principal_id"] != ctx.principal_id
                    or existing["repository_id"] != repository_id
                    or existing["snapshot_id"] != old["snapshot_id"]
                    or existing["index_spec_id"] != old["index_spec_id"]
                    or existing["retry_of_job_id"] != job_id
                ):
                    raise DuplicateConflict("retry key has different input")
                return IndexRetryResponseV1(
                    retry_job_id=existing["id"],
                    retry_of_job_id=job_id,
                    index_spec_id=old["index_spec_id"],
                )
            if old["state"] not in {"failed", "cancelled"} or old["deadline"] <= await database_now(
                session
            ):
                raise RetryRejected("retry_not_supported")
            if old["state"] == "failed":
                code = old["failure_code"]
                if code == "attempts_exhausted":
                    attempts = PHASE2_TABLES["job_attempts"]
                    code = await session.scalar(
                        select(attempts.c.error_code)
                        .where(attempts.c.org_id == ctx.org_id, attempts.c.job_id == job_id)
                        .order_by(attempts.c.fence.desc())
                        .limit(1)
                    )
                if code not in RECOVERABLE:
                    raise RetryRejected("retry_not_supported")
            located = await locate_source(session, ctx.org_id, repository_id, old["snapshot_id"])
            current = make_spec(ctx, repository_id, old["snapshot_id"], spec.config, located)
            if current.semantic_hash() != spec.semantic_hash():
                raise RetryRejected("retry_input_differs")
            await self._retained(session, spec)
            new = await self.jobs._enqueue(
                session,
                ctx,
                EnqueueIndexJobV5(
                    repository_id=repository_id,
                    snapshot_id=old["snapshot_id"],
                    index_spec_id=old["index_spec_id"],
                    config_hash=old["config_hash"],
                    request_key=request_key,
                    retry_of_job_id=job_id,
                ),
            )
            return IndexRetryResponseV1(
                retry_job_id=new["id"], retry_of_job_id=job_id, index_spec_id=old["index_spec_id"]
            )
