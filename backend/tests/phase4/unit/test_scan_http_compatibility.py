"""Internal scan jobs cannot escape through legacy HTTP response contracts."""

from uuid import UUID

import pytest
from app.contracts.scan_jobs import ScanJobRecordV4
from fastapi import HTTPException

from tests.phase4.dependency_fixtures import NOW


def test_legacy_boundary_rejects_scan_record_instead_of_schema_500():
    from app.api import application

    assert hasattr(application, "legacy_job_record"), "legacy scan guard missing"
    record = ScanJobRecordV4(
        org_id=UUID(int=1),
        id=UUID(int=2),
        repository_id=UUID(int=3),
        snapshot_id=UUID(int=4),
        scan_spec_id=UUID(int=5),
        state="queued",
        attempt_count=0,
        max_attempts=3,
        deadline=NOW,
        failure_code=None,
        result_run_id=None,
        created_at=NOW,
        finished_at=None,
    )
    with pytest.raises(HTTPException) as error:
        application.legacy_job_record(record)
    assert error.value.status_code == 409
    assert error.value.detail == "scan_operation_unavailable"
