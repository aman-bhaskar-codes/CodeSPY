"""Policy V3 admits more languages; V1/V2 manifests for the same bytes never change."""

import io
import zipfile

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts import ingestion

FILES = [
    ("main.go", b"package main\n"),
    ("lib.rs", b"fn main() {}\n"),
    ("A.java", b"class A {}\n"),
    ("a.c", b"int a;\n"),
    ("a.h", b"int a;\n"),
    ("a.cpp", b"int a;\n"),
    ("a.hpp", b"int a;\n"),
    ("a.rb", b"def a; end\n"),
    ("a.php", b"<?php\n"),
    ("a.swift", b"func a() {}\n"),
    ("app.py", b"x = 1\n"),
    ("notes.txt", b"hi\n"),
]
HINTS = {
    "main.go": "go",
    "lib.rs": "rust",
    "A.java": "java",
    "a.c": "c",
    "a.h": "c",
    "a.cpp": "cpp",
    "a.hpp": "cpp",
    "a.rb": "ruby",
    "a.php": "php",
    "a.swift": "swift",
}


def archive(files=FILES):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as stream:
        for name, data in files:
            stream.writestr(name, data)
    return out.getvalue()


def entries(policy, files=FILES):
    return {e.path: e for e in inspect_zip_bytes(archive(files), policy).entries}


@pytest.mark.parametrize("policy", [ingestion.AdmissionPolicyV1(), ingestion.AdmissionPolicyV2()])
def test_older_policies_still_exclude_the_new_languages(policy):
    found = entries(policy)
    for path in HINTS:
        assert not found[path].eligible and found[path].reason == "unsupported_language"
        assert found[path].language_hint is None
    assert found["app.py"].eligible


def test_v3_admits_the_new_languages_with_language_hints():
    found = entries(ingestion.AdmissionPolicyV3())
    for path, hint in HINTS.items():
        assert found[path].eligible and found[path].language_hint == hint, path
    assert found["app.py"].language_hint == "python"
    assert found["notes.txt"].language_hint == "text"


def test_v3_keeps_v2_lock_metadata_and_exclusions():
    files = [("uv.lock", b"version = 1\n"), ("vendor/x.go", b"package x\n"), ("k.pem", b"x")]
    found = entries(ingestion.AdmissionPolicyV3(), files)
    assert found["uv.lock"].eligible and found["uv.lock"].language_hint == "toml"
    assert found["vendor/x.go"].reason == "generated_dependency"
    assert found["k.pem"].reason == "sensitive_path"


def test_v3_has_its_own_identity_and_round_trips():
    v1, v2, v3 = (
        ingestion.AdmissionPolicyV1(),
        ingestion.AdmissionPolicyV2(),
        ingestion.AdmissionPolicyV3(),
    )
    assert len({v1.digest(), v2.digest(), v3.digest()}) == 3
    manifest = inspect_zip_bytes(archive(), v3)
    assert manifest.policy_version == "source-zip-v3" and manifest.policy_hash == v3.digest()
    assert ingestion.ArtifactManifestV1.model_validate_json(manifest.model_dump_json()) == manifest
    assert ingestion.parse_admission_policy(v3.model_dump()) == v3
    for strip in (False, True):
        resolved = ingestion.resolve_admission_policy(
            ingestion.AdmissionPolicyV3(github_strip_root=strip).digest(), github_strip_root=strip
        )
        assert isinstance(resolved, ingestion.AdmissionPolicyV3)


def test_new_policy_does_not_change_existing_default_digests():
    assert ingestion.AdmissionPolicyV1().digest() == (
        "b7fbabbf51c5a9541517f08c6133b94818d46e67fb211d14e5452bf97f234f8d"
    )
    assert ingestion.AdmissionPolicyV1(github_strip_root=True).digest() == (
        "f0fe6a03bfd4430ed47937ade001131f51de78916056d2eef9bc19c693e922a1"
    )


def test_same_archive_yields_identical_archive_digest_under_every_policy():
    data = archive()
    digests = {
        inspect_zip_bytes(data, p).archive_sha256
        for p in (
            ingestion.AdmissionPolicyV1(),
            ingestion.AdmissionPolicyV2(),
            ingestion.AdmissionPolicyV3(),
        )
    }
    assert len(digests) == 1
