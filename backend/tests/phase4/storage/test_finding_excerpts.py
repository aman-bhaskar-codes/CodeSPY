"""Synthetic scanner publication with real admission, database and isolated source reads."""

import io
import os
import zipfile
from pathlib import Path

import pytest
from app.adapters.storage.local import LocalObjectStore
from app.domain.source_ranges import SourceRangeV1
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.services.findings_query import FindingsQueryService
from app.services.scan_publication import FindingFinalizer
from app.source_read.adapter import DockerSourceReader
from tests.phase4.storage.test_scan_jobs import Registry
from tests.phase4.storage.test_scan_publication import publication_case


async def test_finding_excerpt_redacts_verified_source_and_preserves_history(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned pinned reader image required")
    canary = "codespy_sec_canary_excerpt"
    code = f"api_key = '{canary}'\nprint(1)\n".encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.py", code)
    monkeypatch.setattr(
        "tests.phase2.integration.test_submission_ingestion.zip_bytes", lambda: buffer.getvalue()
    )
    monkeypatch.setattr(
        "tests.phase4.storage.test_scan_publication.SourceRangeV1",
        lambda **kwargs: SourceRangeV1(
            start_byte=0, end_byte=len(code), start_line=1, end_line=3, start_col=0, end_col=0
        ),
    )
    ctx, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    store = LocalObjectStore(tmp_path / "objects")
    owner = ScannerOwnership.create(tmp_path / "reader-owner", image)
    controller = DockerController(
        Path("/usr/local/bin/docker"),
        Path.home() / ".docker/run/docker.sock",
        tmp_path / "docker-config",
    )
    reader = DockerSourceReader(owner, controller, store)
    service = FindingsQueryService(api_factory, Registry(), source_reader=reader, store=store)
    try:
        result = await service.get_finding_detail(
            ctx, draft.spec.repository_id, lease.run_ref.snapshot_id, draft.findings[0].id
        )
        assert result.disposition == result.historical_disposition == "supported-static"
        assert result.excerpt_reason == "available"
        assert result.code_excerpt and "[REDACTED_SECRET:" in result.code_excerpt
        assert canary not in result.model_dump_json()
        assert len(result.code_excerpt.encode()) <= 8192
        assert any("Heuristic secret detection" in item for item in result.limitations)
    finally:
        await reader.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


async def test_concurrent_first_privacy_views_share_one_verified_registry(
    api_factory, migration_factory, tmp_path, monkeypatch
):
    import asyncio

    from app.contracts.source_view import PrivacyPolicyV1, SourceViewRequestV1
    from app.models.derived_artifacts import derived_artifacts
    from app.services.analysis_sources import locate_source
    from app.services.source_view import SourceViewService
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession
    from tests.phase3.integration.test_structural_publication import admitted

    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if not image:
        pytest.skip("owned pinned reader image required")
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    async with api_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    file = next(f for f in located.files if f.eligible)
    owner = ScannerOwnership.create(tmp_path / "reader-owner", image)
    controller = DockerController(
        Path("/usr/local/bin/docker"),
        Path.home() / ".docker/run/docker.sock",
        tmp_path / "docker-config",
    )
    reader = DockerSourceReader(owner, controller, store)
    request = SourceViewRequestV1(
        org_id=ctx.org_id,
        repository_id=repo,
        snapshot_id=snapshot,
        principal_id=ctx.principal_id,
        file_id=file.file_id,
    )
    both_read = asyncio.Event()
    arrived = 0
    original = AsyncSession.execute

    async def coordinated(session, statement, *args, **kwargs):
        nonlocal arrived
        result = await original(session, statement, *args, **kwargs)
        if (
            getattr(statement, "is_select", False)
            and any(table is derived_artifacts for table in statement.get_final_froms())
            and arrived < 2
        ):
            arrived += 1
            if arrived == 2:
                both_read.set()
            await both_read.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", coordinated)

    async def read_one():
        async with api_factory() as session, session.begin():
            return await SourceViewService(session, store, reader=reader).get(
                request, PrivacyPolicyV1()
            )

    try:
        async with asyncio.timeout(30):
            one, two = await asyncio.gather(read_one(), read_one())
        assert one.content_sha256 == two.content_sha256
        assert arrived == 2
        async with api_factory() as session:
            assert await session.scalar(select(func.count()).select_from(derived_artifacts)) == 1
    finally:
        await reader.recover_owned()
