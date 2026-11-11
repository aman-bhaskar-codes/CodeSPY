import httpx
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.services.github_acquisition import GitHubAcquisitionService
from scripts.phase1_fixture import scenario


class NoNetwork:
    async def resolve(self, full_name, ref):
        raise AssertionError("import request must not touch the network")

    async def archive(self, full_name, commit):
        raise AssertionError("import request must not touch the network")
        yield b""


async def client_for(factory, tmp_path, *, public):
    _, ctx, _, _, _ = await scenario(factory)
    store = LocalObjectStore(tmp_path / "objects")
    github = GitHubAcquisitionService(factory, store, None, public=NoNetwork()) if public else None
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=factory, store=store, fixture_context=ctx, github=github),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    token = (await client.get("/api/v1/session")).json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token, "Idempotency-Key": "imp-1"}
    return client, headers


async def test_import_queues_pinned_public_submission(factory, tmp_path):
    client, headers = await client_for(factory, tmp_path, public=True)
    try:
        body = {"url": "https://github.com/expressjs/express"}
        assert (await client.post("/api/v1/github-imports", json=body)).status_code == 403
        first = await client.post("/api/v1/github-imports", json=body, headers=headers)
        assert first.status_code == 202, first.text
        record = first.json()
        assert record["source_kind"] == "github_public_archive"
        assert record["state"] == "admission_queued"
        again = await client.post("/api/v1/github-imports", json=body, headers=headers)
        assert again.status_code == 202 and again.json()["id"] == record["id"]
        conflict = await client.post(
            "/api/v1/github-imports",
            json={"url": "https://github.com/expressjs/express", "ref": "4.x"},
            headers=headers,
        )
        assert conflict.status_code == 409
    finally:
        await client.aclose()


async def test_import_rejects_unsafe_urls_with_422(factory, tmp_path):
    client, headers = await client_for(factory, tmp_path, public=True)
    try:
        for url in (
            "http://github.com/o/r",
            "https://user@github.com/o/r",
            "https://github.com.evil.test/o/r",
            "https://github.com/o",
        ):
            response = await client.post(
                "/api/v1/github-imports", json={"url": url}, headers=headers
            )
            assert response.status_code == 422, url
    finally:
        await client.aclose()


async def test_import_is_unavailable_without_public_provider(factory, tmp_path):
    client, headers = await client_for(factory, tmp_path, public=False)
    try:
        response = await client.post(
            "/api/v1/github-imports", json={"url": "https://github.com/o/r"}, headers=headers
        )
        assert response.status_code == 503
    finally:
        await client.aclose()
