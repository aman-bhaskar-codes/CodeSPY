"""Real database authority changes while an owned native source read is in flight."""

import asyncio
import os
from pathlib import Path

import pytest
from app.contracts.source_view import PrivacyPolicyV1, SourceRepresentation, SourceViewRequestV1
from app.domain.errors import PersistenceError
from app.models.phase2_tables import PHASE2_TABLES
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.services.analysis_sources import locate_source
from app.services.source_view import SourceViewService
from app.source_read.adapter import DockerSourceReader
from sqlalchemy import func, update

from tests.phase3.integration.test_structural_publication import admitted


@pytest.mark.parametrize("change", ["revoke", "delete"])
async def test_source_read_rechecks_live_authority(
    api_factory, migration_factory, tmp_path, change
):
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned source-reader image required")
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    async with api_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    selected = next(f for f in located.files if f.eligible)
    request = SourceViewRequestV1(
        org_id=ctx.org_id,
        repository_id=repo,
        snapshot_id=snapshot,
        principal_id=ctx.principal_id,
        file_id=selected.file_id,
        representation=SourceRepresentation.ORIGINAL,
    )
    owner = ScannerOwnership.create(tmp_path / "reader-owner", image)
    controller = DockerController(
        Path("/usr/local/bin/docker"),
        Path.home() / ".docker/run/docker.sock",
        tmp_path / "docker-config",
    )
    native = DockerSourceReader(owner, controller, store)
    extracted, release = asyncio.Event(), asyncio.Event()

    class PausedReader:
        async def read(self, value):
            content = await native.read(value)
            extracted.set()
            await release.wait()
            return content

    try:
        async with api_factory() as session:
            service = SourceViewService(session, store, reader=PausedReader())
            task = asyncio.create_task(
                service.get(request, PrivacyPolicyV1(allow_original_view=True))
            )
            try:
                async with asyncio.timeout(30):
                    await extracted.wait()
                    async with migration_factory() as writer, writer.begin():
                        if change == "revoke":
                            grants = PHASE2_TABLES["repository_grants"]
                            await writer.execute(
                                update(grants)
                                .where(
                                    grants.c.org_id == ctx.org_id,
                                    grants.c.principal_id == ctx.principal_id,
                                )
                                .values(revoked_at=func.clock_timestamp())
                            )
                        else:
                            objects = PHASE2_TABLES["object_records"]
                            await writer.execute(
                                update(objects)
                                .where(objects.c.org_id == ctx.org_id)
                                .values(state="deleted")
                            )
                    release.set()
                    with pytest.raises(PersistenceError):
                        await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        await native.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.parametrize("configured", [False, True])
async def test_authenticated_source_endpoint_requires_configured_native_reader(
    api_factory, migration_factory, tmp_path, configured
):
    import httpx
    from app.api.application import ApiSettings, ApplicationPorts, create_app
    from app.security.sessions import SessionService

    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned source-reader image required")
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    async with api_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    selected = next(f for f in located.files if f.eligible)
    owner = ScannerOwnership.create(tmp_path / "reader-owner", image)
    controller = DockerController(
        Path("/usr/local/bin/docker"),
        Path.home() / ".docker/run/docker.sock",
        tmp_path / "docker-config",
    )
    native = DockerSourceReader(owner, controller, store)
    issued = await SessionService(migration_factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(api_factory, store, ctx, source_reader=native if configured else None),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"codespy_dev": issued.token, "codespy_dev_csrf": issued.csrf},
        ) as client:
            response = await client.get(
                f"/api/v1/repositories/{repo}/snapshots/{snapshot}/files/{selected.file_id}/source-view"
            )
            assert response.status_code == (200 if configured else 503), response.text
            if configured:
                assert response.json()["file_sha256"] == selected.sha256
                assert response.json()["representation"] == "model_safe"
            else:
                assert response.json()["code"] == "source_reader_unavailable"
    finally:
        await native.recover_owned()
