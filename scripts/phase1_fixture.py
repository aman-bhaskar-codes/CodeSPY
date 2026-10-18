from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from app.contracts.evidence import EvidenceEnvelopeV1
from app.contracts.persistence import (
    ArtifactCreate,
    CoverageV1,
    DerivationCreateV1,
    EdgeCreateV1,
    NodeCreateV1,
    RepositoryCreate,
    ResultBatchV1,
    RunCreate,
    SnapshotCreate,
    SourceFileCreate,
)
from app.db.transaction import transaction
from app.domain.source_ranges import SourceRangeV1
from app.repositories.identity_repository import IdentityRepository
from app.services.persistence import PersistenceService

FIXTURE_SOURCES = {
    "a.py": b"def demo():\n    return 1\n",
    "é.py": "def café():\r\n    return 2\r\n".encode(),
}


def fixture_files(ids):
    return [
        SourceFileCreate(
            id=file_id,
            path=path,
            sha256=sha256(source).hexdigest(),
            size_bytes=len(source),
            language="python",
        )
        for file_id, (path, source) in zip(ids, FIXTURE_SOURCES.items(), strict=True)
    ]


async def scenario(factory):
    async with transaction(factory) as s:
        ctx = await IdentityRepository(s).create_fixture("fixture-" + uuid4().hex[:12])
    service = PersistenceService(factory)
    repo = await service.create_repository(
        ctx,
        RepositoryCreate(
            repository_key="fixture",
            display_name="Fixture",
            source_kind="local_fixture",
        ),
    )
    ids = [uuid4(), uuid4()]
    files = fixture_files(ids)
    artifact_bytes = b"".join(
        path.encode("utf-8") + b"\0" + source
        for path, source in FIXTURE_SOURCES.items()
    )
    snapshot = await service.create_snapshot(
        ctx,
        SnapshotCreate(
            repository_id=repo.id,
            artifact=ArtifactCreate(
                sha256=sha256(artifact_bytes).hexdigest(),
                size_bytes=len(artifact_bytes),
                storage_key="fixture/source",
            ),
            admission_config_hash="d" * 64,
        ),
        files,
    )
    run = await service.create_run(
        ctx,
        RunCreate(
            snapshot_id=snapshot.id,
            request_key=uuid4().hex,
            analyzer_name="fixture-builder",
            analyzer_version="1",
            config={},
        ),
    )
    run = await service.start_run(ctx, run.ref, 0)
    return service, ctx, snapshot, run, ids


def batch_for(ref, files):
    r = SourceRangeV1(
        start_byte=0, end_byte=4, start_line=1, end_line=1, start_col=0, end_col=4
    )
    ids = [uuid4(), uuid4()]
    eids = [uuid4(), uuid4()]
    nodes = [
        NodeCreateV1(
            local_id=n,
            file_id=f,
            node_kind="function",
            symbol_name="demo",
            qualified_name="fixture.demo",
            range=r,
        )
        for n, f in zip(ids, files, strict=True)
    ]
    evidence = [
        EvidenceEnvelopeV1(
            scope=ref,
            id=e,
            file_id=files[0],
            range=r,
            provider_name="fixture-builder",
            provider_version="1",
            operation="syntax-fixture",
            observation_mode="static",
            basis="observed" if i == 0 else "derived",
            coverage_status="complete",
            payload={"fixture": i},
            limitations=["Synthetic fixture, no analyzer executed"],
            captured_at=datetime.now(UTC),
        )
        for i, e in enumerate(eids)
    ]
    return ResultBatchV1(
        scope=ref,
        nodes=nodes,
        evidence=evidence,
        edges=[
            EdgeCreateV1(
                id=uuid4(),
                source_local_id=ids[0],
                target_local_id=ids[1],
                edge_kind="CALLS_CANDIDATE",
                support_evidence_id=eids[1],
                properties={"basis": "inferred"},
            )
        ],
        derivations=[DerivationCreateV1(child_id=eids[1], parent_id=eids[0])],
        disposition="completed",
        coverage=CoverageV1(
            status="complete",
            scope_description="synthetic fixture records",
            admitted_count=2,
            processed_count=2,
            omitted_count=0,
            truncated=False,
        ),
        limitations=["No parser or runtime executed"],
    )
