import hashlib
import io
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.source import AdmittedSourceReader, SourceIntegrityError
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.structure import (
    AnalysisConfigV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourcePolicyV1,
)


def create_test_archive() -> tuple[bytes, bytes, dict[str, bytes]]:
    files = {
        "src/main.py": b"def hello():\n    return 'world'\n",
        "package.json": b'{"name": "test-pkg"}\n',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    archive_bytes = buf.getvalue()
    policy = AdmissionPolicyV1()
    manifest = inspect_zip_bytes(archive_bytes, policy)
    manifest_bytes = manifest.model_dump_json().encode("utf-8")
    return archive_bytes, manifest_bytes, files


def test_admitted_source_reader_success(tmp_path: Path):
    archive_bytes, manifest_bytes, files = create_test_archive()
    archive_path = tmp_path / "archive.zip"
    manifest_path = tmp_path / "manifest.json"
    archive_path.write_bytes(archive_bytes)
    manifest_path.write_bytes(manifest_bytes)

    policy = AdmissionPolicyV1()
    config = AnalysisConfigV1(toolchain_hash="0" * 64)

    source_files = []
    for path, data in sorted(files.items()):
        source_files.append(
            SourceFileV1(
                file_id=uuid4(),
                path=path,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                eligible=True,
                exclusion_reason=None,
                language="python" if path.endswith(".py") else "json",
            )
        )

    source_input = SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip",
            sha256=hashlib.sha256(archive_bytes).hexdigest(),
            size_bytes=len(archive_bytes),
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
        ),
        files=tuple(source_files),
        policy=SourcePolicyV1(
            strip_single_root=False,
            policy_hash=policy.digest(),
            admission=policy,
        ),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=config.toolchain_hash,
        request_nonce="test-nonce",
    )

    reader = AdmittedSourceReader(source_input, tmp_path)
    units = reader.read_selected()

    assert len(units) == 1
    assert units[0].path == "src/main.py"
    assert units[0].language == "python"
    assert units[0].data == files["src/main.py"]
    assert len(reader.configs) == 1
    assert reader.configs[0]["path"] == "package.json"


def test_admitted_source_reader_tampered_archive_rejected(tmp_path: Path):
    archive_bytes, manifest_bytes, files = create_test_archive()
    # Write corrupted archive
    (tmp_path / "archive.zip").write_bytes(archive_bytes + b"tampered")
    (tmp_path / "manifest.json").write_bytes(manifest_bytes)

    policy = AdmissionPolicyV1()
    config = AnalysisConfigV1(toolchain_hash="0" * 64)

    source_files = [
        SourceFileV1(
            file_id=uuid4(),
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            eligible=True,
            exclusion_reason=None,
            language="python" if path.endswith(".py") else "json",
        )
        for path, data in sorted(files.items())
    ]

    source_input = SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip",
            sha256=hashlib.sha256(archive_bytes).hexdigest(),
            size_bytes=len(archive_bytes),
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
        ),
        files=tuple(source_files),
        policy=SourcePolicyV1(
            strip_single_root=False,
            policy_hash=policy.digest(),
            admission=policy,
        ),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=config.toolchain_hash,
        request_nonce="test-nonce",
    )

    reader = AdmittedSourceReader(source_input, tmp_path)
    with pytest.raises(SourceIntegrityError):
        reader.read_selected()
