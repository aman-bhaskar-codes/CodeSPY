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


def source_input(root, files=FILES, config=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    archive = buf.getvalue()
    policy = AdmissionPolicyV1()
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


def test_pipeline_extracts_python_and_reports_unsupported_languages_as_omissions(tmp_path):
    draft = analyze_facts(source_input(tmp_path), tmp_path)
    by_path = {f.path: f for f in draft.files}
    assert set(by_path) == {"pkg/service.py", "pkg/broken.py"}
    assert by_path["pkg/service.py"].status == "parsed"
    assert by_path["pkg/broken.py"].status == "partial"
    cov = draft.coverage
    assert (cov.requested_files, cov.processed_files, cov.omitted_files) == (3, 2, 1)
    assert [(o.path, o.reason) for o in cov.omissions] == [("web/app.js", "language_unsupported")]
    assert cov.syntax == "partial"


def test_pipeline_is_deterministic(tmp_path):
    source = source_input(tmp_path)
    first = analyze_facts(source, tmp_path)
    second = analyze_facts(source, tmp_path)
    assert first.model_dump() == second.model_dump()


def test_materialization_accepts_a_faithful_draft_and_marks_partial(tmp_path):
    source = source_input(tmp_path)
    result = materialize_facts(ref(), analyze_facts(source, tmp_path), source, tmp_path)
    assert result.batch.disposition == "partial"
    assert len(result.digests) == 2


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
