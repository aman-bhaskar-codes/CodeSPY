"""Orchestration contract doubles; real containment is qualified separately."""

import asyncio

import pytest
from app.contracts.scanners import ScannerResultV1
from app.domain.canonical_json import semantic_digest
from app.scanners.adapter import DockerScannerAdapter
from app.scanners.ownership import ScannerOwnership
from app.scanners.process import NativeProcessError, NativeResult
from app.scanners.runtime import RuntimeManifest

from tests.phase4.runtime_fixtures import runtime
from tests.phase4.unit.test_scanner_archive import fixture_input

IMAGE = "sha256:" + "a" * 64


class Stager:
    async def stage(self, request, root):
        source = fixture_input(root)
        return source.model_copy(update={"request": request})


class Controller:
    def __init__(self, request):
        self.request = request
        self.current = None
        self.containers = {}
        self.removed = []
        self.stall = False
        self.foreign = False
        self.entered = asyncio.Event()

    async def image_entrypoint(self, image):
        return ("trusted-entrypoint",)

    async def create(self, launch):
        self.current = launch
        self.containers[launch.name] = launch

    async def verify_controls(self, name):
        pass

    async def inspect_owned(self, name):
        if name not in self.containers:
            return None
        return {"id": name[-32:] * 2, "image": IMAGE, "marker": self.containers[name].marker}

    async def remove(self, identity):
        self.removed.append(identity)
        self.containers = {
            name: value for name, value in self.containers.items() if name[-32:] * 2 != identity
        }

    async def start(self, name, maximum):
        self.entered.set()
        if self.stall:
            await asyncio.Event().wait()
        result = ScannerResultV1(
            request_digest="f" * 64 if self.foreign else self.request.semantic_hash(),
            status="completed",
            processed_file_ids=tuple(f.file_id for f in self.request.files),
            omitted_file_ids=(),
        )
        if self.containers[name].mode == "attest":
            return NativeResult(exit_code=0, output=semantic_digest(result.model_dump()).encode())
        return NativeResult(exit_code=0, output=result.model_dump_json().encode())


def make_adapter(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    request = fixture_input(probe).request.model_copy(
        update={"profile": runtime("semgrep").profile()}
    )
    controller = Controller(request)
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    adapter = DockerScannerAdapter(
        owner,
        controller,
        Stager(),
        RuntimeManifest(tools=(runtime("semgrep"),)),
        expected_entrypoint=("trusted-entrypoint",),
    )
    return adapter, controller, owner, request


async def test_adapter_validates_request_bound_output_then_removes_owned_resources(tmp_path):
    adapter, controller, owner, request = make_adapter(tmp_path)
    result = await adapter.scan(request)
    result.validate_for(request)
    assert len(set(controller.removed)) == 2
    assert list(owner.root.glob("codespy-scan-*")) == []


async def test_foreign_result_cannot_be_published_and_still_cleans_container(tmp_path):
    adapter, controller, owner, request = make_adapter(tmp_path)
    controller.foreign = True
    with pytest.raises(NativeProcessError):
        await adapter.scan(request)
    assert len(controller.removed) == 1
    assert list(owner.root.glob("codespy-scan-*")) == []


async def test_cancelled_scan_waits_for_owned_removal_before_returning(tmp_path):
    adapter, controller, owner, request = make_adapter(tmp_path)
    controller.stall = True
    task = asyncio.create_task(adapter.scan(request))
    await asyncio.wait_for(controller.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(controller.removed) == 1
    assert list(owner.root.glob("codespy-scan-*")) == []


async def test_cancellation_preserves_pending_cleanup_when_daemon_is_unavailable(tmp_path):
    adapter, controller, owner, request = make_adapter(tmp_path)
    controller.stall = True

    async def unavailable(identity):
        raise NativeProcessError("scanner_runtime_unavailable")

    controller.remove = unavailable
    task = asyncio.create_task(adapter.scan(request))
    await asyncio.wait_for(controller.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(list(owner.root.glob("codespy-scan-*.json"))) == 1
    assert len(list(owner.root.glob("codespy-scan-*.input"))) == 1


async def test_mismatched_independent_attestation_is_rejected_and_both_owners_cleaned(tmp_path):
    adapter, controller, owner, request = make_adapter(tmp_path)
    original = controller.start

    async def altered(name, maximum):
        if controller.containers[name].mode == "attest":
            return NativeResult(exit_code=0, output=b"0" * 64)
        return await original(name, maximum)

    controller.start = altered
    with pytest.raises(NativeProcessError, match="scanner_output_invalid"):
        await adapter.scan(request)
    assert len(set(controller.removed)) == 2
    assert list(owner.root.glob("codespy-scan-*")) == []
