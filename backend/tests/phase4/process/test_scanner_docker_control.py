"""Real daemon read-only checks, separate from mocked control contracts."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.scanners.docker import DockerController


async def test_daemon_confirms_missing_owned_name_without_removing_anything(tmp_path):
    socket = os.environ.get("CODESPY_SCANNER_TEST_SOCKET")
    binary = os.environ.get("CODESPY_SCANNER_TEST_DOCKER")
    if not socket or not binary:
        pytest.skip("owned Docker qualification configuration not supplied")
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    assert await controller.inspect_owned("codespy-scan-" + uuid4().hex) is None
