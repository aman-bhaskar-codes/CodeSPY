# ruff: noqa: S603, S607
"""Shared setup for the tests that run real containers. They are opt-in: set CODESPY_SANDBOX_IMAGE
to an image built by ``scripts/build_sandbox_image.py`` (for example ``codespy-sandbox:dev``)."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from app.adapters.sandbox.docker import DockerSandbox
from app.adapters.sandbox.fake import MappingSnapshots

IMAGE = os.environ.get("CODESPY_SANDBOX_IMAGE", "")


def _ready() -> bool:
    if not IMAGE or shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _ready(), reason="set CODESPY_SANDBOX_IMAGE to a built sandbox image and start Docker"
)


class Harness:
    """A DockerSandbox over a temporary root, with every container it starts removed afterwards."""

    def __init__(self, snapshots, **kw):
        self.root = Path(tempfile.mkdtemp(prefix="codespy-sbx-"))
        self.marker = uuid4().hex
        self.sandbox = DockerSandbox(
            self.root, MappingSnapshots(snapshots), image=IMAGE, marker=self.marker, **kw
        )

    async def close(self):
        await self.sandbox.reap()
        shutil.rmtree(self.root, ignore_errors=True)
