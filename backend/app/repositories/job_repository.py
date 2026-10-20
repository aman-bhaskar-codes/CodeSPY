"""Scoped job reads and transaction-local worker context; no transaction ownership."""

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.jobs import AttemptLeaseV2, JobRecordV2, JobRecordV3
from app.domain.errors import ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES

jobs = PHASE2_TABLES["jobs"]
attempts = PHASE2_TABLES["job_attempts"]


async def load_job(
    s: AsyncSession, org_id: UUID, job_id: UUID, *, lock: bool = False
) -> dict[str, Any]:
    q = select(jobs).where(jobs.c.org_id == org_id, jobs.c.id == job_id)
    if lock:
        q = q.with_for_update()
    row = (await s.execute(q)).mappings().one_or_none()
    if row is None:
        raise ScopeViolation("job unavailable")
    res: dict[str, Any] = dict(row)
    if res.get("operation") == "analyze_structure":
        from app.models.phase3_tables import structural_jobs

        spec_id = await s.scalar(
            select(structural_jobs.c.analysis_spec_id).where(
                structural_jobs.c.org_id == org_id, structural_jobs.c.id == job_id
            )
        )
        res["analysis_spec_id"] = spec_id
    return res


def job_record(row: dict[str, Any]) -> JobRecordV2:
    return JobRecordV2(**{k: row[k] for k in JobRecordV2.model_fields if k in row})


def job_record_v3(row: dict[str, Any]) -> JobRecordV3:
    data = {k: row[k] for k in JobRecordV3.model_fields if k in row}
    data["report_available"] = (
        row["state"] in {"succeeded", "partial"} and row["result_run_id"] is not None
    )
    return JobRecordV3(**data)


def public_job_record(row: dict[str, Any]) -> JobRecordV2 | JobRecordV3:
    return job_record_v3(row) if row["operation"] == "analyze_structure" else job_record(row)


async def worker_context(s: AsyncSession, lease: AttemptLeaseV2) -> None:
    for key, value in [
        ("org_id", lease.org_id),
        ("job_id", lease.job_id),
        ("attempt_id", lease.attempt_id),
        ("fence", lease.fence),
    ]:
        await s.execute(
            text("SELECT set_config(:key,:value,true)"),
            {"key": "codespy." + key, "value": str(value)},
        )
