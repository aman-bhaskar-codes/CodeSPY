"""Real native ceilings, independently from the small combined publication fixture."""

import hashlib
from pathlib import Path

import pytest
from app.scanners.dependency_adapter import DockerDependencyAdapter
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from tests.phase4.process.test_scanner_image import settings
from tests.phase4.process.test_scanner_pipeline import (
    test_real_store_archive_scan_preserves_findings_and_file_ids as run_native_corpus,
)
from tests.phase4.unit.test_dependency_adapter import prepare


@pytest.mark.parametrize("tool", ["semgrep", "gitleaks"])
async def test_real_native_128_files_16_mib(tmp_path, tool):
    await run_native_corpus(tmp_path, tool, "limits")


async def test_real_dependency_32_files_2_mib(tmp_path):
    binary, socket, image = settings()
    head = b'{"lockfileVersion":3,"packages":{},"padding":"'
    padding = hashlib.shake_256(b"metadata-ceiling").hexdigest(32768).encode()
    data = head + padding[: 65536 - len(head) - 2] + b'"}'
    assert len(data) == 65536
    contents = {f"project_{i}/package-lock.json": data for i in range(32)}
    request, bundle, stager = await prepare(tmp_path, contents=contents)
    request = type(request).model_validate(
        {**request.model_dump(), "runtime_digest": image.removeprefix("sha256:")}
    )
    assert len(request.files) == 32 and sum(f.size_bytes for f in request.files) == 2097152
    owner = ScannerOwnership.create(tmp_path / "owner", image)
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    adapter = DockerDependencyAdapter(
        owner,
        controller,
        stager,
        bundle,
        expected_entrypoint=await controller.image_entrypoint(image),
    )
    try:
        receipt = await adapter.assess_attested(request)
        assert receipt.result.coverage == "complete" and len(receipt.result.files) == 32
        assert receipt.result.packages == ()
    finally:
        await owner.cleanup_pending(controller)
    assert list(owner.root.glob("codespy-scan-*")) == []
