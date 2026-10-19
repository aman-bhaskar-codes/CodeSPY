"""Dedicated qualification at the actual advertised archive entry boundary."""

import io
import os
import time
import zipfile

import pytest
from app.admission.process import DockerAdmissionRunner
from app.contracts.ingestion import AdmissionPolicyV1, UploadReservationV1
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from tests.phase2.integration.test_submission_ingestion import submission_scenario


@pytest.mark.performance
async def test_ten_thousand_files_fit_linux_and_total_finalization_budget(factory, tmp_path):
    image = os.environ.get("CODESPY_ADMISSION_IMAGE")
    if not image:
        pytest.fail("capacity qualification requires owned Linux admission image")
    service, ctx, request, _, _, store = await submission_scenario(factory, tmp_path)
    count = int(os.environ.get("CODESPY_CAPACITY_ENTRIES", "10000"))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(count):
            archive.writestr(f"src/f{index:05}.py", b"x")
    body = out.getvalue()
    request = UploadReservationV1(
        repository_id=request.repository_id, filename="capacity.zip", declared_size=len(body)
    )
    sub = await service.reserve(ctx, request, "capacity-10000")

    async def upload_blocks():
        for offset in range(0, len(body), 65536):
            yield body[offset : offset + 65536]

    uploaded = await service.write_content(ctx, sub.id, upload_blocks())
    runner = DockerAdmissionRunner(image, tmp_path / "runner")
    before = time.monotonic()
    manifest = await runner.inspect_bytes(body, AdmissionPolicyV1())
    inventory_seconds = time.monotonic() - before
    assert len(manifest.entries) == count
    jobs = JobService(factory)
    lease = await jobs.claim(ctx.org_id, uploaded.job_id, 1, "capacity")
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(lease, manifest)
    before = time.monotonic()
    result = await finalizer.finish_admission(lease, prepared)
    finalization_seconds = time.monotonic() - before
    assert result.state == "succeeded" and finalization_seconds < 60
    print(
        {
            "files": count,
            "archive_bytes": len(body),
            "linux_inventory_seconds": inventory_seconds,
            "finalization_seconds": finalization_seconds,
        }
    )
