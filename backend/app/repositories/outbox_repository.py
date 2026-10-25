"""Durable wakeup intent, inserted in the same transaction as a job change."""

from typing import Any
from uuid import uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.indexing import JobEnvelopeV5
from app.contracts.jobs import JobEnvelopeV2, JobEnvelopeV3
from app.contracts.scan_jobs import JobEnvelopeV4
from app.models.phase2_tables import PHASE2_TABLES


async def enqueue_wakeup(s: AsyncSession, job: dict[str, Any]) -> None:
    envelope: JobEnvelopeV2 | JobEnvelopeV3 | JobEnvelopeV4 | JobEnvelopeV5
    if job["operation"] == "index_snapshot":
        from app.repositories.job_repository import load_job

        current = await load_job(s, job["org_id"], job["id"])
        envelope = JobEnvelopeV5(
            org_id=job["org_id"],
            job_id=job["id"],
            index_spec_id=current["index_spec_id"],
            dispatch_generation=job["dispatch_generation"],
            config_hash=job["config_hash"],
            deadline=job["deadline"],
        )
    elif job["operation"] == "scan_snapshot":
        from app.repositories.job_repository import load_job

        current = await load_job(s, job["org_id"], job["id"])
        envelope = JobEnvelopeV4(
            org_id=job["org_id"],
            job_id=job["id"],
            scan_spec_id=current["scan_spec_id"],
            dispatch_generation=job["dispatch_generation"],
            config_hash=job["config_hash"],
            deadline=job["deadline"],
        )
    elif job["operation"] == "analyze_structure":
        envelope = JobEnvelopeV3(
            org_id=job["org_id"],
            job_id=job["id"],
            operation="analyze_structure",
            dispatch_generation=job["dispatch_generation"],
            config_hash=job["config_hash"],
            deadline=job["deadline"],
            analysis_spec_id=job.get("analysis_spec_id"),
        )
    else:
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
