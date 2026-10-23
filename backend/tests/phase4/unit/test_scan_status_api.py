"""Authenticated scan status contracts; job execution is a transport test double."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.jobs import JobRecordV2
from app.contracts.scan_jobs import ScanJobRecordV4
from app.models.phase2_tables import PHASE2_TABLES
from app.security.sessions import SessionService
from app.services.jobs import JobService
from app.services.scan_jobs import ScanJobService
from tests.phase4.unit.test_findings_authorization import authority as authority_fixture
from tests.phase4.unit.test_scan_config import config

authority = authority_fixture


@pytest_asyncio.fixture
async def status_client(authority, monkeypatch):
    async def fixture_now(_session):
        return datetime.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr("app.security.sessions.database_now", fixture_now)
    issued = await SessionService(authority.factory).issue(authority.ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(authority.factory, None, authority.ctx, scan_capabilities=object()),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token, "codespy_dev_csrf": issued.csrf},
        headers={"origin": "http://testserver", "x-csrf-token": issued.csrf},
    ) as client:
        yield client


def job_record(authority, **changes):
    now = datetime.now(UTC)
    return ScanJobRecordV4(
        org_id=authority.ctx.org_id,
        id=uuid4(),
        repository_id=authority.repository_id,
        snapshot_id=authority.snapshot_id,
        scan_spec_id=uuid4(),
        state="queued",
        attempt_count=0,
        max_attempts=3,
        deadline=now,
        failure_code=None,
        result_run_id=None,
        created_at=now,
        finished_at=None,
    ).model_copy(update=changes)


@pytest.mark.parametrize("alias", [False, True])
async def test_creation_status_url_resolves_job_before_publication(
    authority, status_client, monkeypatch, tmp_path, alias
):
    record = job_record(authority)
    selection, _ = config(tmp_path)

    async def create(_self, ctx, repository_id, snapshot_id, value, request_key):
        assert ctx == authority.ctx
        assert (repository_id, snapshot_id) == (record.repository_id, record.snapshot_id)
        return SimpleNamespace(id=record.scan_spec_id), record

    async def get(_self, ctx, job_id):
        assert ctx == authority.ctx and job_id == record.id
        return record

    monkeypatch.setattr(ScanJobService, "create", create)
    monkeypatch.setattr(JobService, "get", get)
    base = f"/api/v1/repositories/{record.repository_id}"
    path = f"{base}/scans" if alias else f"{base}/snapshots/{record.snapshot_id}/scans"
    accepted = await status_client.post(
        path,
        json={"snapshot_id": str(record.snapshot_id), "config": selection.model_dump(mode="json")},
        headers={"idempotency-key": "status-test"},
    )
    assert accepted.status_code == 202, accepted.text
    status = await status_client.get(accepted.json()["status_url"])
    assert status.status_code == 200, status.text
    assert status.json()["id"] == str(record.id)
    assert status.json()["state"] == "queued"
    assert status.json()["result_run_id"] is None


@pytest.mark.parametrize("mismatch", [None, "repository_id", "snapshot_id", "org_id", "revoked"])
async def test_status_preserves_run_identity_and_rejects_foreign_scope(
    authority, status_client, monkeypatch, mismatch
):
    run_id = uuid4()
    record = job_record(authority, state="succeeded", result_run_id=run_id, report_available=True)
    if mismatch == "revoked":
        async with authority.factory() as session, session.begin():
            await session.execute(
                sa.update(PHASE2_TABLES["repository_grants"]).values(revoked_at=datetime.now(UTC))
            )
    elif mismatch:
        record = record.model_copy(update={mismatch: uuid4()})

    async def get(_self, ctx, job_id):
        return record

    monkeypatch.setattr(JobService, "get", get)
    path = (
        f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}"
        f"/scan-jobs/{record.id}"
    )
    response = await status_client.get(path)
    if mismatch:
        assert response.status_code == 404, response.text
    else:
        assert response.status_code == 200, response.text
        assert response.json()["result_run_id"] == str(run_id)
        assert response.json()["id"] != str(run_id)


async def test_status_requires_authenticated_session(authority, status_client):
    status_client.cookies.clear()
    response = await status_client.get(
        f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}"
        f"/scan-jobs/{uuid4()}"
    )
    assert response.status_code == 401


async def test_status_rejects_other_job_operations(authority, status_client, monkeypatch):
    scan = job_record(authority)
    data = scan.model_dump(exclude={"schema_version", "scan_spec_id", "report_available"})
    data.update(operation="inventory_snapshot", submission_id=None)
    inventory = JobRecordV2(**data)

    async def get(_self, ctx, job_id):
        return inventory

    monkeypatch.setattr(JobService, "get", get)
    response = await status_client.get(
        f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}"
        f"/scan-jobs/{inventory.id}"
    )
    assert response.status_code == 404
