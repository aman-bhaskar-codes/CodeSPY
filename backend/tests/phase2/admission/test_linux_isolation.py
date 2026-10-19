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
