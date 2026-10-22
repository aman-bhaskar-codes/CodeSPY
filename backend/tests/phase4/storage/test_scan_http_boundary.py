"""Authenticated legacy routes reject internal scans without changing their state."""

from uuid import uuid4

import httpx
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.security.sessions import SessionService
from app.services.jobs import JobService
from app.services.scan_jobs import ScanJobService
from tests.phase4.storage.test_scan_jobs import Registry, scenario


async def test_legacy_routes_reject_scan_before_cancel_or_retry(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, snapshot, repo, store, config = await scenario(migration_factory, tmp_path)
    _, job = await ScanJobService(api_factory, Registry()).create(
        ctx, repo, snapshot, config, "http"
    )
    issued = await SessionService(migration_factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(api_factory, store, ctx),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token, "codespy_dev_csrf": issued.csrf},
    ) as client:
        headers = {
            "origin": "http://testserver",
            "x-csrf-token": issued.csrf,
            "idempotency-key": "unsupported-retry",
        }
        for method, suffix in (
            ("GET", ""),
            ("GET", "/events"),
            ("POST", "/cancel"),
            ("POST", "/retry"),
        ):
            response = await client.request(
                method, f"/api/v1/jobs/{job.id}{suffix}", headers=headers
            )
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "scan_operation_unavailable"
        assert (await client.get(f"/api/v1/jobs/{uuid4()}")).status_code == 404
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "queued"
