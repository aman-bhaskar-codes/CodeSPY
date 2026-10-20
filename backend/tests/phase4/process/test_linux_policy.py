"""Native profile compatibility; CI provides an owned digest-pinned image."""

import os

import pytest
from app.admission.process import DockerAdmissionRunner
from app.contracts.ingestion import AdmissionPolicyV1, AdmissionPolicyV2
from tests.phase4.unit.test_admission_policy import archive


async def test_linux_admission_handles_both_policies_without_reinterpreting_v1(tmp_path):
    image = os.environ.get("CODESPY_ADMISSION_IMAGE")
    if not image:
        pytest.skip("owned Linux admission image required")
    runner = DockerAdmissionRunner(image, tmp_path / "owned")
    data = archive([("main.py", b"print(1)\n"), ("uv.lock", b"version = 1\n")])
    old = await runner.inspect_bytes(data, AdmissionPolicyV1())
    new = await runner.inspect_bytes(data, AdmissionPolicyV2())
    assert old.archive_sha256 == new.archive_sha256
    assert old.entries[-1].reason == "unsupported_language"
    assert new.entries[-1].eligible and new.entries[-1].language_hint == "toml"
    assert old.policy_hash != new.policy_hash
    assert not list(runner.root.glob("*/container.json"))
