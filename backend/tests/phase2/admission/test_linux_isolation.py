import io
import os
import zipfile

import pytest
from app.admission.process import DockerAdmissionRunner
from app.contracts.ingestion import AdmissionPolicyV1


@pytest.fixture
def runner(tmp_path):
    image = os.environ.get("CODESPY_ADMISSION_IMAGE")
    if not image:
        pytest.skip("owned pinned admission image not supplied")
    return DockerAdmissionRunner(image, tmp_path / "owned")


async def test_linux_profile_is_unprivileged_readonly_and_networkless(runner):
    result = await runner.qualify()
    assert result["nonroot"] and result["readonly_root"] and result["readonly_input"]
    assert result["network_denied"] and result["no_inherited_secrets"] and result["no_capabilities"]


async def test_linux_inventory_hashes_actual_bytes_without_extraction(runner):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("a.py", b"print(1)\n")
    manifest = await runner.inspect_bytes(out.getvalue(), AdmissionPolicyV1())
    assert len(manifest.entries) == 1 and manifest.entries[0].path == "a.py"
    assert manifest.entries[0].size_bytes == 9


async def test_linux_policy_v3_admits_the_extended_languages(runner):
    from app.contracts.ingestion import AdmissionPolicyV3

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("main.go", b"package main\n")
        archive.writestr("lib.rs", b"fn main() {}\n")
        archive.writestr("app.py", b"x = 1\n")
    data = out.getvalue()
    old = await runner.inspect_bytes(data, AdmissionPolicyV1())
    new = await runner.inspect_bytes(data, AdmissionPolicyV3())
    assert {e.path: (e.eligible, e.language_hint) for e in new.entries} == {
        "app.py": (True, "python"),
        "lib.rs": (True, "rust"),
        "main.go": (True, "go"),
    }
    assert {e.path: e.reason for e in old.entries if not e.eligible} == {
        "lib.rs": "unsupported_language",
        "main.go": "unsupported_language",
    }
    assert new.policy_version == "source-zip-v3" and new.archive_sha256 == old.archive_sha256
