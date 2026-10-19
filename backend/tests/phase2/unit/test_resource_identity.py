import io
import zipfile

from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1
from app.domain.canonical_json import semantic_digest


def test_same_commit_different_compression_has_distinct_artifact_identity():
    values = []
    for compression in [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=compression) as archive:
            archive.writestr("provider-root/a.py", b"print(1)\n")
        values.append(
            inspect_zip_bytes(
                out.getvalue(),
                AdmissionPolicyV1(github_strip_root=True),
                source_revision="a" * 40,
                github_repository_id=42,
            )
        )
    first, second = values
    assert first.archive_sha256 != second.archive_sha256
    assert first.normalized_files_sha256 == second.normalized_files_sha256
    assert semantic_digest(first.model_dump()) != semantic_digest(second.model_dump())
