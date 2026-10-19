import httpx
from app.models.phase2_tables import PHASE2_TABLES
from sqlalchemy import update
from tests.phase2.api.test_session_upload_api import fixture_api
from tests.phase2.integration.test_submission_ingestion import zip_bytes


async def test_event_cursor_is_scoped_and_old_cursor_expires(factory, tmp_path):
    app, _, repo = await fixture_api(factory, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = (await client.get("/api/v1/session")).json()["csrf_token"]
        headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf, "Idempotency-Key": "events"}
        reserved = (
            await client.post(
                f"/api/v1/repositories/{repo.repository_id}/uploads",
                headers=headers,
                json={
                    "repository_id": str(repo.repository_id),
                    "filename": "source.zip",
                    "declared_size": len(zip_bytes()),
                },
            )
        ).json()
        uploaded = (
            await client.put(
                "/api/v1/uploads/" + reserved["id"] + "/content",
                content=zip_bytes(),
                headers={**headers, "Content-Type": "application/zip"},
            )
        ).json()
        job = uploaded["job_id"]
        # Add a second ordinary state event by cancellation.
        assert (await client.post(f"/api/v1/jobs/{job}/cancel", headers=headers)).status_code == 200
        first = (await client.get(f"/api/v1/jobs/{job}/events?limit=1")).json()
        assert first["next_cursor"]
        async with factory.begin() as s:
            await s.execute(
                update(PHASE2_TABLES["jobs"])
                .where(PHASE2_TABLES["jobs"].c.id == job)
                .values(events_min_sequence=3)
            )
        assert (
            await client.get(f"/api/v1/jobs/{job}/events", params={"after": first["next_cursor"]})
        ).status_code == 410
        assert (await client.get(f"/api/v1/jobs/{job}/events")).status_code == 200
