from uuid import UUID, uuid4

import httpx
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.security.sessions import SessionService
from app.services.jobs import JobService

from tests.ast_facts.test_ast_facts_publication import (
    admitted,
    operations,
)


async def ready():
    return True


async def client_for(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    issued = await SessionService(factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(
            environment="test",
            auth_mode="local_fixture",
            origin="http://testserver",
            structural_enabled=True,
            structural_toolchain_hash="a" * 64,
        ),
        ApplicationPorts(factory, store, ctx, readiness=ready),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token},
    )
    token = (await client.get("/api/v1/session")).json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token, "Idempotency-Key": "ast-1"}
    return client, headers, ctx, snapshot, repo, store


async def test_create_publish_and_read_ast_facts_through_the_api(factory, tmp_path):
    client, headers, ctx, snapshot, repo, store = await client_for(factory, tmp_path)
    try:
        url = f"/api/v1/repositories/{repo}"
        missing = await client.get(f"{url}/snapshots/{snapshot}/ast-facts")
        assert missing.status_code == 404
        accepted = await client.post(
            f"{url}/ast-facts", json={"snapshot_id": str(snapshot)}, headers=headers
        )
        assert accepted.status_code == 202, accepted.text
        replay = await client.post(
            f"{url}/ast-facts", json={"snapshot_id": str(snapshot)}, headers=headers
        )
        assert replay.json() == accepted.json()
        lease = await JobService(factory).claim(
            ctx.org_id, UUID(accepted.json()["job_id"]), 1, "worker"
        )
        await operations(factory, store)[2].execute(lease)
        summary = await client.get(f"{url}/snapshots/{snapshot}/ast-facts")
        assert summary.status_code == 200
        assert summary.json()["files"] == 1 and summary.json()["languages"] == {"python": 1}
        page = await client.get(f"{url}/snapshots/{snapshot}/ast-facts/files?limit=10")
        item = page.json()["items"][0]
        assert item["path"] == "a.py" and page.json()["next_after"] is None
        facts = await client.get(f"{url}/snapshots/{snapshot}/ast-facts/files/{item['file_id']}")
        assert facts.status_code == 200 and facts.json()["language"] == "python"
        assert (
            await client.get(f"{url}/snapshots/{snapshot}/ast-facts/files?limit=0")
        ).status_code == 422
        unknown = await client.get(f"{url}/snapshots/{snapshot}/ast-facts/files/{uuid4()}")
        assert unknown.status_code == 404
    finally:
        await client.aclose()


async def test_create_requires_csrf(factory, tmp_path):
    client, _headers, _, snapshot, repo, _ = await client_for(factory, tmp_path)
    try:
        url = f"/api/v1/repositories/{repo}/ast-facts"
        assert (await client.post(url, json={"snapshot_id": str(snapshot)})).status_code == 403
    finally:
        await client.aclose()


async def test_create_is_unavailable_without_a_qualified_toolchain(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    issued = await SessionService(factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory, store, ctx),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token},
    ) as client:
        token = (await client.get("/api/v1/session")).json()["csrf_token"]
        headers = {"Origin": "http://testserver", "X-CSRF-Token": token, "Idempotency-Key": "k"}
        response = await client.post(
            f"/api/v1/repositories/{repo}/ast-facts",
            json={"snapshot_id": str(snapshot)},
            headers=headers,
        )
        assert response.status_code == 503


class RouteModelRunner:
    """Adds framework facts (with valid ranges) to the fixture file's real facts."""

    async def analyze(self, source, root):
        raise AssertionError("not a structural job")

    async def analyze_facts(self, source, root):
        from app.analyzers.facts_pipeline import analyze_facts
        from app.analyzers.positions import SourceIndex
        from app.contracts.ast_facts import (
            AstDraftV1,
            ColumnFactV1,
            ModelFactV1,
            RouteFactV1,
        )

        draft = analyze_facts(source, root)
        (file,) = draft.files
        span = SourceIndex(b"print(1)\n").range_from_bytes(0, 8)
        routes = tuple(
            RouteFactV1(
                framework="flask", method="GET", path=f"/r{i}", handler=f"app.h{i}", range=span
            )
            for i in range(3)
        )
        models = (
            ModelFactV1(
                framework="sqlalchemy",
                name="User",
                table="users",
                columns=(ColumnFactV1(name="id", type="Integer", primary_key=True),),
                range=span,
            ),
        )
        updated = file.model_copy(update={"routes": routes, "models": models})
        return AstDraftV1.model_validate_json(
            draft.model_copy(update={"files": (updated,)}).model_dump_json()
        )


async def test_routes_and_models_are_listed_paged_and_scoped(factory, tmp_path):
    from app.services.ast_finalization import AstFactsFinalizer
    from app.services.structural_finalization import StructuralFinalizer
    from app.workers.operations.analyze_structure import StructuralAnalysisOperation
    from app.workers.operations.ast_facts import AstFactsOperation, ProfileDispatchOperation

    client, headers, ctx, snapshot, repo, store = await client_for(factory, tmp_path)
    try:
        url = f"/api/v1/repositories/{repo}/snapshots/{snapshot}/ast-facts"
        accepted = await client.post(
            f"/api/v1/repositories/{repo}/ast-facts",
            json={"snapshot_id": str(snapshot)},
            headers=headers,
        )
        lease = await JobService(factory).claim(
            ctx.org_id, UUID(accepted.json()["job_id"]), 1, "worker"
        )
        runner = RouteModelRunner()
        structural = StructuralAnalysisOperation(
            factory, store, StructuralFinalizer(factory, store), runner
        )
        facts = AstFactsOperation(factory, store, AstFactsFinalizer(factory, store), runner)
        await ProfileDispatchOperation(factory, structural, facts).execute(lease)

        first = await client.get(f"{url}/routes?limit=2")
        assert first.status_code == 200
        body = first.json()
        assert [i["route_path"] for i in body["items"]] == ["/r0", "/r1"]
        assert body["items"][0]["file_path"] == "a.py" and body["items"][0]["method"] == "GET"
        assert body["items"][0]["handler"] == "app.h0" and body["items"][0]["start_line"] == 1
        assert body["next_after"] == "2:a.py"
        second = await client.get(f"{url}/routes?limit=2&after={body['next_after']}")
        assert [i["route_path"] for i in second.json()["items"]] == ["/r2"]
        assert second.json()["next_after"] is None

        models = await client.get(f"{url}/models")
        (model,) = models.json()["items"]
        assert (model["framework"], model["name"], model["table"]) == (
            "sqlalchemy",
            "User",
            "users",
        )
        assert model["columns"] == [
            {
                "name": "id",
                "type": "Integer",
                "nullable": None,
                "primary_key": True,
                "foreign_key": None,
            }
        ]
        assert (await client.get(f"{url}/routes?limit=0")).status_code == 422
        assert (await client.get(f"{url}/models?limit=201")).status_code == 422
    finally:
        await client.aclose()


async def test_routes_are_not_found_before_publication(factory, tmp_path):
    client, _, _, snapshot, repo, _ = await client_for(factory, tmp_path)
    try:
        url = f"/api/v1/repositories/{repo}/snapshots/{snapshot}/ast-facts"
        assert (await client.get(f"{url}/routes")).status_code == 404
        assert (await client.get(f"{url}/models")).status_code == 404
    finally:
        await client.aclose()
