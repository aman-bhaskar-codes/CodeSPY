import httpx
from tests.phase2.api.test_session_upload_api import fixture_api


async def test_repository_command_key_rejects_changed_input(factory, tmp_path):
    app, _, _ = await fixture_api(factory, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = (await client.get("/api/v1/session")).json()["csrf_token"]
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "repository-command",
        }
        body = {"repository_key": "new-one", "display_name": "New one", "source_kind": "archive"}
        first = await client.post("/api/v1/repositories", json=body, headers=headers)
        assert first.status_code == 201
        replay = await client.post("/api/v1/repositories", json=body, headers=headers)
        assert replay.json()["id"] == first.json()["id"]
        changed = await client.post(
            "/api/v1/repositories", json={**body, "repository_key": "new-two"}, headers=headers
        )
        assert changed.status_code == 409
