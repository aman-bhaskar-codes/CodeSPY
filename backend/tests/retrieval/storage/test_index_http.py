"""Real PostgreSQL HTTP indexing boundaries with explicitly synthetic runtime readiness."""

import json
from uuid import uuid4

import httpx
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.indexing import IndexStatusV1
from app.models.index_tables import index_jobs, index_specs
from app.models.phase2_tables import PHASE2_TABLES
from app.security.sessions import SessionService
from sqlalchemy import func, select, update

from tests.phase3.integration.test_structural_publication import admitted
from tests.retrieval.storage.test_index_jobs import ReadyCapabilities, writer
from tests.retrieval.unit.test_index_contracts import config


def application(factory, store, ctx, *, enabled=True):
    return create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory,
            store,
            ctx,
            index_config=config() if enabled else None,
            index_builder=ReadyCapabilities() if enabled else None,
        ),
    )


async def client_for(app, migration_factory, ctx):
    issued = await SessionService(migration_factory).issue(ctx.principal_id)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token, "codespy_dev_csrf": issued.csrf},
    )
    headers = {"origin": "http://testserver", "x-csrf-token": issued.csrf}
    return client, headers


async def test_index_http_create_replay_status_cancel_retry(
    api_factory, migration_factory, tmp_path
):
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    app = application(api_factory, store, ctx)
    client, headers = await client_for(app, migration_factory, ctx)
    base = f"/api/v1/repositories/{repo}/snapshots/{snapshot}"
    async with client:
        before = await client.get(base + "/indexes")
        assert before.status_code == 200, before.text
        assert before.json()["can_build"] is True
        assert before.json()["lexical_availability"] == "index_unavailable"
        request_headers = {**headers, "idempotency-key": "http-index"}
        first = await client.post(
            base + "/indexes", headers=request_headers, json={"request_key": "http-index"}
        )
        replay = await client.post(
            base + "/indexes", headers=request_headers, json={"request_key": "http-index"}
        )
        assert first.status_code == replay.status_code == 202, first.text
        assert first.json() == replay.json()
        status_url = first.json()["status_url"]
        job_id = first.json()["job"]["id"]
        result = await client.get(status_url)
        assert result.status_code == 200, result.text
        status = IndexStatusV1.model_validate_json(result.content)
        assert status.job.id.hex == job_id.replace("-", "")
        assert status.can_cancel and not status.can_retry and status.receipt is None
        from app.mcp.server import ReadOnlyMcpServer
        from app.services.index_jobs import IndexJobService

        mcp = ReadOnlyMcpServer(
            identity=ctx, index_service=IndexJobService(api_factory, ReadyCapabilities())
        )
        mcp_result = await mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_index_status",
                    "arguments": {
                        "repository_id": str(repo),
                        "snapshot_id": str(snapshot),
                        "job_id": job_id,
                    },
                },
            }
        )
        assert json.loads(mcp_result["result"]["content"][0]["text"]) == result.json()
        assert "runtime_image" not in result.text and "storage_key" not in result.text
        generic = await client.get(f"/api/v1/jobs/{job_id}")
        assert generic.status_code == 200 and generic.json()["schema_version"] == 5
        for suffix in ("/events", "/retry"):
            blocked = await client.request(
                "GET" if suffix == "/events" else "POST",
                f"/api/v1/jobs/{job_id}{suffix}",
                headers=request_headers,
            )
            assert (
                blocked.status_code == 409
                and blocked.json()["code"] == "index_operation_unavailable"
            )
        cancelled = await client.post(status_url + "/cancel", headers=headers)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["job"]["state"] == "cancelled"
        assert cancelled.json()["can_retry"] is True and cancelled.json()["can_cancel"] is False
        retry = await client.post(
            status_url + "/retry",
            headers={**headers, "idempotency-key": "retry-http"},
            json={"request_key": "retry-http"},
        )
        assert retry.status_code == 202, retry.text
        retry_id = retry.json()["retry_job_id"]
        assert retry_id != job_id
        generic_cancel = await client.post(f"/api/v1/jobs/{retry_id}/cancel", headers=headers)
        assert generic_cancel.status_code == 200 and generic_cancel.json()["state"] == "cancelled"
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(index_specs)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(index_jobs)
                .where(index_jobs.c.operation == "index_snapshot")
            )
            == 2
        )


async def test_index_http_disabled_validation_csrf_and_route_scope(
    api_factory, migration_factory, tmp_path
):
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    app = application(api_factory, store, ctx, enabled=False)
    client, headers = await client_for(app, migration_factory, ctx)
    base = f"/api/v1/repositories/{repo}/snapshots/{snapshot}"
    async with client:
        availability = await client.get(base + "/indexes")
        assert availability.status_code == 200 and not availability.json()["can_build"]
        for body, header_key, expected in (
            ({"request_key": "key"}, "key", 503),
            ({"request_key": "key", "config": {}}, "key", 422),
            ({"request_key": "key"}, "different", 422),
            ({"request_key": "job:reserved"}, "job:reserved", 422),
        ):
            response = await client.post(
                base + "/indexes", headers={**headers, "idempotency-key": header_key}, json=body
            )
            assert response.status_code == expected, response.text
        assert (
            await client.post(base + "/indexes", json={"request_key": "key"})
        ).status_code == 403
        wrong = f"/api/v1/repositories/{repo}/snapshots/{uuid4()}/indexes"
        assert (
            await client.post(
                wrong, headers={**headers, "idempotency-key": "key"}, json={"request_key": "key"}
            )
        ).status_code == 404
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(index_specs)) == 0


async def test_index_http_readers_and_other_writers_do_not_gain_retry(
    api_factory, migration_factory, tmp_path
):
    from app.services.index_jobs import IndexJobService

    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    service = IndexJobService(api_factory, ReadyCapabilities())
    created = await service.create(ctx, repo, snapshot, config(), "original")
    await service.cancel(ctx, repo, snapshot, created.job.id)
    other = await writer(migration_factory, ctx, repo)
    app = application(api_factory, store, other)
    client, headers = await client_for(app, migration_factory, other)
    base = f"/api/v1/repositories/{repo}/snapshots/{snapshot}"
    status_url = base + f"/index-jobs/{created.job.id}"
    async with client:
        status = await client.get(status_url)
        assert status.status_code == 200 and status.json()["can_retry"] is False
        retry = await client.post(
            status_url + "/retry",
            headers={**headers, "idempotency-key": "other-retry"},
            json={"request_key": "other-retry"},
        )
        assert retry.status_code == 409
        assert (
            await client.get(status_url.replace(str(snapshot), str(uuid4())))
        ).status_code == 404
        async with migration_factory() as session, session.begin():
            await session.execute(
                update(PHASE2_TABLES["repository_grants"])
                .where(PHASE2_TABLES["repository_grants"].c.principal_id == other.principal_id)
                .values(permission="read")
            )
        availability = await client.get(base + "/indexes")
        assert availability.status_code == 200 and not availability.json()["can_build"]
        denied = await client.post(
            base + "/indexes",
            headers={**headers, "idempotency-key": "reader"},
            json={"request_key": "reader"},
        )
        assert denied.status_code == 404
        async with migration_factory() as session, session.begin():
            await session.execute(
                update(PHASE2_TABLES["repository_grants"])
                .where(PHASE2_TABLES["repository_grants"].c.principal_id == other.principal_id)
                .values(revoked_at=func.clock_timestamp())
            )
        assert (await client.get(status_url)).status_code == 404
