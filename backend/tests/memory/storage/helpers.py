"""A second real admission in the same repository; source bytes are fixture owned."""

import io
import zipfile
from uuid import uuid4

from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1, UploadReservationV1
from app.contracts.jobs import AttemptLeaseV2
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.submissions import SubmissionService
from tests.phase2.integration.test_submission_ingestion import chunks


async def admit_another(factory, store, ctx, repo, content=b"print(1)\n"):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.py", content)
    body = archive.getvalue()
    service = SubmissionService(factory, store)
    submission = await service.reserve(
        ctx,
        UploadReservationV1(repository_id=repo, filename="another.zip", declared_size=len(body)),
        str(uuid4()),
    )
    uploaded = await service.write_content(ctx, submission.id, chunks(body))
    lease = await JobService(factory).claim(ctx.org_id, uploaded.job_id, 1, "admission-another")
    assert isinstance(lease, AttemptLeaseV2)
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(
        lease, inspect_zip_bytes(body, AdmissionPolicyV1())
    )
    return (await finalizer.finish_admission(lease, prepared)).snapshot_id
