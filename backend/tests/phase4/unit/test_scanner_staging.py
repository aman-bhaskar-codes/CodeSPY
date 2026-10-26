"""Real local object-store streaming connects admitted identities to scanner input."""

from uuid import UUID

import pytest
from app.adapters.storage.local import LocalObjectStore
from app.domain.errors import ScopeViolation
from app.scanners.source import read_scanner_sources
from app.services.analysis_sources import LocatedSource
from app.services.scanner_sources import BoundScannerStager

from tests.phase4.unit.test_scanner_archive import fixture_input


async def chunks(data):
    yield data


async def prepare(root):
    original = root / "original"
    original.mkdir()
    source = fixture_input(original)
    store = LocalObjectStore(root / "objects")
    archive = await store.put_stage(
        "stage/source/archive", chunks((original / "archive.zip").read_bytes()), 104857600
    )
    manifest = await store.put_stage(
        "stage/source/manifest", chunks((original / "manifest.json").read_bytes()), 67108864
    )
    located = LocatedSource(archive, manifest, source.files, source.policy.admission, None)
    return BoundScannerStager(
        store, located, source.request.scope, source.request.repository_id
    ), source.request


async def test_real_store_stages_original_archive_for_persisted_selected_ids(tmp_path):
    stager, request = await prepare(tmp_path)
    target = tmp_path / "input"
    target.mkdir()
    source = await stager.stage(request, target)
    assert source.request == request
    assert read_scanner_sources(source, target) == {
        request.files[0].file_id: b"def hello():\n    return 'world'\n"
    }


@pytest.mark.parametrize("field", ["run", "repository"])
async def test_scope_replay_is_rejected_before_objects_are_staged(tmp_path, field):
    stager, request = await prepare(tmp_path)
    if field == "run":
        request = request.model_copy(
            update={"scope": request.scope.model_copy(update={"run_id": UUID(int=999)})}
        )
    else:
        request = request.model_copy(update={"repository_id": UUID(int=999)})
    target = tmp_path / "input"
    target.mkdir()
    with pytest.raises(ScopeViolation):
        await stager.stage(request, target)
    assert list(target.iterdir()) == []
