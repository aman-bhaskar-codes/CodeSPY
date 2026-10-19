import io
import zipfile
from uuid import uuid4

import pytest
from app.adapters.storage.local import LocalObjectStore
from app.contracts.ingestion import UploadReservationV1
from app.models.phase2_tables import PHASE2_TABLES
from app.services.submissions import SubmissionService
from scripts.phase1_fixture import scenario
from sqlalchemy import insert, select


async def chunks(data):
    for offset in range(0, len(data), 7):
        yield data[offset : offset + 7]


def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.py", b"print(1)\n")
    return buf.getvalue()


async def submission_scenario(factory, tmp_path):
    _, ctx, snap, _, _ = await scenario(factory)
    async with factory() as s, s.begin():
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=snap.repository_id,
                principal_id=ctx.principal_id,
                permission="write",
            )
        )
        await s.execute(insert(PHASE2_TABLES["org_work_limits"]).values(org_id=ctx.org_id))
    store = LocalObjectStore(tmp_path / "objects")
    svc = SubmissionService(factory, store)
    body = zip_bytes()
    request = UploadReservationV1(
        repository_id=snap.repository_id, filename="fixture.zip", declared_size=len(body)
    )
    key = uuid4().hex
    sub = await svc.reserve(ctx, request, key)
    return svc, ctx, request, sub, body, store


async def test_verified_upload_schedules_one_job_and_holds_reservation(factory, tmp_path):
    svc, ctx, request, sub, body, _ = await submission_scenario(factory, tmp_path)
    uploaded = await svc.write_content(ctx, sub.id, chunks(body))
    assert uploaded.state == "admission_queued" and uploaded.job_id
    replay = await svc.write_content(ctx, sub.id, chunks(body))
    assert replay.job_id == uploaded.job_id
    async with factory() as s:
        assert (
            await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == request.declared_size
        )
        assert (
            await s.scalar(
                select(PHASE2_TABLES["source_submissions"].c.reservation_released_at).where(
                    PHASE2_TABLES["source_submissions"].c.id == sub.id
                )
            )
            is None
        )


async def test_cancel_before_job_releases_exactly_once(factory, tmp_path):
    svc, ctx, _, sub, _, _ = await submission_scenario(factory, tmp_path)
    assert (await svc.cancel(ctx, sub.id)).state == "cancelled"
    await svc.cancel(ctx, sub.id)
    async with factory() as s:
        assert (
            await s.scalar(
                select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                    PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
                )
            )
            == 0
        )


async def test_manual_retry_creates_new_submission_and_reservation(factory, tmp_path):
    from app.services.retries import RetryService

    svc, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await svc.write_content(ctx, sub.id, chunks(body))
    await svc.cancel(ctx, sub.id)
    retry = RetryService(factory, store)
    key = uuid4().hex
    result = await retry.retry(ctx, uploaded.job_id, key)
    assert result.submission_id != sub.id and result.retry_job_id != uploaded.job_id
    assert (await retry.retry(ctx, uploaded.job_id, key)) == result
    assert (await svc.get(ctx, sub.id)).state == "cancelled"
    assert (await svc.get(ctx, result.submission_id)).state == "admission_queued"
    async with factory() as s:
        assert await s.scalar(
            select(PHASE2_TABLES["org_work_limits"].c.reserved_bytes).where(
                PHASE2_TABLES["org_work_limits"].c.org_id == ctx.org_id
            )
        ) == len(body)


async def test_terminal_submission_cannot_reopen_by_raw_update(factory, tmp_path):
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    svc, ctx, _, sub, _, _ = await submission_scenario(factory, tmp_path)
    await svc.cancel(ctx, sub.id)
    with pytest.raises(DBAPIError) as error:
        async with factory() as s, s.begin():
            table = PHASE2_TABLES["source_submissions"]
            await s.execute(
                update(table)
                .where(table.c.org_id == ctx.org_id, table.c.id == sub.id)
                .values(state="reserved", finished_at=None, reservation_released_at=None)
            )
    assert getattr(error.value.orig, "sqlstate", None) == "23514"
