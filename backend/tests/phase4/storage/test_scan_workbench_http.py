"""Real HTTP/database scan selection and lifecycle; scanner registry is synthetic."""

from uuid import uuid4

import httpx
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.security.sessions import SessionService

from tests.phase4.storage.test_scan_jobs import Registry, scenario


async def test_scoped_scan_configuration_cancel_retry_and_replay(
    api_factory, migration_factory, tmp_path
):
    ctx, snapshot, repo, store, config = await scenario(migration_factory, tmp_path)
    issued = await SessionService(migration_factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            api_factory, store, ctx, scan_capabilities=Registry(), scan_defaults=config
        ),
    )
    base = f"/api/v1/repositories/{repo}/snapshots/{snapshot}"
    headers = {
        "origin": "http://testserver",
        "x-csrf-token": issued.csrf,
        "idempotency-key": "workbench-create",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token, "codespy_dev_csrf": issued.csrf},
    ) as client:
        metadata = await client.get(f"/api/v1/repositories/{repo}/metadata")
        assert metadata.status_code == 200
        assert metadata.json()["id"] == str(repo)
        assert (await client.get(f"/api/v1/repositories/{uuid4()}/metadata")).status_code == 404
        assert (
            await client.get(f"/api/v1/snapshots/{snapshot}/inventory?repository_id={repo}")
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/snapshots/{snapshot}/inventory?repository_id={uuid4()}")
        ).status_code == 404
        preview = await client.get(base + "/scan-configuration")
        assert preview.status_code == 200, preview.text
        assert preview.json()["configured"] is True
        assert preview.json()["live_access_verified"] is False
        body = {"config": preview.json()["config"]}
        denied = await client.post(base + "/scans", json=body)
        assert denied.status_code == 403
        created = await client.post(base + "/scans", headers=headers, json=body)
        assert created.status_code == 202, created.text
        assert (
            await client.post(base + "/scans", headers=headers, json=body)
        ).json() == created.json()
        job = created.json()["job_id"]
        wrong = await client.post(
            f"/api/v1/repositories/{repo}/snapshots/{uuid4()}/scan-jobs/{job}/cancel",
            headers=headers,
        )
        assert wrong.status_code == 404
        cancelled = await client.post(base + f"/scan-jobs/{job}/cancel", headers=headers)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "cancelled"
        retry_headers = {**headers, "idempotency-key": "workbench-retry"}
        retried = await client.post(base + f"/scan-jobs/{job}/retry", headers=retry_headers)
        assert retried.status_code == 202, retried.text
        assert (
            await client.post(base + f"/scan-jobs/{job}/retry", headers=retry_headers)
        ).json() == retried.json()
        assert retried.json()["retry_job_id"] != job
