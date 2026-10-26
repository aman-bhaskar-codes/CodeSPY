import hashlib
import io
import zipfile
from pathlib import Path
from uuid import uuid4

from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.pipeline import analyze
from app.analyzers.reporting import render_json, render_markdown
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.structure import (
    AnalysisConfigV1,
    ArchitectureReportV1,
    ServiceBindingV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourcePolicyV1,
)
from app.domain.scope import RunRef


def create_pipeline_fixture() -> tuple[bytes, bytes, dict[str, bytes]]:
    files = {
        "api/main.py": b"""from fastapi import FastAPI
app = FastAPI()

@app.get("/items")
def get_items():
    return [1, 2, 3]
""",
        "web/app.js": b"""fetch("/items");
""",
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


def test_full_pipeline_and_reporting(tmp_path: Path):
    archive_bytes, manifest_bytes, files = create_pipeline_fixture()
    (tmp_path / "archive.zip").write_bytes(archive_bytes)
    (tmp_path / "manifest.json").write_bytes(manifest_bytes)

    policy = AdmissionPolicyV1()
    bindings = (
        ServiceBindingV1(
            source_prefix="api",
            service_key="api",
            canonical_origin="https://api.test",
        ),
        ServiceBindingV1(
            source_prefix="web",
            service_key="web",
            canonical_origin="https://web.test",
            same_origin_service="api",
        ),
    )
    config = AnalysisConfigV1(
        toolchain_hash="0" * 64,
        service_bindings=bindings,
    )

    source_files = [
        SourceFileV1(
            file_id=uuid4(),
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            eligible=True,
            exclusion_reason=None,
            language="python" if path.endswith(".py") else "javascript",
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
        request_nonce="pipeline-nonce-1",
    )

    draft = analyze(source_input, tmp_path)

    # Validate draft shape
    assert draft.schema_version == 1
    assert len(draft.symbols) >= 2
    assert len(draft.routes) == 1
    assert draft.routes[0].template_path == "/items"
    assert len(draft.requests) == 1
    assert draft.requests[0].path == "/items"
    assert len(draft.links) == 1
    assert draft.links[0].state == "candidate"

    # Coverage reconciliation
    cov = draft.coverage
    assert cov.processed_files == 2
    assert cov.requested_files == 2
    assert cov.omitted_files == 0
    assert cov.syntax == "complete"
    assert cov.resolution == "complete"

    # ArchitectureReportV1 instantiation & rendering
    run_ref = RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4())
    report = ArchitectureReportV1(
        run_ref=run_ref,
        request_nonce=draft.request_nonce,
        config_hash=draft.config_hash,
        toolchain_hash=draft.toolchain_hash,
        archive_sha256=draft.archive_sha256,
        policy_hash=draft.policy_hash,
        symbols=draft.symbols,
        resolutions=draft.resolutions,
        routes=draft.routes,
        requests=draft.requests,
        links=draft.links,
        diagnostics=draft.diagnostics,
        components=draft.components,
        coverage=draft.coverage,
    )

    rendered_json = render_json(report)
    assert len(rendered_json) > 100
    assert '"routes"' in rendered_json

    rendered_md = render_markdown(report)
    assert "# CodeSPY Architecture Report" in rendered_md
    assert "/items" in rendered_md
    assert "Nonclaims" in rendered_md


def test_candidate_call_components_use_enclosing_functions(tmp_path):
    import io
    import zipfile

    from app.contracts.structure import SourceFileV1

    from tests.phase3.unit.test_materialization import fixture_input

    source = fixture_input(tmp_path)
    code = b"def alpha():\n beta()\ndef beta():\n alpha()\nalpha()\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.py", code)
    raw = buffer.getvalue()
    manifest = inspect_zip_bytes(raw, AdmissionPolicyV1()).model_dump_json().encode()
    (tmp_path / "archive.zip").write_bytes(raw)
    (tmp_path / "manifest.json").write_bytes(manifest)
    source = source.model_copy(
        update={
            "archive": SourceDescriptorV1(
                path="archive.zip", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
            ),
            "manifest": SourceDescriptorV1(
                path="manifest.json",
                sha256=hashlib.sha256(manifest).hexdigest(),
                size_bytes=len(manifest),
            ),
            "files": (
                SourceFileV1(
                    file_id=uuid4(),
                    path="main.py",
                    sha256=hashlib.sha256(code).hexdigest(),
                    size_bytes=len(code),
                    language="python",
                    eligible=True,
                ),
            ),
        }
    )
    draft = analyze(source, tmp_path)
    calls = next(c for c in draft.components if c.relation == "candidate_calls")
    expected = {s.semantic_key for s in draft.symbols if s.kind == "function"}
    assert calls.state == "complete" and any(set(group) == expected for group in calls.components)
