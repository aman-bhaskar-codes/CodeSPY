import io
import zipfile

from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1


def test_nul_beyond_prefix_is_still_excluded_as_binary():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("large.py", b"a" * 5000 + b"\x00")
    manifest = inspect_zip_bytes(out.getvalue(), AdmissionPolicyV1())
    assert manifest.entries[0].reason == "binary"
