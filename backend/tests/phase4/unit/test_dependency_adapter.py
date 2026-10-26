"""Real local object-store integration with an explicit mocked controller."""

from uuid import UUID

import pytest
from app.adapters.storage.local import LocalObjectStore
from app.contracts.dependencies import DependencyResultV1
from app.domain.errors import ScopeViolation
from app.scanners.dependency_adapter import DockerDependencyAdapter
from app.scanners.dependency_worker import DependencyInputV1, assess_dependencies
from app.scanners.ownership import ScannerOwnership
from app.scanners.process import NativeProcessError, NativeResult
from app.services.analysis_sources import LocatedSource
from app.services.dependency_sources import BoundDependencyStager

from tests.phase4.dependency_fixtures import fixture_dependency

IMAGE = "sha256:" + "a" * 64


async def chunks(data):
    yield data


async def prepare(tmp_path, **options):
    root = tmp_path / "original"
    root.mkdir()
    source, bundle = fixture_dependency(root, **options)
    store = LocalObjectStore(tmp_path / "objects")
    archive = await store.put_stage(
        "stage/dependencies/archive", chunks((root / "archive.zip").read_bytes()), 104857600
    )
    manifest = await store.put_stage(
        "stage/dependencies/manifest", chunks((root / "manifest.json").read_bytes()), 67108864
    )
    stager = BoundDependencyStager(
        store,
        LocatedSource(archive, manifest, source.files, source.policy.admission, None),
        source.request.scope,
        source.request.repository_id,
    )
    return source.request, bundle, stager


class Controller:
    def __init__(self):
        self.launch = None
        self.foreign = False
        self.removed = []

    async def image_entrypoint(self, image):
        return ("trusted-entrypoint",)

    async def create(self, launch):
        self.launch = launch

    async def verify_controls(self, name):
        pass

    async def inspect_owned(self, name):
        return (
            None
            if self.launch is None
            else {"id": "b" * 64, "image": IMAGE, "marker": self.launch.marker}
        )

    async def remove(self, identity):
        self.removed.append(identity)
        self.launch = None

    async def start(self, name, maximum):
        value = DependencyInputV1.model_validate_json(
            (self.launch.input_root / "request.json").read_bytes()
        )
        result = assess_dependencies(value, self.launch.input_root)
        if self.foreign:
            result = result.model_copy(update={"request_digest": "f" * 64})
        return NativeResult(exit_code=0, output=result.model_dump_json().encode())


async def test_store_to_offline_assessment_preserves_scope_and_removes_owned_resources(tmp_path):
    request, bundle, stager = await prepare(tmp_path)
    controller = Controller()
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    adapter = DockerDependencyAdapter(
        owner, controller, stager, bundle, expected_entrypoint=("trusted-entrypoint",)
    )
    result = await adapter.assess(request)
    assert isinstance(result, DependencyResultV1)
    assert result.coverage == "complete"
    assert controller.removed == ["b" * 64]
    assert list(owner.root.glob("codespy-scan-*")) == []


async def test_foreign_result_is_rejected_before_acceptance_and_cleanup_still_runs(tmp_path):
    request, bundle, stager = await prepare(tmp_path)
    controller = Controller()
    controller.foreign = True
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    adapter = DockerDependencyAdapter(
        owner, controller, stager, bundle, expected_entrypoint=("trusted-entrypoint",)
    )
    with pytest.raises(NativeProcessError, match="scanner_output_invalid"):
        await adapter.assess(request)
    assert controller.removed == ["b" * 64]


async def test_stager_rejects_cross_repository_request_before_streaming(tmp_path):
    request, _, stager = await prepare(tmp_path)
    request = request.model_copy(update={"repository_id": UUID(int=99)})
    root = tmp_path / "input"
    root.mkdir()
    with pytest.raises(ScopeViolation):
        await stager.stage(request, root)
    assert list(root.iterdir()) == []
