import httpx
import pytest
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.models.phase2_tables import PHASE2_TABLES
from scripts.phase1_fixture import scenario
from sqlalchemy import insert

from tests.phase2.integration.test_submission_ingestion import zip_bytes


async def fixture_api(factory, tmp_path):
    _, ctx, snap, _, _ = await scenario(factory)
    async with factory() as s, s.begin():
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=snap.repository_id,
                principal_id=ctx.principal_id,
                permission="write",
            )
        )
        await s.execute(insert(PHASE2_TABLES["org_work_limits"]).values(org_id=ctx.org_id))
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=factory, store=LocalObjectStore(tmp_path / "objects"), fixture_context=ctx
        ),
    )
    return app, ctx, snap


async def test_api_session_csrf_wire_uuid_and_upload(factory, tmp_path):
    app, _, repo = await fixture_api(factory, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await client.get("/api/v1/session")
        assert session.status_code == 200
        token = session.json()["csrf_token"]
        body = {
            "repository_id": str(repo.repository_id),
            "filename": "fixture.zip",
            "declared_size": len(zip_bytes()),
        }
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": token,
            "Idempotency-Key": "fixture-upload",
        }
        assert (
            await client.post(f"/api/v1/repositories/{repo.repository_id}/uploads", json=body)
        ).status_code == 403
        reserved = await client.post(
            f"/api/v1/repositories/{repo.repository_id}/uploads", json=body, headers=headers
        )
        assert reserved.status_code == 201, reserved.text
        id = reserved.json()["id"]
        response = await client.put(
            f"/api/v1/submissions/{id}/content",
            content=zip_bytes(),
            headers={**headers, "Content-Type": "application/zip"},
        )
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "admission_queued"
        job = await client.get("/api/v1/jobs/" + response.json()["job_id"])
        assert job.status_code == 200 and job.json()["state"] == "queued"
        assert (await client.post("/api/v1/logout", headers=headers)).status_code == 204
        assert (await client.get("/api/v1/jobs/" + response.json()["job_id"])).status_code == 401


async def test_fixture_mode_rejects_hosted_startup(factory, tmp_path):
    with pytest.raises(ValueError):
        create_app(
            ApiSettings(
                environment="hosted", auth_mode="local_fixture", origin="https://codespy.example"
            ),
            ApplicationPorts(factory=factory, store=LocalObjectStore(tmp_path / "objects")),
        )
