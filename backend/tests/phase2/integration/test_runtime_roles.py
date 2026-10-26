from uuid import uuid4

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import AdmissionPolicyV1
from app.models.tables import artifacts
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.submissions import SubmissionService
from sqlalchemy import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


async def test_api_and_broker_roles_execute_the_slice_without_migration_privileges(
    factory, db_values, tmp_path
):
    _, ctx, _request, sub, body, store = await submission_scenario(factory, tmp_path)
    base = factory.kw["bind"].url
    api_engine = create_async_engine(
        base.set(username="p2_api", password=db_values["CODESPY_TEST_APP_PASSWORD"])
    )
    broker_engine = create_async_engine(
        base.set(username="p2_broker", password=db_values["CODESPY_TEST_BROKER_PASSWORD"])
    )
    try:
        api = async_sessionmaker(api_engine, expire_on_commit=False)
        broker = async_sessionmaker(broker_engine, expire_on_commit=False)
        uploaded = await SubmissionService(api, store).write_content(ctx, sub.id, chunks(body))
        lease = await JobService(broker).claim(ctx.org_id, uploaded.job_id, 1, "broker-role")
        finalizer = JobFinalizer(broker, store)
        prepared = await finalizer.prepare_admission(
            lease, inspect_zip_bytes(body, AdmissionPolicyV1())
        )
        assert (await finalizer.finish_admission(lease, prepared)).state == "succeeded"
        with pytest.raises(DBAPIError) as error:
            async with api.begin() as s:
                await s.execute(insert(artifacts).values(org_id=ctx.org_id, id=uuid4()))
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
    finally:
        await api_engine.dispose()
        await broker_engine.dispose()
