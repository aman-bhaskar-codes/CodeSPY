"""Reservations and bounded streaming into quarantine; no archive execution."""

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.ingestion import (
    AdmissionPolicy,
    AdmissionPolicyV1,
    SubmissionRecordV1,
    UploadReservationV1,
    resolve_admission_policy,
)
from app.contracts.jobs import EnqueueJobV2
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import (
    CapacityExceeded,
    DuplicateConflict,
    InvalidTransition,
    ScopeViolation,
    UploadRejected,
)
from app.domain.scope import TenantContext
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.object_store import ObjectStore
from app.repositories.job_repository import jobs
from app.security.authorization import lock_scope, require_repository
from app.services.jobs import JobService

submissions = PHASE2_TABLES["source_submissions"]
objects = PHASE2_TABLES["object_records"]
limits = PHASE2_TABLES["org_work_limits"]


class SubmissionService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        *,
        policy: AdmissionPolicy | None = None,
    ):
        self.factory = factory
        self.store = store
        self.jobs = JobService(factory)
        chosen = policy if policy is not None else AdmissionPolicyV1()
        self.policy = resolve_admission_policy(chosen.digest(), github_strip_root=False)

    async def _load(
        self, s: AsyncSession, ctx: TenantContext, id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        q = select(submissions).where(submissions.c.org_id == ctx.org_id, submissions.c.id == id)
        if lock:
            q = q.with_for_update()
        row = (await s.execute(q)).mappings().one_or_none()
        if row is None:
            raise ScopeViolation("submission unavailable")
        return dict(row)

    async def _record(self, s: AsyncSession, row: dict[str, Any]) -> SubmissionRecordV1:
        job_id = await s.scalar(
            select(jobs.c.id).where(
                jobs.c.org_id == row["org_id"],
                jobs.c.submission_id == row["id"],
                jobs.c.operation == "admit_source",
            )
        )
        return SubmissionRecordV1(
            **{
                k: row[k]
                for k in SubmissionRecordV1.model_fields
                if k not in {"schema_version", "job_id"}
            },
            job_id=job_id,
        )

    async def get(self, ctx: TenantContext, id: UUID) -> SubmissionRecordV1:
        async with transaction(self.factory) as s:
            row = await self._load(s, ctx, id)
            await require_repository(s, ctx.org_id, ctx.principal_id, row["repository_id"])
            return await self._record(s, row)

    async def reserve(
        self, ctx: TenantContext, request: UploadReservationV1, key: str
    ) -> SubmissionRecordV1:
        request = UploadReservationV1.model_validate(request.model_dump())
        if not 1 <= len(key.encode()) <= 128 or key.startswith(("job:", "child:")):
            raise ValueError("invalid idempotency key")
        digest = semantic_digest(
            {
                "source_kind": "upload_zip",
                "request": request.model_dump(),
                "policy_hash": self.policy.digest(),
            }
        )
        async with transaction(self.factory) as s:
            await lock_scope(s, ctx.org_id, ctx.principal_id, request.repository_id)
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
                await require_repository(
                    s, ctx.org_id, ctx.principal_id, old["repository_id"], write=True
                )
                if old["request_hash"] != digest:
                    raise DuplicateConflict("request key has different input")
                return await self._record(s, dict(old))
            quota = (
                (await s.execute(select(limits).where(limits.c.org_id == ctx.org_id)))
                .mappings()
                .one()
            )
            if quota["reserved_bytes"] + request.declared_size > quota["max_reserved_bytes"]:
                raise CapacityExceeded("upload byte quota reached")
            id = uuid4()
            await s.execute(
                insert(objects).values(
                    org_id=ctx.org_id,
                    id=uuid4(),
                    repository_id=request.repository_id,
                    key=f"quarantine/{ctx.org_id}/{request.repository_id}/{id}/source.zip",
                    kind="input",
                    state="reserved",
                    expires_at=func.clock_timestamp() + text("interval '24 hours'"),
                )
            )
            row = dict(
                (
                    await s.execute(
                        insert(submissions)
                        .values(
                            org_id=ctx.org_id,
                            id=id,
                            repository_id=request.repository_id,
                            principal_id=ctx.principal_id,
                            source_kind="upload_zip",
                            state="reserved",
                            request_key=key,
                            request_hash=digest,
                            reserved_bytes=request.declared_size,
                            declared_size=request.declared_size,
                            declared_sha=request.declared_sha256,
                            policy_hash=self.policy.digest(),
                            expires_at=func.clock_timestamp() + text("interval '24 hours'"),
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
                .values(reserved_bytes=limits.c.reserved_bytes + request.declared_size)
            )
            return await self._record(s, row)

    async def _stream(self, stream: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
        iterator = aiter(stream)
        while True:
            try:
                async with asyncio.timeout(10):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            yield chunk

    async def write_content(
        self, ctx: TenantContext, id: UUID, stream: AsyncIterable[bytes]
    ) -> SubmissionRecordV1:
        owner = uuid4().hex
        stage_id = uuid4()
        async with transaction(self.factory) as s:
            hint = await self._load(s, ctx, id)
            await lock_scope(s, ctx.org_id, ctx.principal_id, hint["repository_id"])
            sub = await self._load(s, ctx, id, lock=True)
            now = await database_now(s)
            replay = sub["state"] in {"admission_queued", "admitting", "admitted"}
            if not replay:
                if sub["state"] not in {"reserved", "uploading"} or sub["expires_at"] <= now:
                    raise InvalidTransition("submission is not uploadable")
                if sub["upload_owner"] is not None and sub["upload_expires_at"] > now:
                    raise InvalidTransition("upload already has an owner")
                concurrent = await s.scalar(
                    select(func.count())
                    .select_from(submissions)
                    .where(
                        submissions.c.org_id == ctx.org_id,
                        submissions.c.id != id,
                        submissions.c.state == "uploading",
                        submissions.c.upload_expires_at > now,
                    )
                )
                if concurrent:
                    raise CapacityExceeded("concurrent upload quota reached")
                await s.execute(
                    update(submissions)
                    .where(submissions.c.org_id == ctx.org_id, submissions.c.id == id)
                    .values(
                        state="uploading",
                        upload_owner=owner,
                        upload_expires_at=func.clock_timestamp() + text("interval '15 minutes'"),
                    )
                )
                stage_key = f"stage/{ctx.org_id}/{sub['repository_id']}/{id}/{owner}"
                await s.execute(
                    insert(objects).values(
                        org_id=ctx.org_id,
                        id=stage_id,
                        repository_id=sub["repository_id"],
                        key=stage_key,
                        kind="stage",
                        state="writing",
                        expires_at=func.clock_timestamp() + text("interval '1 hour'"),
                    )
                )
        if replay:
            digest = hashlib.sha256()
            size = 0
            async with asyncio.timeout(120):
                async for chunk in self._stream(stream):
                    size += len(chunk)
                    if size > sub["declared_size"]:
                        raise DuplicateConflict("replayed upload differs")
                    digest.update(chunk)
            if digest.hexdigest() != sub["actual_sha"] or size != sub["actual_size"]:
                raise DuplicateConflict("replayed upload differs")
            return await self.get(ctx, id)
        prefix = bytearray()

        async def observed() -> AsyncIterator[bytes]:
            async for chunk in self._stream(stream):
                if len(prefix) < 4:
                    prefix.extend(chunk[: 4 - len(prefix)])
                yield chunk

        try:
            async with asyncio.timeout(120):
                try:
                    stage = await self.store.put_stage(stage_key, observed(), sub["declared_size"])
                except ValueError as error:
                    raise UploadRejected("upload_length_mismatch") from error
            if stage.size_bytes != sub["declared_size"]:
                raise UploadRejected("upload_length_mismatch")
            if sub["declared_sha"] and stage.sha256 != sub["declared_sha"]:
                raise UploadRejected("upload_hash_mismatch")
            if bytes(prefix) not in {b"PK\x03\x04", b"PK\x05\x06"}:
                raise UploadRejected("unsupported_format")
            async with transaction(self.factory) as s:
                await s.execute(
                    update(objects)
                    .where(
                        objects.c.org_id == ctx.org_id,
                        objects.c.id == stage_id,
                        objects.c.state == "writing",
                    )
                    .values(state="verified", sha256=stage.sha256, size_bytes=stage.size_bytes)
                )
            input_key = f"quarantine/{ctx.org_id}/{sub['repository_id']}/{id}/source.zip"
            descriptor = await self.store.put_if_absent(input_key, stage)
            async with transaction(self.factory) as s:
                await lock_scope(s, ctx.org_id, ctx.principal_id, sub["repository_id"])
                current = await self._load(s, ctx, id, lock=True)
                now = await database_now(s)
                if (
                    current["state"] != "uploading"
                    or current["upload_owner"] != owner
                    or current["upload_expires_at"] <= now
                ):
                    raise InvalidTransition("upload ownership changed")
                obj = (
                    (
                        await s.execute(
                            select(objects)
                            .where(
                                objects.c.org_id == ctx.org_id,
                                objects.c.repository_id == sub["repository_id"],
                                objects.c.key == input_key,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                await s.execute(
                    update(objects)
                    .where(objects.c.org_id == ctx.org_id, objects.c.id == obj["id"])
                    .values(
                        state="verified",
                        sha256=descriptor.sha256,
                        size_bytes=descriptor.size_bytes,
                        version_id=descriptor.version_id,
                    )
                )
                row = dict(
                    (
                        await s.execute(
                            update(submissions)
                            .where(submissions.c.org_id == ctx.org_id, submissions.c.id == id)
                            .values(
                                state="admission_queued",
                                actual_sha=descriptor.sha256,
                                actual_size=descriptor.size_bytes,
                                input_object_id=obj["id"],
                                upload_owner=None,
                                upload_expires_at=None,
                            )
                            .returning(submissions)
                        )
                    )
                    .mappings()
                    .one()
                )
                await self.jobs._enqueue(
                    s,
                    ctx,
                    EnqueueJobV2(
                        repository_id=sub["repository_id"],
                        submission_id=id,
                        operation="admit_source",
                        request_key=f"job:submission:{id}",
                        config_hash=sub["policy_hash"],
                    ),
                )
                return await self._record(s, row)
        except UploadRejected as error:
            async with transaction(self.factory) as s:
                await lock_scope(s, ctx.org_id, ctx.principal_id, sub["repository_id"])
                current = await self._load(s, ctx, id, lock=True)
                if current["state"] == "uploading" and current["upload_owner"] == owner:
                    await self.jobs.release_reservation(
                        s,
                        {"org_id": ctx.org_id, "submission_id": id, "operation": "admit_source"},
                        "rejected",
                        error.code,
                    )
            raise

    async def cancel(self, ctx: TenantContext, id: UUID) -> SubmissionRecordV1:
        async with transaction(self.factory) as s:
            hint = await self._load(s, ctx, id)
            await lock_scope(s, ctx.org_id, ctx.principal_id, hint["repository_id"])
            job = (
                (
                    await s.execute(
                        select(jobs)
                        .where(
                            jobs.c.org_id == ctx.org_id,
                            jobs.c.submission_id == id,
                            jobs.c.operation == "admit_source",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            row = await self._load(s, ctx, id, lock=True)
            if row["state"] not in {"admitted", "rejected", "failed", "expired", "cancelled"}:
                if job is not None:
                    attempts = PHASE2_TABLES["job_attempts"]
                    attempt = (
                        (
                            await s.execute(
                                select(attempts)
                                .where(
                                    attempts.c.org_id == ctx.org_id,
                                    attempts.c.job_id == job["id"],
                                    attempts.c.id == job["current_attempt_id"],
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    await self.jobs._finish_failure(
                        s,
                        dict(job),
                        dict(attempt) if attempt else None,
                        "user_requested",
                        transient=False,
                        cancel=True,
                    )
                else:
                    await self.jobs.release_reservation(
                        s,
                        {"org_id": ctx.org_id, "submission_id": id, "operation": "admit_source"},
                        "cancelled",
                        "user_requested",
                    )
            return await self._record(s, await self._load(s, ctx, id))
