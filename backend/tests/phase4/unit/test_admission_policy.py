"""Admission V2 is opt-in; existing policy bytes and exclusions stay stable."""

import io
import zipfile

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts import ingestion
from app.contracts.structure import SourcePolicyV1
from pydantic import ValidationError


def archive(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as stream:
        for name, data in files:
            stream.writestr(name, data)
    return out.getvalue()


def v2(**kwargs):
    return ingestion.AdmissionPolicyV2(**kwargs)


def test_v1_default_hashes_and_excluded_lockfile_remain_unchanged():
    assert ingestion.AdmissionPolicyV1().digest() == (
        "b7fbabbf51c5a9541517f08c6133b94818d46e67fb211d14e5452bf97f234f8d"
    )
    assert ingestion.AdmissionPolicyV1(github_strip_root=True).digest() == (
        "f0fe6a03bfd4430ed47937ade001131f51de78916056d2eef9bc19c693e922a1"
    )
    manifest = inspect_zip_bytes(
        archive([("uv.lock", b"version = 1\n")]), ingestion.AdmissionPolicyV1()
    )
    assert not manifest.entries[0].eligible
    assert manifest.entries[0].reason == "unsupported_language"
    assert manifest.entries[0].language_hint is None


def test_v2_authenticates_exact_lock_metadata_without_reinterpreting_old_manifest():
    data = archive([("uv.lock", b"version = 1\n"), ("other.lock", b"not supported")])
    old = inspect_zip_bytes(data, ingestion.AdmissionPolicyV1())
    policy = v2()
    current = inspect_zip_bytes(data, policy)
    assert current.policy_version == "source-zip-v2"
    assert current.policy_hash != old.policy_hash
    assert current.archive_sha256 == old.archive_sha256
    assert current.normalized_files_sha256 != old.normalized_files_sha256
    entries = {e.path: e for e in current.entries}
    assert entries["uv.lock"].eligible
    assert entries["uv.lock"].language_hint == "toml"
    assert entries["other.lock"].reason == "unsupported_language"
    assert ingestion.ArtifactManifestV1.model_validate_json(current.model_dump_json()) == current
    assert old.entries[-1].reason == "unsupported_language"


@pytest.mark.parametrize(
    "path,content,reason",
    [
        ("node_modules/pkg/uv.lock", b"version = 1", "generated_dependency"),
        (".git/uv.lock", b"version = 1", "generated_dependency"),
        ("vendor/uv.lock", b"version = 1", "generated_dependency"),
        ("uv.lock", b"\x00binary", "binary"),
        ("uv.lock", b"x" * 262145, "oversized"),
        ("UV.LOCK", b"version = 1", "unsupported_language"),
        ("uv.lock.backup", b"version = 1", "unsupported_language"),
    ],
    ids=["generated-node", "git", "vendor", "binary", "oversized", "case", "suffix"],
)
def test_v2_preserves_exclusions_and_bounded_exact_metadata(path, content, reason):
    manifest = inspect_zip_bytes(archive([(path, content)]), v2())
    assert not manifest.entries[0].eligible
    assert manifest.entries[0].reason == reason


def test_policy_decoder_discriminates_version_and_source_mode():
    for policy in (ingestion.AdmissionPolicyV1(), v2(), v2(github_strip_root=True)):
        decoded = ingestion.parse_admission_policy(policy.model_dump())
        assert decoded == policy
        assert (
            ingestion.resolve_admission_policy(
                policy.digest(), github_strip_root=policy.github_strip_root
            )
            == policy
        )
        source = SourcePolicyV1(
            policy_hash=policy.digest(),
            strip_single_root=policy.github_strip_root,
            admission=policy,
        )
        assert SourcePolicyV1.model_validate_json(source.model_dump_json()) == source
    with pytest.raises(ValueError):
        ingestion.resolve_admission_policy("0" * 64, github_strip_root=False)
    with pytest.raises(ValueError):
        ingestion.resolve_admission_policy(v2().digest(), github_strip_root=True)
    with pytest.raises(ValidationError):
        ingestion.parse_admission_policy({"version": "source-zip-v3"})


def test_v1_decoder_does_not_accept_v2_fields():
    with pytest.raises(ValidationError):
        ingestion.parse_admission_policy({"version": "source-zip-v1", "metadata_file_bytes": 1})


def test_v2_source_replay_preserves_structural_selection(tmp_path):
    import hashlib
    from uuid import uuid4

    from app.analyzers.source import AdmittedSourceReader
    from app.contracts.structure import (
        AnalysisConfigV1,
        SourceDescriptorV1,
        SourceFileV1,
        SourceInputV1,
    )

    policy = v2()
    raw = archive([("main.py", b"def hello(): pass\n"), ("uv.lock", b"version = 1\n")])
    manifest = inspect_zip_bytes(raw, policy)
    encoded = manifest.model_dump_json().encode()
    (tmp_path / "archive.zip").write_bytes(raw)
    (tmp_path / "manifest.json").write_bytes(encoded)
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    request = SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
        ),
        files=tuple(
            SourceFileV1(
                file_id=uuid4(),
                path=e.path,
                sha256=e.sha256,
                size_bytes=e.size_bytes,
                eligible=e.eligible,
                language=e.language_hint,
                exclusion_reason=e.reason,
            )
            for e in manifest.entries
        ),
        policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=config.toolchain_hash,
        request_nonce="v2-source-test",
    )
    units = AdmittedSourceReader(request, tmp_path).read_selected()
    assert [u.path for u in units] == ["main.py"]
    assert units[0].data == b"def hello(): pass\n"


def test_services_default_to_v1_and_reject_unregistered_policies():
    from app.services.github_acquisition import GitHubAcquisitionService
    from app.services.submissions import SubmissionService

    # Construction must open no clients/DB; methods are never called on these sentinels.
    assert SubmissionService(None, None).policy == ingestion.AdmissionPolicyV1()
    assert SubmissionService(None, None, policy=v2()).policy == v2()
    assert GitHubAcquisitionService(None, None, None, policy=v2(github_strip_root=True)).policy == (
        v2(github_strip_root=True)
    )
    with pytest.raises(ValueError):
        SubmissionService(None, None, policy=v2(github_strip_root=True))
    with pytest.raises(ValueError):
        SubmissionService(None, None, policy=v2(metadata_file_bytes=1))
