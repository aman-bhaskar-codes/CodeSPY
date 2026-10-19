"""Durable wakeup intent, inserted in the same transaction as a job change."""

from typing import Any
from uuid import uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.jobs import JobEnvelopeV2
from app.models.phase2_tables import PHASE2_TABLES


async def enqueue_wakeup(s: AsyncSession, job: dict[str, Any]) -> None:
    envelope = JobEnvelopeV2(
        org_id=job["org_id"],
        job_id=job["id"],
        operation=job["operation"],
        dispatch_generation=job["dispatch_generation"],
        config_hash=job["config_hash"],
        deadline=job["deadline"],
    )
    await s.execute(
        insert(PHASE2_TABLES["outbox"]).values(
            org_id=job["org_id"],
            id=uuid4(),
            job_id=job["id"],
            kind="wakeup",
            generation=job["dispatch_generation"],
            payload=envelope.model_dump(mode="json"),
            next_send_at=job["not_before"],
        )
    )
