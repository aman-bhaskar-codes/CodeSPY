"""Scoped job reads and transaction-local worker context; no transaction ownership."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.indexing import IndexJobRecordV5
from app.contracts.investigation_jobs import InvestigationJobRecordV1
from app.contracts.jobs import AttemptLeaseV2, JobRecordV2, JobRecordV3
from app.contracts.scan_jobs import ScanJobRecordV4
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
    if res.get("operation") == "index_snapshot":
        from app.models.index_tables import index_jobs

        projection = (
            (
                await s.execute(
                    select(index_jobs).where(
                        index_jobs.c.org_id == org_id, index_jobs.c.id == job_id
                    )
                )
            )
            .mappings()
            .one()
        )
        # Refresh lifecycle and publication fields from the same statement snapshot.
        res = dict(projection)
    if res.get("operation") == "scan_snapshot":
        from app.models.scan_tables import scan_jobs, scan_reports

        res["scan_spec_id"] = await s.scalar(
            select(scan_jobs.c.scan_spec_id).where(
                scan_jobs.c.org_id == org_id,
                scan_jobs.c.id == job_id,
            )
        )
        res["report_available"] = row["state"] in {"succeeded", "partial"} and (
            await s.scalar(
                select(scan_reports.c.run_id).where(
                    scan_reports.c.org_id == org_id,
                    scan_reports.c.snapshot_id == row["snapshot_id"],
                    scan_reports.c.run_id == row["result_run_id"],
                )
            )
            is not None
        )
    if res.get("operation") == "investigate_snapshot":
        from app.models.investigation_execution_tables import investigation_jobs

        projection = (
            (
                await s.execute(
                    select(investigation_jobs).where(
                        investigation_jobs.c.org_id == org_id, investigation_jobs.c.id == job_id
                    )
                )
            )
            .mappings()
            .one()
        )
        res = dict(projection)
    if res.get("operation") == "analyze_structure":
        from app.models.phase3_tables import structural_jobs

        spec_id = await s.scalar(
            select(structural_jobs.c.analysis_spec_id).where(
                structural_jobs.c.org_id == org_id, structural_jobs.c.id == job_id
            )
        )
        res["analysis_spec_id"] = spec_id
    for k, v in list(res.items()):
        if isinstance(v, datetime) and v.tzinfo is None:
            res[k] = v.replace(tzinfo=UTC)
    return res


def job_record(row: dict[str, Any]) -> JobRecordV2:
    return JobRecordV2(**{k: row[k] for k in JobRecordV2.model_fields if k in row})


def job_record_v3(row: dict[str, Any]) -> JobRecordV3:
    data = {k: row[k] for k in JobRecordV3.model_fields if k in row}
    data["report_available"] = (
        row["state"] in {"succeeded", "partial"} and row["result_run_id"] is not None
    )
    return JobRecordV3(**data)


def scan_job_record(row: dict[str, Any]) -> ScanJobRecordV4:
    return ScanJobRecordV4(**{k: row[k] for k in ScanJobRecordV4.model_fields if k in row})


def index_job_record(row: dict[str, Any]) -> IndexJobRecordV5:
    return IndexJobRecordV5(**{k: row[k] for k in IndexJobRecordV5.model_fields if k in row})


def _ensure_utc(dt: Any) -> Any:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            return dt
    return dt


def investigation_job_record(row: dict[str, Any]) -> InvestigationJobRecordV1:
    return InvestigationJobRecordV1(
        org_id=row["org_id"],
        id=row["id"],
        repository_id=row["repository_id"],
        investigation_id=row["investigation_id"],
        spec_hash=row["spec_hash"],
        operation="investigate_snapshot",
        state=row["state"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        deadline=_ensure_utc(row["deadline"]),
        failure_code=row.get("failure_code"),
        report_id=row.get("result_report_id"),
        created_at=_ensure_utc(row["created_at"]),
        finished_at=_ensure_utc(row["finished_at"]) if row.get("finished_at") is not None else None,
    )


def public_job_record(
    row: dict[str, Any],
) -> JobRecordV2 | JobRecordV3 | ScanJobRecordV4 | IndexJobRecordV5 | InvestigationJobRecordV1:
    if row["operation"] == "index_snapshot":
        return index_job_record(row)
    if row["operation"] == "scan_snapshot":
        return scan_job_record(row)
    if row["operation"] == "investigate_snapshot":
        return investigation_job_record(row)
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
