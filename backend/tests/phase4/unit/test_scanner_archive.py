"""Scanner source selection reuses admitted archive authority without structural config."""

import hashlib
from uuid import UUID

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.source import SourceIntegrityError
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.scanners import ScannerArchiveV1, ScannerRequestV1
from app.contracts.structure import SourceDescriptorV1, SourceFileV1, SourcePolicyV1
from app.scanners.source import read_scanner_sources

from tests.phase3.unit.test_source import create_test_archive


def fixture_input(root):
    archive, manifest, _ = create_test_archive()
    (root / "archive.zip").write_bytes(archive)
    (root / "manifest.json").write_bytes(manifest)
    policy = AdmissionPolicyV1()
    entries = inspect_zip_bytes(archive, policy).entries
    files = tuple(
        SourceFileV1(
            file_id=UUID(int=i + 1),
            path=e.path,
            sha256=e.sha256,
            size_bytes=e.size_bytes,
            eligible=e.eligible,
            exclusion_reason=e.reason,
            language=e.language_hint,
        )
        for i, e in enumerate(entries)
    )
    selected = next(f for f in files if f.path == "src/main.py")
    request = ScannerRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=10), "snapshot_id": UUID(int=11), "run_id": UUID(int=12)},
            "repository_id": UUID(int=13),
            "config_hash": "a" * 64,
            "profile": {
                "tool": "semgrep",
                "artifact_sha256": "b" * 64,
                "rules_sha256": "c" * 64,
                "rule_ids": ("python.shell-true",),
            },
            "files": (
                {
                    "file_id": selected.file_id,
                    "sha256": selected.sha256,
                    "size_bytes": selected.size_bytes,
                },
            ),
        }
    )
    return ScannerArchiveV1(
        request=request,
        files=files,
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=hashlib.sha256(archive).hexdigest(), size_bytes=len(archive)
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest).hexdigest(),
            size_bytes=len(manifest),
        ),
        policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
    )


def test_selected_bytes_preserve_stored_file_identity(tmp_path):
    source = fixture_input(tmp_path)
    actual = read_scanner_sources(source, tmp_path)
    assert actual == {source.request.files[0].file_id: b"def hello():\n    return 'world'\n"}
    assert not hasattr(source, "config")


@pytest.mark.parametrize("filename", ["archive.zip", "manifest.json"])
def test_modified_admitted_objects_are_rejected_before_scanning(tmp_path, filename):
    source = fixture_input(tmp_path)
    (tmp_path / filename).write_bytes(b"corrupt")
    with pytest.raises(SourceIntegrityError, match="source_integrity_error"):
        read_scanner_sources(source, tmp_path)


def test_foreign_candidate_file_id_cannot_replace_persisted_identity(tmp_path):
    source = fixture_input(tmp_path)
    candidate = source.request.files[0].model_copy(update={"file_id": UUID(int=999)})
    changed = source.model_dump()
    changed["request"]["files"] = (candidate.model_dump(),)
    with pytest.raises(ValueError):
        ScannerArchiveV1.model_validate(changed)
