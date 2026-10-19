"""Bounded event retention keeps the latest durable outcome and cursor floor."""

from datetime import timedelta

from app.db.transaction import transaction
from app.models.phase2_tables import PHASE2_TABLES
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class RetentionWorker:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def tick(self) -> int:
        events, jobs = (PHASE2_TABLES[name] for name in ["job_events", "jobs"])
        async with transaction(self.factory) as s:
            candidates = (
                await s.execute(
                    select(events.c.org_id, events.c.job_id)
                    .join(jobs, (jobs.c.org_id == events.c.org_id) & (jobs.c.id == events.c.job_id))
                    .where(
                        events.c.created_at < func.clock_timestamp() - timedelta(days=7),
                        events.c.sequence < jobs.c.event_sequence,
                    )
                    .distinct()
                    .order_by(events.c.org_id, events.c.job_id)
                    .limit(100)
                )
            ).all()
        removed = 0
        for org, job in candidates:
            async with transaction(self.factory) as s:
                current = (
                    (
                        await s.execute(
                            select(jobs)
                            .where(jobs.c.org_id == org, jobs.c.id == job)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is None:
                    continue
                sequences = (
                    await s.scalars(
                        select(events.c.sequence)
                        .where(
                            events.c.org_id == org,
                            events.c.job_id == job,
                            events.c.sequence < current["event_sequence"],
                            events.c.created_at < func.clock_timestamp() - timedelta(days=7),
                        )
                        .order_by(events.c.sequence)
                        .limit(100 - removed)
                    )
                ).all()
                if sequences:
                    await s.execute(
                        delete(events).where(
                            events.c.org_id == org,
                            events.c.job_id == job,
                            events.c.sequence.in_(sequences),
                        )
                    )
                    minimum = await s.scalar(
                        select(func.min(events.c.sequence)).where(
                            events.c.org_id == org, events.c.job_id == job
                        )
                    )
                    await s.execute(
                        update(jobs)
                        .where(jobs.c.org_id == org, jobs.c.id == job)
                        .values(events_min_sequence=minimum or current["event_sequence"] + 1)
                    )
                    removed += len(sequences)
            if removed >= 100:
                break
        # These tables contain expiring authentication/deduplication metadata,
        # not immutable source or result history. Each sweep deletes <=100/table.
        async with transaction(self.factory) as s:
            flows, sessions, receipts = (
                PHASE2_TABLES[name] for name in ["oauth_flows", "sessions", "webhook_receipts"]
            )
            for table, column, predicate in [
                (flows, flows.c.id, flows.c.expires_at <= func.clock_timestamp()),
                (sessions, sessions.c.id, sessions.c.idle_expires_at <= func.clock_timestamp()),
                (
                    receipts,
                    receipts.c.delivery_id,
                    receipts.c.received_at < func.clock_timestamp() - timedelta(days=7),
                ),
            ]:
                due = select(column).where(predicate).order_by(column).limit(100)
                await s.execute(delete(table).where(column.in_(due)))
        return removed
