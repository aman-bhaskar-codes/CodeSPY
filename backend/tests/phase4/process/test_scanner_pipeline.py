"""Real admitted archive to isolated scanner checks using synthetic source only."""

import hashlib
import io
import zipfile
from uuid import UUID

import pytest
from app.adapters.storage.local import LocalObjectStore
from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.scanners import ScannerRequestV1
from app.contracts.structure import SourceFileV1
from app.scanners.adapter import DockerScannerAdapter
from app.scanners.docker import DockerController, SandboxLaunch
from app.scanners.ownership import ScannerOwnership
from app.scanners.runtime import RuntimeManifest
from app.services.analysis_sources import LocatedSource
from app.services.findings import FindingMaterializer, ScanPublicationSpecV1, reviewed_catalog
from app.services.scanner_sources import BoundScannerStager

from tests.phase4.dependency_fixtures import NOW
from tests.phase4.process.test_native_secret_format import cases
from tests.phase4.process.test_scanner_image import settings
from tests.phase4.unit.test_scanner_archive import fixture_input


async def chunks(data):
    yield data


def corpus(tool, kind):
    shell = b'subprocess.run("example", shell=True)\n'
    token = b"ghp_" + b"A" * 36
    rule = "python.shell-true" if tool == "semgrep" else "secret.github-classic-pat"
    positive = shell if tool == "semgrep" else b'token = "' + token + b'"\n'
    if kind == "limits":
        contents = {}
        for i in range(128):
            padding = hashlib.shake_256(str(i).encode()).hexdigest(65536).encode()
            contents[f"src/file_{i}.py"] = (
                b"#" + padding[: 131072 - len(positive) - 2] + b"\n" + positive
            )
        assert sum(map(len, contents.values())) == 16777216
        return contents, {(path, rule) for path in contents}
    if kind == "formats" and tool == "gitleaks":
        contents = {f"src/case_{i}.py": data for i, (_, data, _) in enumerate(cases())}
        return contents, {
            (f"src/case_{i}.py", rule)
            for i, (_, _, start) in enumerate(cases())
            if start is not None
        }
    if kind == "formats":
        contents, expected = {}, set()
        for i in range(20):
            prefix = (f"# example {i} 雪\r\n").encode()
            for label, statement, selected in (
                ("shell", shell, "python.shell-true"),
                (
                    "tls",
                    b'requests.get("https://example.invalid", verify=False)\n',
                    "python.requests-verify-false",
                ),
            ):
                path = f"src/{label}_{i}.py"
                contents[path] = prefix + statement
                expected.add((path, selected))
                contents[f"src/safe_{label}_{i}.py"] = (
                    prefix + statement.replace(b"True", b"False")
                    if label == "shell"
                    else prefix + statement.replace(b"False", b"True")
                )
        return contents, expected
    return {"src/positive.py": positive, "src/negative.py": b"print('example')\n"}, {
        ("src/positive.py", rule)
    }


@pytest.mark.parametrize("kind", ["smoke", "formats", "limits"])
@pytest.mark.parametrize("tool", ["semgrep", "gitleaks"])
async def test_real_store_archive_scan_preserves_findings_and_file_ids(tmp_path, tool, kind):
    from pathlib import Path

    binary, socket, image = settings()
    owner = ScannerOwnership.create(tmp_path / "owned", image)
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    name = owner.reserve()
    probe = owner.root / (name + ".input")
    probe.mkdir()
    source = fixture_input(probe)
    (probe / "request.json").write_text(source.model_dump_json())
    try:
        await controller.create(SandboxLaunch(name, owner.marker, image, probe, mode="identities"))
        native = await controller.start(name, 65536)
        manifest = RuntimeManifest.model_validate_json(native.output)
    finally:
        await owner.remove(name, controller)

    contents, expected = corpus(tool, kind)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in contents.items():
            archive.writestr(path, content)
    archive_bytes = buffer.getvalue()
    policy = AdmissionPolicyV1()
    inventory = inspect_zip_bytes(archive_bytes, policy)
    files = tuple(
        SourceFileV1(
            file_id=UUID(int=i + 100),
            path=e.path,
            sha256=e.sha256,
            size_bytes=e.size_bytes,
            eligible=e.eligible,
            exclusion_reason=e.reason,
            language=e.language_hint,
        )
        for i, e in enumerate(inventory.entries)
    )
    store = LocalObjectStore(tmp_path / "objects")
    stored_archive = await store.put_stage("stage/scan/archive", chunks(archive_bytes), 104857600)
    stored_manifest = await store.put_stage(
        "stage/scan/manifest", chunks(inventory.model_dump_json().encode()), 67108864
    )
    runtime = next(t for t in manifest.tools if t.tool == tool)
    data = source.request.model_dump()
    data.update(
        profile=runtime.profile(),
        files=tuple(
            {"file_id": f.file_id, "sha256": f.sha256, "size_bytes": f.size_bytes} for f in files
        ),
    )
    request = ScannerRequestV1.model_validate(data)
    stager = BoundScannerStager(
        store,
        LocatedSource(stored_archive, stored_manifest, files, policy, None),
        request.scope,
        request.repository_id,
    )
    adapter = DockerScannerAdapter(
        owner,
        controller,
        stager,
        manifest,
        expected_entrypoint=await controller.image_entrypoint(image),
    )
    receipt = await adapter.scan_attested(request)
    result = receipt.result
    assert result.status == "completed"
    assert set(result.processed_file_ids) == {f.file_id for f in files}
    paths = {f.file_id: f.path for f in files}
    assert {(paths[o.file_id], o.rule_id) for o in result.observations} == expected
    assert len(result.observations) == len(expected)
    for observation in result.observations:
        assert (
            observation.sha256 == hashlib.sha256(contents[paths[observation.file_id]]).hexdigest()
        )
    assert (b"ghp_" + b"A" * 36) not in result.model_dump_json().encode()
    catalog = reviewed_catalog((request.profile,))
    draft = FindingMaterializer(catalog).materialize(
        ScanPublicationSpecV1(
            scope=request.scope,
            repository_id=request.repository_id,
            config_hash=request.config_hash,
            runtime_digest=image.removeprefix("sha256:"),
            catalog_digest=catalog.semantic_hash(),
            captured_at=NOW,
            native_requests=(request,),
        ),
        (receipt,),
    )
    assert len(draft.findings) == len(draft.evidence) == len(draft.decisions) == len(expected)
    assert draft.coverage == "complete"
    assert (b"ghp_" + b"A" * 36) not in draft.model_dump_json().encode()
    assert list(owner.root.glob("codespy-scan-*")) == []
