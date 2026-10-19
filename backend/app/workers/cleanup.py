"""Ledger-owned exact-version cleanup, serialized with reference creation."""

import logging
from datetime import timedelta
from uuid import uuid4

from app.db.transaction import transaction
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.object_store import ObjectStore
from app.services.job_finalization import descriptor
from app.services.jobs import TERMINAL
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class CleanupWorker:
    def __init__(self, factory: async_sessionmaker[AsyncSession], store: ObjectStore):
        self.factory, self.store = factory, store

    async def tick(self) -> int:
        tasks, objects, submissions, jobs, limits = (
            PHASE2_TABLES[name]
            for name in [
                "cleanup_tasks",
                "object_records",
                "source_submissions",
                "jobs",
                "org_work_limits",
            ]
        )
        async with transaction(self.factory) as s:
            candidates = (
                await s.execute(
                    select(tasks.c.org_id, tasks.c.id)
                    .where(
                        tasks.c.next_attempt_at <= func.clock_timestamp(),
                        or_(
                            tasks.c.state == "pending",
                            and_(
                                tasks.c.state == "claimed",
                                tasks.c.claim_expires_at <= func.clock_timestamp(),
                            ),
                        ),
                    )
                    .order_by(tasks.c.org_id, tasks.c.id)
                    .limit(10)
                )
            ).all()
        changed = 0
        for org, identity in candidates:
            token = uuid4().hex
            async with transaction(self.factory) as s:
                await s.execute(
                    select(limits.c.org_id).where(limits.c.org_id == org).with_for_update()
                )
                task = (
                    (
                        await s.execute(
                            select(tasks)
                            .where(tasks.c.org_id == org, tasks.c.id == identity)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                now = await s.scalar(select(func.clock_timestamp()))
                if task["state"] not in {"pending", "claimed"} or (
                    task["state"] == "claimed" and task["claim_expires_at"] > now
                ):
                    continue
                obj = (
                    (
                        await s.execute(
                            select(objects)
                            .where(objects.c.org_id == org, objects.c.id == task["object_id"])
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                canonical = objects.alias("retained_canonical")
                canonical_exists = (
                    select(canonical.c.id)
                    .where(
                        canonical.c.org_id == org,
                        canonical.c.repository_id == objects.c.repository_id,
                        canonical.c.kind == "source",
                        canonical.c.state == "available",
                        canonical.c.sha256 == objects.c.sha256,
                        canonical.c.size_bytes == objects.c.size_bytes,
                    )
                    .correlate(objects)
                    .exists()
                )
                referenced = await s.scalar(
                    select(submissions.c.id)
                    .where(
                        submissions.c.org_id == org,
                        submissions.c.input_object_id == obj["id"],
                        or_(
                            submissions.c.state.not_in(
                                ["failed", "rejected", "expired", "cancelled", "admitted"]
                            ),
                            and_(submissions.c.state == "admitted", ~canonical_exists),
                            objects.c.expires_at > func.clock_timestamp(),
                        ),
                    )
                    .select_from(
                        submissions.join(
                            objects,
                            and_(
                                objects.c.org_id == submissions.c.org_id,
                                objects.c.id == submissions.c.input_object_id,
                            ),
                        )
                    )
                    .limit(1)
                )
                active_owner = await s.scalar(
                    select(jobs.c.id)
                    .where(
                        jobs.c.org_id == org,
                        jobs.c.id == obj["owner_job_id"],
                        jobs.c.state.not_in(TERMINAL),
                    )
                    .limit(1)
                )
                if referenced or active_owner or obj["state"] == "available":
                    await s.execute(
                        update(tasks)
                        .where(tasks.c.org_id == org, tasks.c.id == identity)
                        .values(
                            state="pending",
                            next_attempt_at=func.clock_timestamp() + timedelta(seconds=15),
                            claim_token=None,
                            claim_expires_at=None,
                        )
                    )
                    continue
                if obj["state"] == "deleted":
                    await s.execute(
                        update(tasks)
                        .where(tasks.c.org_id == org, tasks.c.id == identity)
                        .values(state="done", claim_token=None, claim_expires_at=None)
                    )
                    changed += 1
                    continue
                selected = (
                    descriptor(dict(obj))
                    if obj["state"] not in {"reserved", "writing"}
                    and obj["sha256"] is not None
                    and obj["size_bytes"] is not None
                    else None
                )
                await s.execute(
                    update(objects)
                    .where(objects.c.org_id == org, objects.c.id == obj["id"])
                    .values(state="deleting")
                )
                await s.execute(
                    update(tasks)
                    .where(tasks.c.org_id == org, tasks.c.id == identity)
                    .values(
                        state="claimed",
                        claim_token=token,
                        claim_expires_at=func.clock_timestamp() + timedelta(seconds=60),
                        attempt_count=tasks.c.attempt_count + 1,
                    )
                )
            failed = False
            try:
                if selected is None:
                    selected = await self.store.describe_owned(obj["key"], 104857600)
                    if selected is not None:
                        if (obj["sha256"] is not None and selected.sha256 != obj["sha256"]) or (
                            obj["size_bytes"] is not None
                            and selected.size_bytes != obj["size_bytes"]
                        ):
                            raise ValueError("owned orphan descriptor differs")
                        async with transaction(self.factory) as s:
                            # The object is fenced as deleting before discovery; attaching
                            # a new submission/publication reference is forbidden.
                            await s.execute(
                                update(objects)
                                .where(
                                    objects.c.org_id == org,
                                    objects.c.id == obj["id"],
                                    objects.c.state == "deleting",
                                )
                                .values(
                                    sha256=selected.sha256,
                                    size_bytes=selected.size_bytes,
                                    version_id=selected.version_id,
                                )
                            )
                if selected is not None:
                    await self.store.delete_exact(selected)
            except Exception:
                failed = True
            async with transaction(self.factory) as s:
                await s.execute(
                    select(limits.c.org_id).where(limits.c.org_id == org).with_for_update()
                )
                current = (
                    (
                        await s.execute(
                            select(tasks)
                            .where(tasks.c.org_id == org, tasks.c.id == identity)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if current["claim_token"] != token:
                    continue
                exhausted = failed and current["attempt_count"] >= 5
                await s.execute(
                    update(tasks)
                    .where(tasks.c.org_id == org, tasks.c.id == identity)
                    .values(
                        state=("exhausted" if exhausted else "pending") if failed else "done",
                        claim_token=None,
                        claim_expires_at=None,
                        next_attempt_at=func.clock_timestamp() + timedelta(seconds=15),
                        last_error_code="storage_unavailable" if failed else None,
                    )
                )
                if not failed:
                    await s.execute(
                        update(objects)
                        .where(
                            objects.c.org_id == org,
                            objects.c.id == task["object_id"],
                            objects.c.state == "deleting",
                        )
                        .values(state="deleted")
                    )
                if exhausted:
                    logger.warning("cleanup_attempts_exhausted")
                changed += 1
        return changed
