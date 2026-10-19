"""Independent DB-time recovery; queue availability is not a prerequisite."""

from datetime import timedelta
from uuid import UUID, uuid4

from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.repositories.job_repository import attempts
from app.repositories.outbox_repository import enqueue_wakeup
from app.security.authorization import lock_scope, require_repository
from app.services.jobs import TERMINAL, JobService
from app.workers.retention import RetentionWorker
from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class RecoveryWorker:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory
        self.jobs = JobService(factory)
        self._authority_cursor: tuple[UUID, UUID] | None = None

    async def tick(self) -> int:
        changed = await self.jobs.recover_due()
        await RetentionWorker(self.factory).tick()
        submissions = PHASE2_TABLES["source_submissions"]
        jobs = PHASE2_TABLES["jobs"]
        objects = PHASE2_TABLES["object_records"]
        tasks = PHASE2_TABLES["cleanup_tasks"]
        async with transaction(self.factory) as s:
            pending = (
                await s.execute(
                    select(jobs.c.org_id, jobs.c.id)
                    .where(
                        jobs.c.state.not_in(TERMINAL),
                        (
                            tuple_(jobs.c.org_id, jobs.c.id)
                            > tuple_(
                                literal(self._authority_cursor[0]),
                                literal(self._authority_cursor[1]),
                            )
                        )
                        if self._authority_cursor is not None
                        else literal(True),
                    )
                    .order_by(jobs.c.org_id, jobs.c.id)
                    .limit(100)
                )
            ).all()
        if not pending:
            self._authority_cursor = None
        for org, identity in pending:
            self._authority_cursor = (org, identity)
            async with transaction(self.factory) as s:
                job, _ = await self.jobs._locked(s, org, identity, access=False)
                if job["state"] in TERMINAL:
                    continue
                if job["state"] == "cancelling":
                    attempt = (
                        (
                            await s.execute(
                                select(attempts)
                                .where(
                                    attempts.c.org_id == org,
                                    attempts.c.job_id == identity,
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
                        job,
                        dict(attempt) if attempt else None,
                        "user_requested",
                        transient=False,
                        cancel=True,
                    )
                    changed += 1
                    continue
                try:
                    await require_repository(
                        s, org, job["principal_id"], job["repository_id"], write=True, lock=True
                    )
                except ScopeViolation:
                    attempt = (
                        (
                            await s.execute(
                                select(attempts)
                                .where(
                                    attempts.c.org_id == org,
                                    attempts.c.job_id == identity,
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
                        job,
                        dict(attempt) if attempt else None,
                        "access_revoked",
                        transient=False,
                        cancel=True,
                    )
                    changed += 1
                    continue
                now = await database_now(s)
                if job["state"] == "queued" and job["deadline"] > now:
                    outbox = PHASE2_TABLES["outbox"]
                    wakeup = (
                        (
                            await s.execute(
                                select(outbox)
                                .where(
                                    outbox.c.org_id == org,
                                    outbox.c.job_id == identity,
                                    outbox.c.kind == "wakeup",
                                    outbox.c.generation == job["dispatch_generation"],
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    stranded = (
                        wakeup is None and job["created_at"] < now - timedelta(seconds=180)
                    ) or (
                        wakeup is not None
                        and wakeup["state"] == "sent"
                        and wakeup["sent_at"] < now - timedelta(seconds=180)
                    )
                    if stranded:
                        refreshed = dict(
                            (
                                await s.execute(
                                    update(jobs)
                                    .where(jobs.c.org_id == org, jobs.c.id == identity)
                                    .values(dispatch_generation=jobs.c.dispatch_generation + 1)
                                    .returning(jobs)
                                )
                            )
                            .mappings()
                            .one()
                        )
                        await enqueue_wakeup(s, refreshed)
                        changed += 1
        async with transaction(self.factory) as s:
            rows = (
                (
                    await s.execute(
                        select(submissions)
                        .where(
                            submissions.c.state.in_(["reserved", "uploading", "uploaded"]),
                            submissions.c.expires_at <= func.clock_timestamp(),
                            ~select(jobs.c.id)
                            .where(
                                jobs.c.org_id == submissions.c.org_id,
                                jobs.c.submission_id == submissions.c.id,
                            )
                            .exists(),
                        )
                        .order_by(submissions.c.org_id, submissions.c.id)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            async with transaction(self.factory) as s:
                await lock_scope(
                    s, row["org_id"], row["principal_id"], row["repository_id"], access=False
                )
                current = (
                    (
                        await s.execute(
                            select(submissions)
                            .where(
                                submissions.c.org_id == row["org_id"], submissions.c.id == row["id"]
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if current["state"] not in {"reserved", "uploading", "uploaded"}:
                    continue
                await self.jobs.release_reservation(
                    s,
                    {
                        "org_id": row["org_id"],
                        "submission_id": row["id"],
                        "operation": "admit_source",
                    },
                    "expired",
                    "deadline_exceeded",
                )
                # Reserved inputs are named by submission; stage rows expire independently.
                await s.execute(
                    update(objects)
                    .where(
                        objects.c.org_id == row["org_id"],
                        objects.c.key
                        == (
                            f"quarantine/{row['org_id']}/{row['repository_id']}/"
                            f"{row['id']}/source.zip"
                        ),
                    )
                    .values(expires_at=func.clock_timestamp())
                )
                changed += 1
        async with transaction(self.factory) as s:
            due = (
                (
                    await s.execute(
                        select(objects)
                        .where(
                            objects.c.expires_at <= func.clock_timestamp(),
                            objects.c.state.not_in(["available", "deleted", "deleting"]),
                        )
                        .order_by(objects.c.org_id, objects.c.id)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
            for obj in due:
                await s.execute(
                    insert(tasks)
                    .values(
                        org_id=obj["org_id"],
                        id=uuid4(),
                        object_id=obj["id"],
                        reason="retention_expired",
                    )
                    .on_conflict_do_update(
                        index_elements=[tasks.c.org_id, tasks.c.object_id, tasks.c.reason],
                        set_={"state": "pending", "next_attempt_at": func.clock_timestamp()},
                        where=tasks.c.state == "done",
                    )
                )
        return changed
