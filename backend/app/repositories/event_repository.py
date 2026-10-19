"""Public events are allocated under the owning job's lock."""

from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.phase2_tables import PHASE2_TABLES
from app.repositories.job_repository import jobs


async def append_event(
    s: AsyncSession, job: dict[str, Any], event_type: str, payload: dict[str, Any] | None = None
) -> None:
    sequence = await s.scalar(
        update(jobs)
        .where(jobs.c.org_id == job["org_id"], jobs.c.id == job["id"])
        .values(event_sequence=jobs.c.event_sequence + 1)
        .returning(jobs.c.event_sequence)
    )
    await s.execute(
        insert(PHASE2_TABLES["job_events"]).values(
            org_id=job["org_id"],
            job_id=job["id"],
            sequence=sequence,
            event_type=event_type,
            public_payload=payload or {},
        )
    )
