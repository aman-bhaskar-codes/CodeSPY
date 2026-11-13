import hashlib
import io
import json
import zipfile
from uuid import uuid4

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.facts_pipeline import analyze_facts
from app.contracts.ast_facts import AstDraftV1
from app.contracts.ingestion import AdmissionPolicyV1, ArtifactManifestV1
from app.contracts.structure import (
    AnalysisConfigV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourcePolicyV1,
)
from app.domain.errors import DuplicateConflict
from app.domain.scope import RunRef
from app.services.ast_results import materialize_facts

FILES = {
    "pkg/service.py": (
        b"import os\n\nclass Service(Base):\n"
        b"    def run(self, a: int) -> str:\n        return os.name\n"
    ),
    "pkg/broken.py": b"def broken(:\n    pass\n",
    "web/app.js": b"export const x = 1;\n",
    "README.md": b"# readme\n",
}


def source_input(root, files=FILES, config=None, policy=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    archive = buf.getvalue()
    policy = policy or AdmissionPolicyV1()
    manifest = inspect_zip_bytes(archive, policy).model_dump_json().encode()
    (root / "archive.zip").write_bytes(archive)
    (root / "manifest.json").write_bytes(manifest)
    config = config or AnalysisConfigV1(toolchain_hash="a" * 64, adapters=("ast_facts",))
    entries = ArtifactManifestV1.model_validate_json(manifest).entries
    return SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=hashlib.sha256(archive).hexdigest(), size_bytes=len(archive)
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest).hexdigest(),
            size_bytes=len(manifest),
        ),
        files=tuple(
            SourceFileV1(
                file_id=uuid4(),
                path=e.path,
                sha256=e.sha256,
                size_bytes=e.size_bytes,
                language=e.language_hint,
                eligible=e.eligible,
                exclusion_reason=e.reason,
            )
            for e in entries
        ),
        policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=config.toolchain_hash,
        request_nonce="nonce",
    )


def ref():
    return RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())


def test_pipeline_extracts_python_and_javascript_and_skips_non_source_files(tmp_path):
    draft = analyze_facts(source_input(tmp_path), tmp_path)
    by_path = {f.path: f for f in draft.files}
    assert set(by_path) == {"pkg/service.py", "pkg/broken.py", "web/app.js"}
    assert by_path["pkg/service.py"].status == "parsed"
    assert by_path["pkg/broken.py"].status == "partial"
    assert by_path["web/app.js"].language == "javascript"
    assert [e.name for e in by_path["web/app.js"].exports] == ["x"]
    cov = draft.coverage
    assert (cov.requested_files, cov.processed_files, cov.omitted_files) == (3, 3, 0)
    assert cov.omissions == () and cov.syntax == "partial"  # broken.py has syntax errors


def test_pipeline_is_deterministic(tmp_path):
    source = source_input(tmp_path)
    first = analyze_facts(source, tmp_path)
    second = analyze_facts(source, tmp_path)
    assert first.model_dump() == second.model_dump()


def test_materialization_accepts_a_faithful_draft_and_marks_partial(tmp_path):
    source = source_input(tmp_path)
    result = materialize_facts(ref(), analyze_facts(source, tmp_path), source, tmp_path)
    assert result.batch.disposition == "partial"
    assert len(result.digests) == 3


def load(payload) -> AstDraftV1:
    return AstDraftV1.model_validate_json(json.dumps(payload))


def tampered(draft: AstDraftV1, **change) -> AstDraftV1:
    return load({**draft.model_dump(mode="json"), **change})


def test_materialization_rejects_identity_and_coverage_drift(tmp_path):
    source = source_input(tmp_path)
    draft = analyze_facts(source, tmp_path)
    for field in (
        "request_nonce",
        "config_hash",
        "toolchain_hash",
        "archive_sha256",
        "policy_hash",
    ):
        value = "forged" if field == "request_nonce" else "f" * 64
        with pytest.raises(DuplicateConflict):
            materialize_facts(ref(), tampered(draft, **{field: value}), source, tmp_path)
    with pytest.raises(DuplicateConflict):
        coverage = {
            **draft.coverage.model_dump(mode="json"),
            "processed_files": 1,
            "omitted_files": 2,
        }
        materialize_facts(ref(), tampered(draft, coverage=coverage), source, tmp_path)


def test_materialization_rejects_facts_that_do_not_cite_original_bytes(tmp_path):
    source = source_input(tmp_path)
    draft = analyze_facts(source, tmp_path)
    payload = draft.model_dump(mode="json")
    service = next(f for f in payload["files"] if f["path"] == "pkg/service.py")
    service["symbols"][0]["range"]["start_byte"] += 1  # no longer a faithful range
    with pytest.raises(ValueError, match="source"):
        materialize_facts(ref(), load(payload), source, tmp_path)


