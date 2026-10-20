"""Advertised 128-file / 16-MiB source ceiling in the owned Linux runtime."""

import base64
import hashlib
import io
import os
import random
import time
import zipfile
from uuid import uuid4

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.identities import normalize_report
from app.analyzers.runner import DockerAnalysisRunner
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.structure import (
    AnalysisConfigV1,
    ArchitectureReportV1,
    SourceDescriptorV1,
    SourceFileV1,
    SourceInputV1,
    SourcePolicyV1,
)
from app.domain.canonical_json import semantic_digest
from app.domain.scope import RunRef


@pytest.mark.parametrize("language", ["python", "typescript"])
async def test_maximum_source_profile_is_bounded_and_repeatable(tmp_path, language):
    image = os.environ.get("CODESPY_ANALYSIS_IMAGE")
    if not image:
        pytest.skip("owned Linux image required for capacity qualification")
    rng = random.Random(73129)  # noqa: S311 - reproducible synthetic source bytes
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for i in range(128):
            prefix = (
                f"def helper_{i}():\n return 1\nhelper_{i}()\n#"
                if language == "python"
                else f"export function helper_{i}(){{return 1;}}\nhelper_{i}();\n//"
            ).encode()
            padding = base64.b64encode(rng.randbytes(100000))
            data = prefix + padding[: 131072 - len(prefix)]
            assert len(data) == 131072
            folder = f"project_{i // 4}/" if language == "typescript" else ""
            suffix = "ts" if language == "typescript" else "py"
            archive.writestr(f"{folder}source_{i:03}.{suffix}", data)
        if language == "typescript":
            import json

            for project in range(32):
                archive.writestr(
                    f"project_{project}/tsconfig.json",
                    json.dumps(
                        {
                            "files": [
                                f"source_{i:03}.ts" for i in range(project * 4, project * 4 + 4)
                            ]
                        }
                    ),
                )
    raw = stream.getvalue()
    policy = AdmissionPolicyV1()
    manifest = inspect_zip_bytes(raw, policy)
    manifest_raw = manifest.model_dump_json().encode()
    (tmp_path / "archive.zip").write_bytes(raw)
    (tmp_path / "manifest.json").write_bytes(manifest_raw)
    runner = DockerAnalysisRunner(image, tmp_path / "owner")
    toolchain = await runner.readiness()
    config = AnalysisConfigV1(toolchain_hash=toolchain)
    source = SourceInputV1(
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest_raw).hexdigest(),
            size_bytes=len(manifest_raw),
        ),
        policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
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
            for e in manifest.entries
        ),
        config=config,
        config_hash=config.semantic_hash(),
        toolchain_hash=toolchain,
        request_nonce="capacity",
    )
    started = time.monotonic()
    first = await runner.analyze(source, tmp_path)
    assert first.coverage.processed_files == 128 and first.coverage.omitted_files == 0
    (
        tmp_path / "request.json"
    ).unlink()  # Own generated transport file; each launch creates it exclusively.
    second = await runner.analyze(source, tmp_path)

    def digest(draft):
        report = ArchitectureReportV1(
            **draft.model_dump(),
            run_ref=RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4()),
        )
        return semantic_digest(normalize_report(report))

    assert digest(first) == digest(second)
    print(
        {
            "language": language,
            "source_files": 128,
            "source_bytes": 16777216,
            "symbols": len(first.symbols),
            "two_run_seconds": round(time.monotonic() - started, 3),
            "toolchain": toolchain,
            "resource_metrics": runner.last_metrics,
        }
    )
