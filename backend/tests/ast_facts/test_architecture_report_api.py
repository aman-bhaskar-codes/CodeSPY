"""Architecture report HTTP routes: prepare, wait, build, read, and refusals."""

import os
from uuid import UUID, uuid4

import httpx
import pytest
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.security.sessions import SessionService
from app.services.jobs import JobService

from tests.ast_facts.test_ast_facts_publication import admitted, operations

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0048)",
)


async def ready():
    return True


async def client_for(factory, tmp_path, *, enabled=True):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    issued = await SessionService(factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(
            environment="test",
            auth_mode="local_fixture",
            origin="http://testserver",
            structural_enabled=enabled,
            structural_toolchain_hash="a" * 64 if enabled else None,
        ),
        ApplicationPorts(factory, store, ctx, readiness=ready),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token},
    )
    token = (await client.get("/api/v1/session")).json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token, "Idempotency-Key": "report-1"}
    base = f"/api/v1/repositories/{repo}/snapshots/{snapshot}/architecture-reports"
    return client, headers, ctx, store, base


async def test_prepare_build_and_read_a_report_through_http(factory, tmp_path):
    client, headers, ctx, store, base = await client_for(factory, tmp_path)
    try:
        assert (await client.post(base + "/prepare")).status_code == 403  # no CSRF
        prepared = await client.post(base + "/prepare", headers=headers)
        assert prepared.status_code == 202, prepared.text
        shards = prepared.json()
        assert shards["ready"] is False and shards["total_files"] == 1
        assert (await client.get(base + "/prepare")).json()["plan_digest"] == shards["plan_digest"]

        body = {"request_key": "report-1"}
        pending = await client.post(base, json=body, headers=headers)
        assert pending.status_code == 409 and pending.json()["code"] == "report_inputs_pending"
        assert (await client.get(base + "/latest")).status_code == 404

        lease = await JobService(factory).claim(
            ctx.org_id, UUID(shards["shards"][0]["job_id"]), 1, "worker"
        )
        await operations(factory, store)[2].execute(lease)
        assert (await client.get(base + "/prepare")).json()["ready"] is True

        mismatch = await client.post(base, json={"request_key": "other"}, headers=headers)
        assert mismatch.status_code == 422
        built = await client.post(base, json=body, headers=headers)
        assert built.status_code == 201, built.text
        ref = built.json()
        assert ref["complete"] is True and (ref["analyzed_files"], ref["eligible_files"]) == (1, 1)
        again = await client.post(base, json=body, headers=headers)
        assert again.json()["id"] == ref["id"]  # same inputs, same report
        assert (await client.get(base + "/latest")).json()["id"] == ref["id"]

        report = await client.get(f"{base}/{ref['id']}")
        assert report.status_code == 200 and report.json()["coverage"]["analyzed_files"] == 1
        names = [s["name"] for s in report.json()["sections"]]
        assert names[0] == "overview" and names[-1] == "coverage"
        markdown = await client.get(f"{base}/{ref['id']}/markdown")
        assert markdown.status_code == 200 and markdown.headers["content-type"].startswith(
            "text/markdown"
        )
        assert markdown.text.startswith("# Architecture report")
        assert "## Coverage and limitations" in markdown.text and "**UNKNOWN**" in markdown.text
        graph = await client.get(f"{base}/{ref['id']}/graph?limit=10")
        assert graph.status_code == 200 and graph.json()["nodes_total"] == 1
        assert graph.json()["nodes"][0]["label"] == "a.py"
        section = await client.get(f"{base}/{ref['id']}/sections/coverage")
        assert section.status_code == 200 and section.json()["name"] == "coverage"
        assert (await client.get(f"{base}/{ref['id']}/graph?limit=0")).status_code == 422
        assert (await client.get(f"{base}/{ref['id']}/sections/bogus")).status_code == 422
        assert (await client.get(f"{base}/{uuid4()}")).status_code == 404
    finally:
        await client.aclose()


async def test_prepare_is_unavailable_without_a_qualified_analysis_runtime(factory, tmp_path):
    client, headers, _, _, base = await client_for(factory, tmp_path, enabled=False)
    try:
        response = await client.post(base + "/prepare", headers=headers)
        assert response.status_code == 503 and response.json()["code"] == "analysis_unavailable"
    finally:
        await client.aclose()
