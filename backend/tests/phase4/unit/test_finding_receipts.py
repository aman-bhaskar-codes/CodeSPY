"""Broker receipts are internal execution results, never caller-supplied DTO proof."""

import pytest
from app.domain.canonical_json import semantic_digest
from app.scanners.dependency_adapter import DockerDependencyAdapter
from app.scanners.ownership import ScannerOwnership

from tests.phase4.unit.test_dependency_adapter import IMAGE, Controller, prepare
from tests.phase4.unit.test_scanner_adapter import make_adapter


async def test_receipt_is_returned_only_after_independent_native_verification(tmp_path):
    adapter, controller, _, request = make_adapter(tmp_path)
    assert hasattr(adapter, "scan_attested"), "broker does not expose verified execution"
    receipt = await adapter.scan_attested(request)
    receipt.validate()
    assert receipt.request == request
    assert receipt.image == IMAGE
    assert receipt.result_digest == semantic_digest(receipt.result.model_dump())
    assert len(controller.removed) == 2


async def test_failed_native_attestation_cannot_issue_receipt(tmp_path):
    from app.scanners.process import NativeProcessError, NativeResult

    adapter, controller, _, request = make_adapter(tmp_path)
    assert hasattr(adapter, "scan_attested"), "broker does not expose verified execution"
    original = controller.start

    async def invalid(name, maximum):
        if controller.containers[name].mode == "attest":
            return NativeResult(exit_code=0, output=b"0" * 64)
        return await original(name, maximum)

    controller.start = invalid
    with pytest.raises(NativeProcessError):
        await adapter.scan_attested(request)


async def test_dependency_receipt_follows_authenticated_archive_execution(tmp_path):
    request, bundle, stager = await prepare(tmp_path)
    adapter = DockerDependencyAdapter(
        ScannerOwnership.create(tmp_path / "owned", IMAGE),
        Controller(),
        stager,
        bundle,
        expected_entrypoint=("trusted-entrypoint",),
    )
    assert hasattr(adapter, "assess_attested"), "broker does not expose verified execution"
    receipt = await adapter.assess_attested(request)
    receipt.validate()
    assert receipt.result.packages[0].query.name == "example"
    assert receipt.result.coverage == "complete"
