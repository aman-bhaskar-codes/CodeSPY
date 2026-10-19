import asyncio
from hashlib import sha256
from uuid import uuid4

import pytest
from app.contracts.persistence import ArtifactCreate, SnapshotCreate
from app.domain.errors import DuplicateConflict
from scripts.phase1_fixture import FIXTURE_SOURCES, fixture_files, scenario


async def test_replay_returns_persisted_file_ids(factory):
    service, ctx, snapshot, _, ids = await scenario(factory)
    data = SnapshotCreate(
        repository_id=snapshot.repository_id,
        artifact=ArtifactCreate(
            sha256=sha256(
                b"".join(p.encode() + b"\0" + b for p, b in FIXTURE_SOURCES.items())
            ).hexdigest(),
            size_bytes=sum(len(p.encode()) + 1 + len(b) for p, b in FIXTURE_SOURCES.items()),
            storage_key="fixture/source",
        ),
        admission_config_hash="d" * 64,
    )
    candidates = fixture_files([uuid4(), uuid4()])
    first, second = await asyncio.gather(
        service.create_snapshot(ctx, data, candidates),
        service.create_snapshot(ctx, data, fixture_files([uuid4(), uuid4()])),
    )
    assert first.id == second.id == snapshot.id
    async with factory() as s:
        stored = await service._resolve_snapshot_files_in_session(s, ctx, snapshot.id, candidates)
    assert {f.id for f in stored} == set(ids)
    assert not {f.id for f in stored} & {f.id for f in candidates}
    wrong = [candidates[0].model_copy(update={"size_bytes": 999}), candidates[1]]
    with pytest.raises(DuplicateConflict):
        async with factory() as s:
            await service._resolve_snapshot_files_in_session(s, ctx, snapshot.id, wrong)
