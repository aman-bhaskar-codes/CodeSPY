"""Owned real Linux scanner image qualification; no target code is executed."""

import os
from pathlib import Path

import pytest
from app.scanners.docker import DockerController, SandboxLaunch
from app.scanners.ownership import ScannerOwnership
from app.scanners.runtime import RuntimeManifest
from tests.phase4.unit.test_scanner_archive import fixture_input


def settings():
    values = tuple(
        os.environ.get(key)
        for key in (
            "CODESPY_SCANNER_TEST_DOCKER",
            "CODESPY_SCANNER_TEST_SOCKET",
            "CODESPY_SCANNER_TEST_IMAGE",
        )
    )
    if not all(values):
        pytest.skip("owned pinned Linux scanner image not supplied")
    return values


async def test_real_image_reports_authenticated_runtime_versions_under_controls(tmp_path):
    binary, socket, image = settings()
    owner = ScannerOwnership.create(tmp_path / "owned", image)
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    name = owner.reserve()
    root = owner.root / (name + ".input")
    root.mkdir()
    source = fixture_input(root)
    (root / "request.json").write_text(source.model_dump_json())
    try:
        await controller.create(SandboxLaunch(name, owner.marker, image, root, mode="identities"))
        await controller.verify_controls(name)
        native = await controller.start(name, 65536)
        manifest = RuntimeManifest.model_validate_json(native.output)
        assert {t.tool: t.version for t in manifest.tools} == {
            "semgrep": "1.179.0",
            "gitleaks": "8.30.1",
        }
        assert all(t.artifact_sha256 != "0" * 64 for t in manifest.tools)
    finally:
        await owner.remove(name, controller)
    assert list(owner.root.glob("codespy-scan-*")) == []


async def test_real_pending_container_is_recovered_by_durable_owner(tmp_path):
    binary, socket, image = settings()
    owner = ScannerOwnership.create(tmp_path / "owned", image)
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    name = owner.reserve()
    root = owner.root / (name + ".input")
    root.mkdir()
    source = fixture_input(root)
    (root / "request.json").write_text(source.model_dump_json())
    try:
        await controller.create(SandboxLaunch(name, owner.marker, image, root, mode="identities"))
        restored = ScannerOwnership.restore(owner.root, image)
        await restored.cleanup_pending(controller)
        assert await controller.inspect_owned(name) is None
        assert list(owner.root.glob("codespy-scan-*")) == []
    finally:
        await owner.remove(name, controller)


async def test_real_output_overflow_still_removes_exact_owned_container(tmp_path):
    from app.scanners.process import NativeProcessError

    binary, socket, image = settings()
    owner = ScannerOwnership.create(tmp_path / "owned", image)
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    name = owner.reserve()
    root = owner.root / (name + ".input")
    root.mkdir()
    source = fixture_input(root)
    (root / "request.json").write_text(source.model_dump_json())
    try:
        await controller.create(SandboxLaunch(name, owner.marker, image, root, mode="identities"))
        with pytest.raises(NativeProcessError, match="scanner_output_limit"):
            await controller.start(name, 32)
    finally:
        await owner.remove(name, controller)
    assert await controller.inspect_owned(name) is None
    assert list(owner.root.glob("codespy-scan-*")) == []
