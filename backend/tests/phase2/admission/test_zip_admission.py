import io
import zipfile

import pytest
from app.admission.zip_inventory import AdmissionRejected, inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1


def archive(files, compression=zipfile.ZIP_STORED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compression) as z:
        for name, content in files:
            z.writestr(name, content)
    return out.getvalue()


def test_inventory_hashes_unicode_crlf_and_exclusions():
    data = archive(
        [
            ("café.py", "def café():\r\n pass\r\n".encode()),
            (".env", b"synthetic fixture"),
            ("a.zip", archive([])),
        ]
    )
    result = inspect_zip_bytes(data, AdmissionPolicyV1())
    assert len(result.entries) == 3
    assert {e.reason for e in result.entries} == {None, "sensitive_path", "nested_archive"}


@pytest.mark.parametrize("name", ["../bad", "/bad", "a\\b", "x/NUL", "a/../b", "a//b", "a:drive"])
def test_hostile_path_rejected(name):
    with pytest.raises(AdmissionRejected):
        inspect_zip_bytes(archive([(name, b"x")]), AdmissionPolicyV1())


def test_portability_collision_rejected():
    with pytest.raises(AdmissionRejected):
        inspect_zip_bytes(archive([("A.py", b"x"), ("a.py", b"y")]), AdmissionPolicyV1())


def test_compression_is_not_revision_identity():
    a = inspect_zip_bytes(
        archive([("a.py", b"print(1)\n")]), AdmissionPolicyV1(), source_revision="a" * 40
    )
    b = inspect_zip_bytes(
        archive([("a.py", b"print(1)\n")], zipfile.ZIP_DEFLATED),
        AdmissionPolicyV1(),
        source_revision="a" * 40,
    )
    assert a.archive_sha256 != b.archive_sha256
    assert a.normalized_files_sha256 == b.normalized_files_sha256
