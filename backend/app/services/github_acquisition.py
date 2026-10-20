"""Pinned GitHub acquisition through an injected, authority-checking provider port.

The live fixed-origin transport is a separately qualified adapter, never an
implicit HTTP client or a credentials fallback.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.ingestion import (
    AdmissionPolicy,
    AdmissionPolicyV1,
    ObjectDescriptor,
    SubmissionRecordV1,
    resolve_admission_policy,
)
from app.contracts.jobs import AttemptLeaseV2, EnqueueJobV2
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import CapacityExceeded, DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories
from app.ports.object_store import ObjectStore
from app.repositories.command_repository import record_command, replay_command
from app.repositories.event_repository import append_event
from app.security.authorization import lock_scope
from app.services.job_finalization import descriptor
from app.services.jobs import JobService
from app.services.submissions import SubmissionService


@dataclass(frozen=True)
class RepositoryAccess:
    repository_id: int
    installation_id: int
    account_id: int


class GitHubProvider(Protocol):
    async def verify_access(
        self, principal: UUID, repository_id: int, installation_id: int
    ) -> RepositoryAccess: ...
    async def resolve(self, access: RepositoryAccess, ref: str) -> str: ...
    def archive(self, access: RepositoryAccess, commit: str) -> AsyncIterator[bytes]: ...


class GitHubAcquisitionService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        provider: GitHubProvider,
        *,
        policy: AdmissionPolicy | None = None,
    ):
        self.factory, self.store, self.provider = factory, store, provider
        self.jobs = JobService(factory)
        self.submissions = SubmissionService(factory, store)
        chosen = policy if policy is not None else AdmissionPolicyV1(github_strip_root=True)
        self.policy = resolve_admission_policy(chosen.digest(), github_strip_root=True)

    async def bind(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        github_repository_id: int,
        installation_id: int,
        *,
        request_key: str | None = None,
    ) -> RepositoryAccess:
        if github_repository_id <= 0 or installation_id <= 0:
            raise ValueError("numeric provider identities required")
        # The provider must prove the current user+App intersection, not just
        # possession of an installation ID. This I/O holds no database lock.
        access = await self.provider.verify_access(
            ctx.principal_id, github_repository_id, installation_id
        )
        if (
            access.repository_id != github_repository_id
            or access.installation_id != installation_id
            or access.account_id <= 0
        ):
            raise ScopeViolation("provider intersection unavailable")
        bindings, grants, limits, user_grants = (
            PHASE2_TABLES[name]
            for name in [
                "github_repository_bindings",
                "repository_grants",
                "org_work_limits",
                "github_user_grants",
            ]
        )
        async with transaction(self.factory) as s:
            await s.execute(
                select(limits.c.org_id).where(limits.c.org_id == ctx.org_id).with_for_update()
            )
            role = await s.scalar(
                select(memberships.c.role)
                .where(
                    memberships.c.org_id == ctx.org_id,
                    memberships.c.principal_id == ctx.principal_id,
                )
                .with_for_update()
            )
            if role not in {"owner", "admin"}:
                raise ScopeViolation("repository binding requires organization owner or admin")
            token = (
                (
                    await s.execute(
                        select(user_grants)
                        .where(user_grants.c.principal_id == ctx.principal_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            now = await database_now(s)
            if token is None or token["revoked_at"] is not None or token["expires_at"] <= now:
                raise ScopeViolation("provider grant unavailable")
            repo = await s.scalar(
                select(repositories.c.id)
                .where(
                    repositories.c.org_id == ctx.org_id,
                    repositories.c.id == repository_id,
                    repositories.c.source_kind == "github",
                )
                .with_for_update()
            )
            if repo is None:
                raise ScopeViolation("GitHub repository unavailable")
            command = {
                "repository_id": str(repository_id),
                "github_repository_id": github_repository_id,
                "installation_id": installation_id,
            }
            replay = (
                await replay_command(s, ctx, request_key, "github_bind", command)
                if request_key
                else None
            )
            existing = (
                (
                    await s.execute(
                        select(bindings)
                        .where(
                            bindings.c.org_id == ctx.org_id,
                            bindings.c.repository_id == repository_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if replay is not None:
                if existing is None or existing["state"] != "active":
                    raise DuplicateConflict("a fresh command is required after revocation")
                return access
            if existing is not None:
                if (
                    existing["github_repository_id"],
                    existing["installation_id"],
                    existing["account_id"],
                ) != (access.repository_id, access.installation_id, access.account_id):
                    raise DuplicateConflict("binding identity differs")
                if existing["state"] != "active":
                    await s.execute(
                        update(bindings)
                        .where(
                            bindings.c.org_id == ctx.org_id,
                            bindings.c.repository_id == repository_id,
                        )
                        .values(state="active", version=bindings.c.version + 1, last_checked_at=now)
                    )
            else:
                await s.execute(
                    insert(bindings).values(
                        org_id=ctx.org_id,
                        repository_id=repository_id,
                        github_repository_id=access.repository_id,
                        installation_id=access.installation_id,
                        account_id=access.account_id,
                        state="active",
                        last_checked_at=now,
                    )
                )
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await s.execute(
                pg_insert(grants)
                .values(
                    org_id=ctx.org_id,
                    repository_id=repository_id,
                    principal_id=ctx.principal_id,
                    permission="write",
                )
                .on_conflict_do_update(
                    index_elements=[grants.c.org_id, grants.c.repository_id, grants.c.principal_id],
                    set_={"permission": "write", "revoked_at": None},
                )
            )
            if request_key:
                await record_command(
                    s,
                    ctx,
                    request_key,
                    "github_bind",
                    command,
                    {
                        "github_repository_id": github_repository_id,
                        "installation_id": installation_id,
                    },
                )
        return access

    async def request(
        self, ctx: TenantContext, repository_id: UUID, ref: str, key: str
    ) -> SubmissionRecordV1:
        if (
            not 1 <= len(ref.encode()) <= 255
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", ref)
            or not 1 <= len(key.encode()) <= 128
            or key.startswith(("job:", "child:"))
        ):
            raise ValueError("invalid acquisition request")
        submissions, limits = (PHASE2_TABLES[n] for n in ["source_submissions", "org_work_limits"])
        digest = semantic_digest(
            {
                "repository_id": repository_id,
                "requested_ref": ref,
                "policy_hash": self.policy.digest(),
                "source_kind": "github_archive",
            }
        )
        async with transaction(self.factory) as s:
            await lock_scope(s, ctx.org_id, ctx.principal_id, repository_id)
            binding = await s.scalar(
                select(PHASE2_TABLES["github_repository_bindings"].c.github_repository_id).where(
                    PHASE2_TABLES["github_repository_bindings"].c.org_id == ctx.org_id,
                    PHASE2_TABLES["github_repository_bindings"].c.repository_id == repository_id,
                    PHASE2_TABLES["github_repository_bindings"].c.state == "active",
                )
            )
            if binding is None:
                raise ScopeViolation("repository unavailable")
            old = (
                (
                    await s.execute(
                        select(submissions).where(
                            submissions.c.org_id == ctx.org_id, submissions.c.request_key == key
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if old:
                if old["request_hash"] != digest:
                    raise DuplicateConflict("acquisition key differs")
                return await self.submissions._record(s, dict(old))
            quota = (
                (await s.execute(select(limits).where(limits.c.org_id == ctx.org_id)))
                .mappings()
                .one()
            )
            if (
                quota["reserved_bytes"] + self.policy.max_archive_bytes
                > quota["max_reserved_bytes"]
            ):
                raise CapacityExceeded("byte reservation exceeded")
            identity = uuid4()
            row = (
                (
                    await s.execute(
                        insert(submissions)
                        .values(
                            org_id=ctx.org_id,
                            id=identity,
                            repository_id=repository_id,
                            principal_id=ctx.principal_id,
                            source_kind="github_archive",
                            state="admission_queued",
                            request_key=key,
                            request_hash=digest,
                            reserved_bytes=self.policy.max_archive_bytes,
                            requested_ref=ref,
                            policy_hash=self.policy.digest(),
                            expires_at=func.clock_timestamp() + timedelta(hours=24),
                        )
                        .returning(submissions)
                    )
                )
                .mappings()
                .one()
            )
            await s.execute(
                update(limits)
                .where(limits.c.org_id == ctx.org_id)
                .values(reserved_bytes=limits.c.reserved_bytes + self.policy.max_archive_bytes)
            )
            await self.jobs._enqueue(
                s,
                ctx,
                EnqueueJobV2(
                    repository_id=repository_id,
                    submission_id=identity,
                    operation="admit_source",
                    request_key=f"job:submission:{identity}",
                    config_hash=self.policy.digest(),
                ),
            )
            return await self.submissions._record(s, dict(row))

    async def acquire(self, lease: AttemptLeaseV2) -> ObjectDescriptor:
        submissions, objects, bindings = (
            PHASE2_TABLES[n]
            for n in ["source_submissions", "object_records", "github_repository_bindings"]
        )
        async with transaction(self.factory) as s:
            job, _ = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, job)
            sub = (
                (
                    await s.execute(
                        select(submissions).where(
                            submissions.c.org_id == lease.org_id,
                            submissions.c.id == job["submission_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            binding = dict(
                (
                    await s.execute(
                        select(bindings).where(
                            bindings.c.org_id == lease.org_id,
                            bindings.c.repository_id == job["repository_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            selected = None
            if sub["input_object_id"] is not None:
                row = (
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
                selected = descriptor(dict(row))
        access = await self.provider.verify_access(
            job["principal_id"], binding["github_repository_id"], binding["installation_id"]
        )
        if access != RepositoryAccess(
            binding["github_repository_id"], binding["installation_id"], binding["account_id"]
        ):
            raise ScopeViolation("provider identity differs")
        if selected is not None:
            async for _ in self.store.open_verified(selected):
                pass
            return selected
        commit = sub["resolved_commit"] or await self.provider.resolve(access, sub["requested_ref"])
        if not re.fullmatch("[0-9a-f]{40}", commit):
            raise DuplicateConflict("provider commit is not full SHA")
        async with transaction(self.factory) as s:
            locked, _ = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, locked)
            current = (
                (
                    await s.execute(
                        select(submissions)
                        .where(submissions.c.org_id == lease.org_id, submissions.c.id == sub["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if current["resolved_commit"] is None:
                await s.execute(
                    update(submissions)
                    .where(submissions.c.org_id == lease.org_id, submissions.c.id == sub["id"])
                    .values(resolved_commit=commit)
                )
                await append_event(s, locked, "commit_pinned", {"commit_sha": commit})
            else:
                commit = current["resolved_commit"]
            identity = uuid4()
            stage_identity = uuid4()
            stage_key = f"stage/{lease.org_id}/{lease.job_id}/{lease.fence}/{stage_identity}"
            key = (
                f"quarantine/{lease.org_id}/{job['repository_id']}/"
                f"{sub['id']}/{identity}/source.zip"
            )
            await s.execute(
                insert(objects).values(
                    org_id=lease.org_id,
                    id=identity,
                    repository_id=job["repository_id"],
                    key=key,
                    kind="input",
                    state="writing",
                    owner_job_id=lease.job_id,
                    owner_fence=lease.fence,
                    expires_at=func.clock_timestamp() + timedelta(hours=24),
                )
            )
            await s.execute(
                insert(objects).values(
                    org_id=lease.org_id,
                    id=stage_identity,
                    repository_id=job["repository_id"],
                    key=stage_key,
                    kind="stage",
                    state="writing",
                    owner_job_id=lease.job_id,
                    owner_fence=lease.fence,
                    expires_at=func.clock_timestamp() + timedelta(hours=1),
                )
            )
        async with asyncio.timeout(120):
            staged = await self.store.put_stage(
                stage_key, self.provider.archive(access, commit), self.policy.max_archive_bytes
            )
        async with transaction(self.factory) as s:
            await s.execute(
                update(objects)
                .where(objects.c.org_id == lease.org_id, objects.c.id == stage_identity)
                .values(
                    state="verified",
                    sha256=staged.sha256,
                    size_bytes=staged.size_bytes,
                    version_id=staged.version_id,
                )
            )
        acquired = await self.store.put_if_absent(key, staged)
        # Recheck provider authority outside locks, then compare the local version.
        if (
            await self.provider.verify_access(
                job["principal_id"], binding["github_repository_id"], binding["installation_id"]
            )
            != access
        ):
            raise ScopeViolation("provider authority changed")
        async with transaction(self.factory) as s:
            locked, _ = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, locked)
            version = await s.scalar(
                select(bindings.c.version).where(
                    bindings.c.org_id == lease.org_id,
                    bindings.c.repository_id == job["repository_id"],
                )
            )
            if version != binding["version"]:
                raise ScopeViolation("provider binding changed")
            current = (
                (
                    await s.execute(
                        select(submissions)
                        .where(submissions.c.org_id == lease.org_id, submissions.c.id == sub["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if current["input_object_id"] is not None:
                raise DuplicateConflict("acquisition already selected")
            await s.execute(
                update(objects)
                .where(objects.c.org_id == lease.org_id, objects.c.id == identity)
                .values(
                    state="verified",
                    sha256=acquired.sha256,
                    size_bytes=acquired.size_bytes,
                    version_id=acquired.version_id,
                )
            )
            await s.execute(
                update(submissions)
                .where(submissions.c.org_id == lease.org_id, submissions.c.id == sub["id"])
                .values(
                    input_object_id=identity,
                    actual_sha=acquired.sha256,
                    actual_size=acquired.size_bytes,
                )
            )
        return acquired