def test_materialization_rejects_files_outside_the_admitted_source(tmp_path):
    source = source_input(tmp_path)
    draft = analyze_facts(source, tmp_path)
    payload = draft.model_dump(mode="json")
    payload["files"][0]["path"] = "pkg/other.py"
    with pytest.raises(DuplicateConflict):
        materialize_facts(ref(), load(payload), source, tmp_path)
    payload = draft.model_dump(mode="json")
    payload["files"][0]["sha256"] = "0" * 64
    with pytest.raises(DuplicateConflict):
        materialize_facts(ref(), load(payload), source, tmp_path)


def test_facts_draft_rejects_duplicate_files(tmp_path):
    draft = analyze_facts(source_input(tmp_path), tmp_path)
    payload = draft.model_dump(mode="json")
    payload["files"].append(payload["files"][0])
    with pytest.raises(ValueError, match="duplicate file facts"):
        load(payload)


def test_v3_archives_expose_new_languages_to_facts_but_not_to_structural(tmp_path):
    from app.analyzers.source import AdmittedSourceReader
    from app.contracts.ingestion import AdmissionPolicyV3

    files = {
        "a.py": b"x = 1\n",
        "svc/main.go": b"package main\n",
        "lib/lib.rs": b"fn f() {}\n",
        "native/x.c": b"int x;\n",
    }
    facts_cfg = AnalysisConfigV1(toolchain_hash="a" * 64, adapters=("ast_facts",))
    source = source_input(tmp_path, files, facts_cfg, AdmissionPolicyV3())
    units = {u.path: u.language for u in AdmittedSourceReader(source, tmp_path).read_selected()}
    assert units == {
        "a.py": "python",
        "lib/lib.rs": "rust",
        "native/x.c": "c",
        "svc/main.go": "go",
    }
    draft = analyze_facts(source, tmp_path)
    assert sorted(f.path for f in draft.files) == [
        "a.py",
        "lib/lib.rs",
        "native/x.c",
        "svc/main.go",
    ]
    assert draft.coverage.omissions == () and draft.coverage.requested_files == 4

    other = tmp_path / "structural"
    other.mkdir()
    structural = source_input(
        other, files, AnalysisConfigV1(toolchain_hash="a" * 64), AdmissionPolicyV3()
    )
    structural_units = AdmittedSourceReader(structural, other).read_selected()
    assert [u.path for u in structural_units] == ["a.py"]  # structural dialects are unchanged


def test_manifests_are_read_as_facts_and_materialize(tmp_path):
    files = {
        **FILES,
        "Cargo.toml": b'[package]\nname = "app"\n[dependencies]\nserde = "1"\n',
        "web/package.json": b'{"name": "web", "dependencies": {"react": "18"}}',
        "bad/pyproject.toml": b"[package",
    }
    source = source_input(tmp_path, files)
    draft = analyze_facts(source, tmp_path)
    by_path = {f.path: f for f in draft.files}
    assert (
        by_path["Cargo.toml"].language == "manifest"
        and by_path["Cargo.toml"].manifest.name == "app"
    )
    assert [d.name for d in by_path["web/package.json"].manifest.dependencies] == ["react"]
    assert (
        by_path["bad/pyproject.toml"].status == "partial"
        and by_path["bad/pyproject.toml"].manifest is None
    )
    assert draft.coverage.requested_files == draft.coverage.processed_files == 6
    result = materialize_facts(ref(), draft, source, tmp_path)
    assert result.batch.disposition == "partial"


def test_go_mod_is_read_under_policy_v4(tmp_path):
    from app.contracts.ingestion import AdmissionPolicyV4

    files = {
        "go.mod": b"module example.com/app\nrequire github.com/x/y v1.0.0\n",
        "a.go": b"package a\n",
    }
    source = source_input(tmp_path, files, None, AdmissionPolicyV4())
    draft = analyze_facts(source, tmp_path)
    by_path = {f.path: f for f in draft.files}
    assert by_path["go.mod"].manifest.name == "example.com/app"
    assert [d.name for d in by_path["go.mod"].manifest.dependencies] == ["github.com/x/y"]


def test_package_swift_is_read_as_a_manifest_through_the_real_pipeline(tmp_path):
    from app.contracts.ingestion import AdmissionPolicyV3

    files = {
        "Package.swift": b'let package = Package(name: "Kit", targets: [.target(name: "Kit")])\n',
        "Sources/Kit/Kit.swift": b"public func a() {}\n",
    }
    source = source_input(tmp_path, files, None, AdmissionPolicyV3())
    by_path = {f.path: f for f in analyze_facts(source, tmp_path).files}
    assert by_path["Package.swift"].language == "manifest"
    assert by_path["Package.swift"].manifest.targets == ("Kit",)
    assert by_path["Sources/Kit/Kit.swift"].language == "swift"
