import httpx
from app.contracts.persistence import RepositoryCreate
from app.models.phase2_tables import PHASE2_TABLES
from app.repositories.repository_repository import RepositoryRepository
from sqlalchemy import insert
from tests.phase2.api.test_session_upload_api import fixture_api


async def test_repository_pages_are_scoped_keysets(factory, tmp_path):
    app, ctx, _ = await fixture_api(factory, tmp_path)
    async with factory.begin() as s:
        for index in range(3):
            repo = await RepositoryRepository(s).create(
                ctx,
                RepositoryCreate(
                    repository_key=f"page-{index}",
                    display_name=f"Page {index}",
                    source_kind="archive",
                ),
            )
            await s.execute(
                insert(PHASE2_TABLES["repository_grants"]).values(
                    org_id=ctx.org_id,
                    repository_id=repo.id,
                    principal_id=ctx.principal_id,
                    permission="write",
                )
            )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.get("/api/v1/session")
        first = await client.get("/api/v1/repositories?limit=2")
        assert first.status_code == 200, first.text
        first = first.json()
        assert len(first["items"]) == 2 and first["next_cursor"]
        second = (
            await client.get(
                "/api/v1/repositories", params={"limit": 2, "after": first["next_cursor"]}
            )
        ).json()
        assert len(second["items"]) == 2 and not second["next_cursor"]
        assert {item["id"] for item in first["items"]}.isdisjoint(
            {item["id"] for item in second["items"]}
        )
