"""Synthetic archive admission through existing database and object-store services."""

import io
import zipfile
from uuid import uuid4

from app.adapters.storage.local import LocalObjectStore
from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV2, UploadReservationV1
from app.models.phase2_tables import PHASE2_TABLES
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.submissions import SubmissionService
from scripts.phase1_fixture import scenario
from sqlalchemy import insert

from tests.phase2.integration.test_submission_ingestion import chunks


async def admit_contents(factory, tmp_path, contents):
    _, ctx, snap, _, _ = await scenario(factory)
    async with factory() as session, session.begin():
        await session.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=snap.repository_id,
                principal_id=ctx.principal_id,
                permission="write",
            )
        )
        await session.execute(insert(PHASE2_TABLES["org_work_limits"]).values(org_id=ctx.org_id))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in contents.items():
            archive.writestr(path, data)
    body = buffer.getvalue()
    store, policy = LocalObjectStore(tmp_path / "objects"), AdmissionPolicyV2()
    service = SubmissionService(factory, store, policy=policy)
    submission = await service.reserve(
        ctx,
        UploadReservationV1(
            repository_id=snap.repository_id, filename="synthetic.zip", declared_size=len(body)
        ),
        uuid4().hex,
    )
    uploaded = await service.write_content(ctx, submission.id, chunks(body))
    lease = await JobService(factory).claim(ctx.org_id, uploaded.job_id, 1, "admission")
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(lease, inspect_zip_bytes(body, policy))
    result = await finalizer.finish_admission(lease, prepared)
    return ctx, snap.repository_id, result.snapshot_id, store
