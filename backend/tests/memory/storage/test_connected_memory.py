"""Real HTTP, PostgreSQL and isolated source reader; provider calls are not exercised."""

import os

import httpx
import pytest
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.domain.scope import OrgRole, TenantContext
from app.models.tables import memberships
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.source_read.adapter import DockerSourceReader
from tests.phase3.integration.test_structural_publication import admitted


@pytest.mark.parametrize(
    "new_source,correspondence", [(b"print(1)\n", "matching_files"), (b"print(2)\n", "changed")]
)
async def test_admitted_source_candidate_review_preview_reload_and_archive(
    api_factory,
    migration_factory,
    tmp_path,
    new_source,
    correspondence,
    docker_runtime_paths,
):
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if image is None:
        pytest.skip("owned source reader image is required")
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    async with migration_factory() as s, s.begin():
        await s.execute(
            memberships.update().where(memberships.c.org_id == ctx.org_id).values(role="owner")
        )
    ctx = TenantContext(org_id=ctx.org_id, principal_id=ctx.principal_id, role=OrgRole.OWNER)
    controller = DockerController(
        docker_runtime_paths[0],
        docker_runtime_paths[1],
        tmp_path / "docker-config",
    )
    owner = ScannerOwnership.create(tmp_path / "reader-owner", image)
    reader = DockerSourceReader(owner, controller, store)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(api_factory, store, ctx, source_reader=reader),
    )
    prefix = f"/api/v1/repositories/{repo}/snapshots/{snapshot}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await client.get("/api/v1/session")
            assert session.status_code == 200
            headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": session.json()["csrf_token"],
                "Idempotency-Key": "proposal",
            }
            response = await client.post(
                prefix + "/memories",
                headers=headers,
                json={
                    "memory_kind": "architecture_fact",
                    "topic": "source routing",
                    "summary": "The admitted source contains a print call.",
                    "evidence_pointers": [{"file_path": "a.py", "start_line": 1, "end_line": 1}],
                },
            )
            assert response.status_code == 200, response.text
            entry = response.json()
            assert entry["state"] == "candidate"
            assert (await client.get(prefix + "/memories?query=source")).json()["items"] == []
            path = f"/api/v1/repositories/{repo}/memories/{entry['memory_id']}"
            denied = await client.post(
                path + "/reviews",
                json={
                    "action": "accept",
                    "expected_version": 1,
                    "request_key": "a",
                    "reason": "Checked",
                },
            )
            assert denied.status_code == 403
            async with migration_factory() as s, s.begin():
                await s.execute(
                    memberships.update()
                    .where(memberships.c.org_id == ctx.org_id)
                    .values(role="member")
                )
            forbidden = await client.post(
                path + "/reviews",
                headers=headers,
                json={
                    "action": "accept",
                    "expected_version": 1,
                    "request_key": "member-review",
                    "reason": "Writer cannot review",
                },
            )
            assert (
                forbidden.status_code == 403
                and forbidden.json()["code"] == "memory_review_forbidden"
            )
            async with migration_factory() as s, s.begin():
                await s.execute(
                    memberships.update()
                    .where(memberships.c.org_id == ctx.org_id)
                    .values(role="owner")
                )
            accepted = await client.post(
                path + "/reviews",
                headers=headers,
                json={
                    "action": "accept",
                    "expected_version": 1,
                    "request_key": "accept",
                    "reason": "Checked admitted source",
                },
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["state"] == "accepted"
            preview = await client.post(
                prefix + "/context/preview",
                headers=headers,
                json={"query": "source", "memory_mode": "current_only"},
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["qualification"] == "selection_preview"
            assert len(preview.json()["memories"]["items"]) == 1
            assert preview.json()["packet"]["lexical_availability"] == "index_unavailable"
            from tests.memory.storage.helpers import admit_another

            current_snapshot = await admit_another(migration_factory, store, ctx, repo, new_source)
            historical = await client.get(
                f"/api/v1/repositories/{repo}/snapshots/{current_snapshot}/memories?query=source&history_mode=historical_hints"
            )
            assert historical.status_code == 200, historical.text
            hint = historical.json()["items"][0]
            assert hint["historical_hint"] is True
            assert hint["source_correspondence"] == correspondence
            history = await client.get(path + "/history")
            assert [v["version"] for v in history.json()["versions"]] == [2, 1]
            assert len((await client.get(prefix + "/memories?query=source")).json()["items"]) == 1
            archive = await client.post(
                path + "/reviews",
                headers=headers,
                json={
                    "action": "archive",
                    "expected_version": 2,
                    "request_key": "archive",
                    "reason": "Withdrawn",
                },
            )
            assert archive.status_code == 200, archive.text
            assert not (await client.get(prefix + "/memories?query=source")).json()["items"]
    finally:
        await reader.recover_owned()
